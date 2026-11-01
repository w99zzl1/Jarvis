"""Speech-to-Text endpoint for Jarvis.

Uses faster-whisper (CTranslate2 implementation of OpenAI Whisper) — the
production-standard local STT. Runs offline after the first model download,
no API keys, supports Russian and English out of the box.

The model is loaded lazily on the first request so the backend starts fast
even without a speech model on disk.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from typing import Optional

from fastapi import UploadFile, File

log = logging.getLogger("server.stt")

# Lazy-loaded Whisper model. None = not loaded yet, False = unavailable.
_whisper_model = None
_WHISPER_MODEL_SIZE = os.environ.get("JARVIS_WHISPER_MODEL", "small")


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
        with open(tmp_path, "wb") as f:
            f.write(content)

        segments, info = model.transcribe(
            tmp_path,
            language=None,  # auto-detect
            beam_size=5,
            vad_filter=True,
        )
        text = "".join(segment.text for segment in segments).strip()
        log.info(f"[STT] transcribed {len(text)} chars (lang={info.language})")
        return {"text": text, "language": info.language}
    except Exception as e:
        log.warning(f"[STT] transcription failed: {e}")
        return {"text": "", "error": str(e)}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
