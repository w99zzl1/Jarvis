"""Streaming Speech-to-Text for Jarvis.

All speech processing lives in this module: endpointing (Silero VAD), interim
captions (local tiny model) and final transcription. The final transcript uses
Groq's hosted Whisper by default and falls back to local faster-whisper when no
API key is configured, the request fails, or the network is unavailable. Set
``stt.provider = "local"`` to force fully offline operation.

Groq exposes only batch transcription, so preview captions deliberately stay on
the local tiny model: calling a hosted API every ~1 s would exhaust the free
tier (20 requests/minute). The client only captures PCM audio and renders the
text events it receives over the WebSocket.

Transport
---------
``WS /stt/stream`` accepts 16 kHz mono signed-16-bit PCM:

* client -> server: binary frames with raw PCM; ``{"type": "stop"}`` to finish
  explicitly (the user pressed Stop);
* server -> client: ``{"type": "partial", "delta", "text"}`` for newly
  recognised words, ``{"type": "final", "text"}`` once the utterance is
  complete, or ``{"type": "error", "message"}`` on failure.

The backend also finalises automatically when Silero VAD detects a pause of
``_ENDPOINT_SILENCE_S`` after speech, so no client-side timer is involved.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import tempfile
import wave

from fastapi import File, UploadFile, WebSocket, WebSocketDisconnect

from server.security import expected_token
from shared.config_loader import get_stt_config

log = logging.getLogger("server.stt")

# Lazy-loaded models. ``None`` means not loaded yet, ``False`` = unavailable.
_whisper_model = None
_partial_model = None
_cached_stt_config: dict | None = None

# Endpointing / captioning parameters. These are ASR tuning values, not UI
# heuristics: they describe what counts as the end of an utterance.
_ENDPOINT_SILENCE_S = 1.2
_MIN_SPEECH_S = 0.4
_PARTIAL_WINDOW_S = 5.0
_VAD_TAIL_S = 4.0
_SAMPLE_RATE = 16000
_BYTES_PER_SAMPLE = 2


def _get_stt_config():
    global _cached_stt_config
    if _cached_stt_config is None:
        _cached_stt_config = get_stt_config()
    return _cached_stt_config


def _language() -> str:
    """UI is Russian; allow an explicit override in the STT config."""
    return _get_stt_config().get("language", "ru")


def _stt_provider() -> str:
    """Configured final-transcription backend: ``groq`` or ``local``."""
    value = str(_get_stt_config().get("provider", "groq")).strip().lower()
    return value if value in {"groq", "local"} else "groq"


def _groq_api_key() -> str | None:
    """Read the Groq key from app config, then env. Never log the value."""
    from shared.config_loader import get_api_keys

    entry = get_api_keys().get("groq")
    key = ""
    if isinstance(entry, dict):
        key = str(entry.get("key") or entry.get("api_key") or "")
    elif isinstance(entry, str):
        key = entry
    if not key.strip():
        key = os.environ.get("GROQ_API_KEY", "")
    return key.strip() or None


_groq_unavailable_logged = False


def _groq_configured() -> bool:
    global _groq_unavailable_logged
    if _stt_provider() != "groq":
        return False
    key = _groq_api_key()
    if key:
        return True
    if not _groq_unavailable_logged:
        log.warning("[STT] no Groq API key — using local Whisper for final transcription")
        _groq_unavailable_logged = True
    return False


def _get_whisper_model():
    """Accurate model used once per finished utterance."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    config = _get_stt_config()
    try:
        from faster_whisper import WhisperModel

        _whisper_model = WhisperModel(
            config.get("model", "small"),
            device=config.get("device", "cpu"),
            compute_type=config.get("compute_type", "int8"),
        )
        log.info(
            "[STT] model '%s' loaded (%s/%s)",
            config.get("model"),
            config.get("device"),
            config.get("compute_type"),
        )
        return _whisper_model
    except Exception as e:
        log.warning("[STT] faster-whisper unavailable: %s", e)
        _whisper_model = False
        return None


def _get_partial_model():
    """Small model used for low-latency interim captions."""
    global _partial_model
    if _partial_model is not None:
        return _partial_model
    config = _get_stt_config()
    try:
        from faster_whisper import WhisperModel

        _partial_model = WhisperModel(
            config.get("partial_model", "tiny"),
            device=config.get("device", "cpu"),
            compute_type=config.get("compute_type", "int8"),
            # Keep the caption model off the main model's CPU threads.
            cpu_threads=2,
        )
        log.info("[STT] partial model '%s' loaded", config.get("partial_model", "tiny"))
        return _partial_model
    except Exception as e:
        log.warning("[STT] partial model unavailable: %s", e)
        _partial_model = False
        return None


