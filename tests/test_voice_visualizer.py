#!/usr/bin/env python
"""Static checks for the cinematic voice-mode core (no frontend build needed)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILED = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS  " if ok else "FAIL  "
    print(f"{status}{name}" + (f"  -> {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def main() -> int:
    voice = (ROOT / "src/app/components/VoiceCore.tsx").read_text(encoding="utf-8")
    visual = (ROOT / "src/app/components/JarvisCoreVisualizer.tsx").read_text(encoding="utf-8")
    capture = (ROOT / "src/app/audioCapture.ts").read_text(encoding="utf-8")

    check("visualizer: old rectangular waveform removed",
          "coreWaveformContainer" not in voice and "WAVE_BARS" not in voice and "wave-bar" not in voice)
    check("visualizer: cinematic canvas component imported",
          'import JarvisCoreVisualizer from "./JarvisCoreVisualizer";' in voice)
    check("visualizer: rendered inside existing core cluster",
          "id=\"coreOrbCluster\"" in voice and "<JarvisCoreVisualizer" in voice)
    check("visualizer: transition geometry preserved",
          "transform: translateY(118px) scale(1.48);" in voice)
    check("visualizer: core + bloom canvases present",
          'id="jarvisCore"' in visual and 'id="bloomLayer"' in visual)
    check("visualizer: audio-reactive analyzer consumed",
          "analyser: AnalyserNode | null" in visual and "getByteFrequencyData" in visual)
    check("capture: exposes analyser to visualizer",
          "onAnalyser?: (analyser: AnalyserNode | null) => void" in capture
          and "callbacks.onAnalyser?.(analyser)" in capture)
    check("capture: clears analyser on cleanup",
          "callbacks.onAnalyser?.(null)" in capture)

    print("\nFAILED:", len(FAILED))
    for name in FAILED:
        print(" -", name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
