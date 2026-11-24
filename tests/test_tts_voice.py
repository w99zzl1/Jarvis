#!/usr/bin/env python
"""Non-build checks for Silero TTS in main-agent voice mode.

Run:
    python tests/test_tts_voice.py

No Silero model download and no frontend build are needed: the synthesis path is
exercised with a tiny fake model that returns a NumPy array.
"""

import base64
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.TemporaryDirectory()
os.environ["JARVIS_DATA_DIR"] = _TMP.name

from server.routes import chat  # noqa: E402
from shared.config_loader import (  # noqa: E402
    DEFAULT_TTS_CONFIG,
    get_tts_config,
    save_app_config,
)

FAILED = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS  " if ok else "FAIL  "
    print(f"{status}{name}" + (f"  -> {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


class FakeEngine:
    def apply_tts(self, **kwargs):
        import numpy as np

        return np.zeros(128, dtype=np.float32)


class FakeTTSModel:
    def __init__(self):
        self.tts_model = FakeEngine()
        self.speaker = "aidar"
        self.sample_rate = 48000
        self.put_accent = True
        self.put_yo = True


def use_config(config):
    chat._get_tts_config = lambda: config


def test_default_config():
    cfg = get_tts_config()
    check("tts config: default provider is silero", cfg.get("provider") == "silero")
    check("tts config: enabled by default", cfg.get("enabled") is True)
    check("tts config: defaults complete", all(k in cfg for k in DEFAULT_TTS_CONFIG))


def test_legacy_config_gets_provider():
    save_app_config({"tts": {"model_id": "v3_en", "speaker": "en_0"}})
    cfg = get_tts_config()
    check("tts config: legacy save keeps silero provider", cfg.get("provider") == "silero")
    check("tts config: legacy save keeps saved voice", cfg.get("speaker") == "en_0")


def test_tts_text_flattens_markdown():
    plain = chat._tts_text("# Заголовок\n\n**Жирный** и `код`\n\n- пункт")
    check("tts text: headings removed", "Заголовок" in plain and "#" not in plain)
    check("tts text: markdown markers removed", "**" not in plain and "`" not in plain)
    check("tts text: list marker removed", "пункт" in plain and "- пункт" not in plain)


def test_voice_mode_gate():
    original = chat._get_tts_config
    try:
        use_config({"provider": "silero", "enabled": True})
        model = FakeTTSModel()
        chat._get_tts_model = lambda config=None: model
        check("tts gate: text mode is silent", chat._synthesize_tts("привет", False) is None)
        check("tts gate: voice mode synthesizes", bool(chat._synthesize_tts("привет", True)))
    finally:
        chat._get_tts_config = original


def test_disabled_and_unknown_provider():
    original = chat._get_tts_config
    try:
        use_config({"provider": "silero", "enabled": False})
        check("tts config: disabled engine is silent", chat._synthesize_tts("привет", True) is None)
        use_config({"provider": "other", "enabled": True})
        check("tts config: unknown provider is silent", chat._synthesize_tts("привет", True) is None)
    finally:
        chat._get_tts_config = original


def test_fake_synthesis_returns_wav():
    original_config = chat._get_tts_config
    original_model = chat._get_tts_model
    try:
        use_config({"provider": "silero", "enabled": True})
        chat._get_tts_model = lambda config=None: FakeTTSModel()
        audio = chat._synthesize_tts("Привет, Джарвис.", True)
        check("tts synthesis: audio returned", bool(audio))
        if audio:
            raw = base64.b64decode(audio)
            check("tts synthesis: WAV header present", raw[:4] == b"RIFF" and raw[8:12] == b"WAVE")
    finally:
        chat._get_tts_config = original_config
        chat._get_tts_model = original_model


def test_explicit_contract_in_source():
    api = (ROOT / "src" / "app" / "api.ts").read_text(encoding="utf-8")
    hook = (ROOT / "src" / "app" / "useChat.ts").read_text(encoding="utf-8")
    voice = (ROOT / "src" / "app" / "components" / "VoiceCore.tsx").read_text(encoding="utf-8")
    backend = (ROOT / "server" / "main.py").read_text(encoding="utf-8")
    code_route = (ROOT / "server" / "routes" / "code_agent.py").read_text(encoding="utf-8")

    check("contract: frontend sends synthesize_speech", "synthesize_speech" in api and "synthesize_speech" in hook)
    check("contract: voice mode requests speech", 'mode === "voice"' in voice and ', true)' in voice)
    check("contract: backend accepts synthesize_speech", "synthesize_speech: bool = False" in backend)
    check("contract: code agent stays silent", '"audio": None' in code_route)


def main():
    test_default_config()
    test_legacy_config_gets_provider()
    test_tts_text_flattens_markdown()
    test_voice_mode_gate()
    test_disabled_and_unknown_provider()
    test_fake_synthesis_returns_wav()
    test_explicit_contract_in_source()
    print("\nFAILED:", len(FAILED))
    for name in FAILED:
        print(" -", name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