def _invalidate_stt_model():
    """Force reload of Whisper models with new config (used by settings UI)."""
    global _whisper_model, _partial_model, _cached_stt_config
    _whisper_model = None
    _partial_model = None
    _cached_stt_config = None


# ---------------------------------------------------------------------------
# Legacy single-file endpoint (kept for API compatibility)
# ---------------------------------------------------------------------------

async def transcribe_audio(file: UploadFile) -> dict:
    """Transcribe an uploaded audio file through the same provider chain."""
    import io

    import numpy as np
    from faster_whisper.audio import decode_audio

    try:
        pcm = decode_audio(io.BytesIO(await file.read()), sampling_rate=_SAMPLE_RATE)
        pcm_bytes = (pcm * 32767).astype("<i2").tobytes()
        text, backend = _transcribe_final_pcm(pcm_bytes)
        return {"text": text, "backend": backend, "language": _language()}
    except Exception as e:
        log.warning("[STT] transcription failed: %s", e)
        return {"text": "", "error": str(e)}


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _new_words(previous: str, current: str) -> str:
    """Return only the words in ``current`` not already present in ``previous``.

    Both strings describe the same sliding recognition window; consecutive
    words that overlap at the boundary must not be emitted twice.
    """
    prev = previous.split()
    curr = current.split()
    for n in range(min(len(prev), len(curr)), 0, -1):
        if prev[-n:] == curr[:n]:
            return " ".join(curr[n:])
    return current


def _write_wav(pcm_s16le: bytes) -> str:
    """Write raw 16 kHz mono signed-16-bit PCM to a temporary WAV file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    path = tmp.name
    tmp.close()
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(_BYTES_PER_SAMPLE)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(pcm_s16le)
    return path


def _transcribe_groq(pcm_s16le: bytes) -> str:
    """Final transcription through Groq's hosted Whisper model."""
    from groq import Groq

    key = _groq_api_key()
    if not key:
        raise RuntimeError("Groq API key is not configured")
    model = _get_stt_config().get("groq_model", "whisper-large-v3-turbo")
    path = _write_wav(pcm_s16le)
    try:
        # Short timeout: a slow/blocked network must not stall the reply. On any
        # failure the caller falls back to the local model.
        timeout = float(_get_stt_config().get("groq_timeout_s", 8))
        client = Groq(api_key=key, timeout=timeout)
        with open(path, "rb") as f:
            result = client.audio.transcriptions.create(
                model=model,
                file=(os.path.basename(path), f, "audio/wav"),
                language=_language(),
                response_format="text",
            )
        text = result if isinstance(result, str) else getattr(result, "text", "")
        return str(text).strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _transcribe_local(pcm_s16le: bytes) -> str:
    """Final transcription through the local faster-whisper model."""
    model = _get_whisper_model()
    if model is None:
        raise RuntimeError("STT unavailable on this machine")
    path = _write_wav(pcm_s16le)
    try:
        segments, info = model.transcribe(
            path, language=_language(), beam_size=5, vad_filter=False
        )
        text = "".join(s.text for s in segments).strip()
        log.debug("[STT] local final language=%s", info.language)
        return text
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _transcribe_final_pcm(pcm_s16le: bytes) -> tuple[str, str]:
    """Return ``(text, backend)``. Groq first; local Whisper on any failure."""
    if _groq_configured():
        try:
            return _transcribe_groq(pcm_s16le), "groq"
        except Exception as e:
            log.warning("[STT] Groq transcription failed, falling back to local: %s", e)
    return _transcribe_local(pcm_s16le), "local"


# ---------------------------------------------------------------------------
# Streaming session — owns every decision about text and utterance boundaries
# ---------------------------------------------------------------------------

