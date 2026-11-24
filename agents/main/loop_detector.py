"""Loop detection logic for the agent."""

import json
import logging

logger = logging.getLogger("agent.loop_detector")

# Shell commands are deliberately exempt from the "same call repeated" rule:
# a multi-step task legitimately runs several commands in a row. The ABAB /
# ABCABC checks below still catch a genuine loop, because they require the
# *exact same* signatures (and therefore the same commands) to repeat.
_SHELL_TOOLS = {"shell", "run_powershell", "shell_tool"}


class LoopDetector:
    """Detects repetitive tool-calling loops."""

    def __init__(self):
        self.history: list[str] = []

    @staticmethod
    def _signature(tc: dict) -> str:
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"].get("arguments", "{}") or "{}")
        except json.JSONDecodeError:
            args = {}
        return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"

    def detect_loop(self, current: list[dict]) -> bool:
        """Detect repetitive tool-calling loops.

        `history` contains ``name:args_hash`` strings of every tool executed in
        this turn. `current` is the list of tool calls the model just requested.
        They are joined before matching, so a pattern is judged on the sequence
        that would run if this request were accepted — not only on what has
        already run. (Checking history alone used to abort a fresh request just
        because the last few *completed* calls happened to alternate.)
        """
        current_sigs = [self._signature(tc) for tc in current]
        seq = self.history + current_sigs
        if len(seq) < 2:
            return False

        # Same tool+args repeated 4+ times consecutively is a loop — unless it's
        # shell, which is legitimate for multi-step command sequences.
        if len(seq) >= 4:
            last_4 = seq[-4:]
            name = last_4[0].split(":", 1)[0]
            if name not in _SHELL_TOOLS and all(s == last_4[0] for s in last_4):
                logger.warning(f"[LOOP] Same tool+args repeated 4+ times: {name}")
                return True

        # ABAB pattern: two calls alternating.
        if len(seq) >= 4:
            last_4 = seq[-4:]
            if last_4[0] == last_4[2] and last_4[1] == last_4[3] and last_4[0] != last_4[1]:
                logger.warning(f"[LOOP] ABAB pattern detected: {' → '.join(last_4)}")
                return True

        # ABCABC pattern: three calls repeating.
        if len(seq) >= 6:
            last_6 = seq[-6:]
            if last_6[0] == last_6[3] and last_6[1] == last_6[4] and last_6[2] == last_6[5]:
                logger.warning(f"[LOOP] ABCABC pattern detected: {' → '.join(last_6)}")
                return True

        return False

    def add_to_history(self, sigs: list[str]):
        """Add executed signatures to history."""
        self.history.extend(sigs)

    def clear(self):
        """Clear history for new attempt."""
        self.history.clear()

    def get_state(self) -> list[str]:
        return self.history.copy()

    def set_state(self, history: list[str]):
        self.history = history.copy()
