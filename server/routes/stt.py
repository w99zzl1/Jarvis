"""Speech-to-Text endpoint for Jarvis.

Uses faster-whisper (CTranslate2 implementation of OpenAI Whisper) ? the
production-standard local STT. Runs offline after the first model download,
no API keys, supports Russian and English out of the box.

The model is loaded lazily on the first request so the backend starts fast
even without a speech model on disk.

Audio arrives from the Rust/Tauri side as base64-encoded WebM/Opus chunks
via /stt/stream-chunk. The backend accumulates them per session and
transcribes on /stt/finalize.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import wave
from typing import Optional

from fastapi import UploadFile, File

log = logging.getLogger("server.stt")

# Lazy-loaded Whisper model. None = not loaded yet, False = unavailable.
_whisper_model = None
_WHISPER_MODEL_SIZE = os.environ.get("JARVIS_WHISPER_MODEL", "small")

# In-memory accumulation of audio chunks per session (single-user desktop app,
# so one buffer is sufficient; keyed by session id for future multi-user).
_audio_buffers: dict[str, bytearray] = {}


def _get_whisper_model():
    """Load and cache the Whisper model on first use."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    try:
        from faster_whisper import WhisperModel

        # CPU, int8 quantization for lower RAM usage on the user's machine.
        model = WhisperModel(
            _WHISPER_MODEL_SIZE,
            device="cpu",
            compute_type="int8",
        )
        _whisper_model = model
        log.info(f"[STT] faster-whisper '{_WHISPER_MODEL_SIZE}' loaded (cpu/int8)")
        return model
    except Exception as e:
        log.warning(f"[STT] faster-whisper unavailable: {e}")
        _whisper_model = False
        return None


def _wav_duration_seconds(path: str) -> float | None:
    """Return WAV duration in seconds, or None if unreadable."""
    try:
        with wave.open(path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            return frames / float(rate) if rate else None
    except Exception:
        return None


async def transcribe_audio(file: UploadFile) -> dict:
    """Transcribe an uploaded audio file. Returns {"text": "..."}."""
    model = _get_whisper_model()
    if model is None:
        return {"text": "", "error": "STT unavailable on this machine"}

    # Save uploaded blob to a temp file (Whisper needs a path).
    suffix = os.path.splitext(file.filename or "")[1] or ".wav"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        content = await file.read()
        file_size = len(content)
        log.info(
            f"[STT] received file: {file.filename} size={file_size} bytes suffix={suffix}"
        )
        with open(tmp_path, "wb") as f:
            f.write(content)

        dur = _wav_duration_seconds(tmp_path)
        log.info(f"[STT] audio duration={dur} s")

        # VAD disabled: it was removing all audio when the microphone is quiet.
        segments, info = model.transcribe(
            tmp_path,
            language=None,  # auto-detect
            beam_size=5,
            vad_filter=False,
        )
        text = "".join(segment.text for segment in segments).strip()
        log.info(
            f"[STT] transcribed {len(text)} chars "
            f"(lang={info.language}, prob={getattr(info, 'language_probability', None)})"
        )
        return {"text": text, "language": info.language}
    except Exception as e:
        log.warning(f"[STT] transcription failed: {e}")
        return {"text": "", "error": str(e)}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def stream_chunk(session_id: str, chunk_b64: str) -> dict:
    """Accumulate a base64-encoded audio chunk from the frontend/Rust.

    Called repeatedly while the user is speaking. Chunks are kept in memory
    until /stt/finalize is called.
    """
    import base64

    try:
        raw = base64.b64decode(chunk_b64)
    except Exception as e:
        log.warning(f"[STT] invalid base64 chunk: {e}")
        return {"ok": False, "error": "invalid base64"}

    buf = _audio_buffers.setdefault(session_id, bytearray())
    buf.extend(raw)
    log.info(f"[STT] chunk {len(raw)} bytes, session total {len(buf)} bytes")
    return {"ok": True, "bytes": len(buf)}


async def finalize(session_id: str) -> dict:
    """Transcribe all accumulated chunks for `session_id` and clear the buffer.

    Returns {"text": "..."} or {"text": "", "error": "..."}.
    """
    import base64

    model = _get_whisper_model()
    if model is None:
        return {"text": "", "error": "STT unavailable on this machine"}

    buf = _audio_buffers.pop(session_id, None)
    if not buf or len(buf) == 0:
        return {"text": "", "error": "no audio recorded"}

    # Write accumulated chunks to a temp file for Whisper.
    tmp = tempfile.NamedTemporaryFile(suffix=".webm", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        with open(tmp_path, "wb") as f:
            f.write(bytes(buf))

        file_size = os.path.getsize(tmp_path)
        log.info(f"[STT] finalize: {file_size} bytes accumulated")

        # VAD disabled: it was removing all audio when the microphone is quiet.
        segments, info = model.transcribe(
            tmp_path,
            language=None,  # auto-detect
            beam_size=5,
            vad_filter=False,
        )
        text = "".join(segment.text for segment in segments).strip()
        log.info(
            f"[STT] transcribed {len(text)} chars "
            f"(lang={info.language}, prob={getattr(info, 'language_probability', None)})"
        )
        return {"text": text, "language": info.language}
    except Exception as e:
        log.warning(f"[STT] finalize transcription failed: {e}")
        return {"text": "", "error": str(e)}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