class StreamingSession:
    """One WebSocket utterance: PCM buffer, VAD state, caption + final output."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.pcm = bytearray()
        self.stop_requested = asyncio.Event()
        self.finished = asyncio.Event()
        self.last_window = ""
        self.emitted = ""
        self.saw_speech = False
        self.first_speech_sample: int | None = None
        # Absolute sample index of the end of the most recent speech segment.
        # Storing it in audio time (not wall time) is what makes endpointing
        # independent of the 4 s VAD tail and of event-loop scheduling.
        self.last_speech_sample: int | None = None
        # Serialises VAD + interim transcription: no overlapping CPU work and
        # no out-of-order partial events.
        self.analysis_lock = asyncio.Lock()

    def add_audio(self, data: bytes) -> None:
        self.pcm.extend(data)

    def request_stop(self) -> None:
        self.stop_requested.set()

    async def run(self, send) -> None:
        """Produce partial events until the utterance ends, then a final event."""
        import numpy as np
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        try:
            while not self.stop_requested.is_set():
                await asyncio.sleep(0.12)
                if len(self.pcm) < _SAMPLE_RATE * _BYTES_PER_SAMPLE:
                    continue
                if self.analysis_lock.locked():
                    # A previous VAD/partial pass is still running; the next
                    # iteration will analyse fresher audio instead of piling up.
                    continue

                async with self.analysis_lock:
                    samples = np.frombuffer(
                        bytes(self.pcm), dtype="<i2"
                    ).astype(np.float32) / 32768.0

                    tail_len = int(_VAD_TAIL_S * _SAMPLE_RATE)
                    tail_start = max(0, len(samples) - tail_len)
                    tail = samples[tail_start:]
                    speech = await asyncio.to_thread(
                        get_speech_timestamps,
                        tail,
                        vad_options=VadOptions(
                            min_silence_duration_ms=int(_ENDPOINT_SILENCE_S * 1000),
                            speech_pad_ms=0,
                        ),
                        sampling_rate=_SAMPLE_RATE,
                    )

                    if speech:
                        self.saw_speech = True
                        if self.first_speech_sample is None:
                            self.first_speech_sample = tail_start + speech[0]["start"]
                        # Convert the tail-relative end into an absolute sample
                        # index so silence is measured in the audio itself.
                        self.last_speech_sample = tail_start + speech[-1]["end"]

                    # Interim captions: newest recognised words of the tail.
                    model = _get_partial_model()
                    if model is not None and speech:
                        end = min(
                            len(tail), speech[-1]["end"] + int(0.2 * _SAMPLE_RATE)
                        )
                        start = max(0, end - int(_PARTIAL_WINDOW_S * _SAMPLE_RATE))
                        text = await asyncio.to_thread(
                            _transcribe_window, tail[start:end], model
                        )
                        delta = _new_words(self.last_window, text)
                        self.last_window = text
                        if delta:
                            self.emitted = (self.emitted + " " + delta).strip()
                            await send({
                                "type": "partial",
                                "delta": delta,
                                "text": self.emitted,
                            })

                    # Endpointing is entirely server-side; the client only renders.
                    trailing = 0.0
                    if self.last_speech_sample is not None:
                        trailing = max(
                            0.0,
                            (len(samples) - self.last_speech_sample) / _SAMPLE_RATE,
                        )
                    speech_len = 0.0
                    if (
                        self.first_speech_sample is not None
                        and self.last_speech_sample is not None
                    ):
                        speech_len = (
                            self.last_speech_sample - self.first_speech_sample
                        ) / _SAMPLE_RATE
                    if (
                        self.saw_speech
                        and speech_len >= _MIN_SPEECH_S
                        and trailing >= _ENDPOINT_SILENCE_S
                    ):
                        log.info("[STT] %s: endpoint detected", self.session_id)
                        break

            final = await asyncio.to_thread(self._transcribe_final)
            await send({"type": "final", "text": final})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("[STT] %s: streaming failed", self.session_id)
            try:
                await send({"type": "error", "message": str(e)})
            except Exception:
                pass
        finally:
            self.finished.set()

    def _transcribe_final(self) -> str:
        """Accurate full-audio transcription of the whole utterance."""
        if not self.pcm:
            return ""
        text, backend = _transcribe_final_pcm(bytes(self.pcm))
        log.info(
            "[STT] %s: final %d chars via %s",
            self.session_id, len(text), backend,
        )
        return text


def _transcribe_window(window, model) -> str:
    """Transcribe a short float32 16 kHz array for interim captions."""
    path = _write_wav((window * 32767).astype("int16").tobytes())
    try:
        segments, _info = model.transcribe(
            path, language=_language(), beam_size=1, vad_filter=False
        )
        return "".join(s.text for s in segments).strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# WebSocket entry point
# ---------------------------------------------------------------------------

async def stt_stream(websocket: WebSocket, session_id: str, token: str) -> None:
    """Streaming STT WebSocket endpoint.

    HTTP middleware does not run for WebSocket upgrades, so the per-launch
    token is checked here explicitly (WebSocket clients cannot set headers).
    """
    expected = expected_token()
    if expected and not hmac.compare_digest(token, expected):
        await websocket.close(code=4401, reason="Unauthorized")
        return

    await websocket.accept()
    session = StreamingSession(session_id)
    log.info("[STT] %s: stream started", session_id)

    async def send(payload: dict) -> None:
        await websocket.send_json(payload)

    worker = asyncio.create_task(session.run(send))
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                session.add_audio(message["bytes"])
            elif message.get("text"):
                import json

                try:
                    control = json.loads(message["text"])
                except ValueError:
                    continue
                if control.get("type") == "stop":
                    session.request_stop()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("[STT] %s: websocket error: %s", session_id, e)
    finally:
        session.request_stop()
        try:
            await asyncio.wait_for(worker, timeout=90)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            worker.cancel()
        await session.finished.wait()
        log.info("[STT] %s: stream closed", session_id)
