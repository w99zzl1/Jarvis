import json
import logging
import platform
import re
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

logger = logging.getLogger("history")


def _runtime_context() -> str:
    """Ambient facts the model must never spend a tool call on."""
    now = datetime.now().astimezone()
    offset = now.strftime("%z")
    tz = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local"
    return (
        "[Runtime context — trust this, do NOT call tools for it]\n"
        f"Current local date and time: {now.strftime('%Y-%m-%d %H:%M:%S')} "
        f"({now.strftime('%A')}, {tz})\n"
        f"Platform: {platform.system()} {platform.release()}\n"
        "Time, date and platform are provided here. Use them directly instead of "
        "running shell commands to discover them."
    )


# "I'm from Russia", "I live in Finland", "я из России", "нахожусь в Германии"
_STATED_COUNTRY_RE = re.compile(
    r"(?:"
    r"\b(?:i\s*(?:am|'m)\s*(?:from|in|based in)|i\s*live\s*in|i'm\s*located\s*in)\s+"
    r"|\b(?:я\s+)?(?:нахожусь|живу)\s+в\s+"
    r"|\bя\s+из\s+"
    r"|\b(?:переехал|переехала)\s+в\s+"
    r")(?P<place>[A-ZА-ЯЁ][\w'\-]+(?:\s+[A-ZА-ЯЁ][\w'\-]+){0,2})",
    re.IGNORECASE,
)


def _stated_country(text: str) -> str | None:
    """Pull an explicitly self-reported country out of the user's own words.

    Deliberately narrow: it only fires on a first-person claim of residence or
    origin, never on a mention of a country in passing. The caller treats the
    result as the top-priority location signal.
    """
    if not text:
        return None
    m = _STATED_COUNTRY_RE.search(text)
    if not m:
        return None
    place = (m.group("place") or "").strip(" .,!?")
    if not place:
        return None
    logger.info("[location] user-stated place: %s", place)
    return place


def _location_context(user_text: str = "") -> str:
    """Ambient location. Fails soft: never raises into a turn."""
    try:
        from shared.location import get as _get_location
        return _get_location(user_stated_country=_stated_country(user_text)).describe()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[history] location context unavailable: %s", e)
        return ""


class ContextSource(ABC):
    @abstractmethod
    def retrieve(self, query: str, limit: int = 5) -> list[dict]:
        pass

    @abstractmethod
    def describe(self) -> str:
        pass


class EventLog(ContextSource):
    def __init__(self):
        self._events: list[dict] = []

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def size(self) -> int:
        return len(self._events)

    def add(self, event: dict):
        self._events.append(event)

    def add_user(self, content: str | list):
        self.add({"role": "user", "content": content})
        preview = content[:200] if isinstance(content, str) else "[multimodal]"
        logger.info(f"[USER] {preview}")

    def add_assistant(self, content: str | None, tool_calls: list | None = None):
        entry: dict[str, Any] = {"role": "assistant"}
        if content:
            entry["content"] = content
        if tool_calls:
            entry["tool_calls"] = tool_calls
        self.add(entry)
        if content:
            logger.info(f"[MODEL] {content[:200]}")

    def add_tool_result(self, tool_call_id: str, name: str, content: str,
                        result_status: str = "success", duration: float = 0.0,
                        summary: str | None = None):
        """Record a tool result.

        `summary` is a short digest of `content`. Both are kept in the log - the
        event log is memory, and losing the full output there would make a
        follow-up question unanswerable - but only the summary is replayed to the
        model once the turn that produced it is over (see `ContextBuilder.build`).
        """
        entry = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content,
        }
        if summary:
            entry["summary"] = summary
        self.add(entry)
        logger.info(f"[TOOL RESULT] {name}: {result_status} | {content[:150]}")

    def retrieve(self, query: str, limit: int = 5) -> list[dict]:
        q = query.lower()
        scored = []
        for e in reversed(self._events):
            score = 0
            c = (e.get("content") or "").lower()
            if q in c:
                score += 2
            if q in e.get("name", "").lower():
                score += 1
            if e.get("role") == "tool" and q in e.get("name", "").lower():
                score += 3
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        return [e for _, e in scored[:limit]]

    def describe(self) -> str:
        return f"EventLog({len(self._events)} events)"

    def clear(self):
        self._events.clear()

    def get_recent(self, n: int) -> list[dict]:
        return list(self._events[-n:])


class MemoryContextSource(ContextSource):
    def __init__(self, memory_store):
        self._store = memory_store

    def retrieve(self, query: str, limit: int = 5) -> list[dict]:
        if not query:
            return []
        result_text = self._store.search(query, limit)
        return [{"role": "memory", "content": result_text, "query": query}]

    def describe(self) -> str:
        return "Memory (long-term storage)"


class FileContextSource(ContextSource):
    def retrieve(self, query: str, limit: int = 5) -> list[dict]:
        return []

    def describe(self) -> str:
        return "Files (not yet implemented)"


class ContextBuilder:
    def __init__(self, event_log: EventLog,
                 system_prompt: str,
                 sources: list[ContextSource] | None = None,
                 max_tokens: int = 120000,
                 max_history: int = 1000):
        self._log = event_log
        self._system_prompt = system_prompt
        self._sources = sources or []
        self._max_tokens = max_tokens
        self._max_history = max_history

    def estimate_tokens(self, messages: list[dict]) -> int:
        total = 4 * len(messages)
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                total += len(content) // 3
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            total += len(block.get("text", "")) // 3
                        elif block.get("type") == "image_url":
                            url = block.get("image_url", {}).get("url", "")
                            if url.startswith("data:"):
                                total += 1105
            for tc in m.get("tool_calls") or []:
                func = tc.get("function", {})
                total += len(func.get("name", "")) // 3
                total += len(func.get("arguments", "")) // 3
        return total

    def build(self, query_for_retrieval: str = "", provider: str | None = None) -> list[dict]:
        messages = []
        # The newest user message is what we check for a self-reported country.
        latest_user = ""
        for ev in reversed(self._log.events):
            if ev.get("role") == "user":
                content = ev.get("content")
                if isinstance(content, str):
                    latest_user = content
                break
        system_parts = [self._system_prompt, _runtime_context()]
        loc = _location_context(latest_user or query_for_retrieval)
        if loc:
            system_parts.append(loc)

        # Ambient knowledge, not a tool result: the paths the last disk scan
        # knows about, as a fact. Costs ~60 tokens and saves the model a
        # re-scan (or a wrong guess) when the user says "delete that folder".
        try:
            from shared.scan_cache import LAST_SCAN_REGISTRY
            hint = LAST_SCAN_REGISTRY.describe()
            if hint:
                system_parts.append(hint)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("[history] scan cache hint unavailable: %s", e)

        for src in self._sources:
            system_parts.append(f"[{src.describe()}]")

        if query_for_retrieval and self._sources:
            retrieved = []
            for src in self._sources:
                results = src.retrieve(query_for_retrieval, limit=3)
                retrieved.extend(results)
            if retrieved:
                ctx_lines = ["[Retrieved context from memory]"]
                for r in retrieved:
                    ctx_lines.append(f"  {r.get('content', '')[:200]}")
                system_parts.append("\n".join(ctx_lines))

        system_entry = {"role": "system", "content": "\n\n".join(system_parts)}
        messages.append(system_entry)

        events = self._log.events
        # Keep ALL events - no forced truncation (max_history is just a soft limit)
        # Events are kept in full; only very end trimming if absolutely needed
        # Commented out forced truncation to allow unlimited context:
        # if len(events) > self._max_history:
        #     events = events[-self._max_history:]

        last_user = self._last_user_index(events)
        collapsed = 0
        for idx, event in enumerate(events):
            clean = self._sanitize_for_llm(event, provider)
            if idx < last_user and event.get("role") == "tool":
                # Older than the current turn: the model is not reasoning about
                # this output any more, so replay the digest instead of the
                # payload. A full disk report is ~2k tokens and used to be resent
                # in every request for the rest of the session.
                shrunk = self._collapse_tool_result(event)
                if shrunk != clean.get("content"):
                    clean["content"] = shrunk
                    collapsed += 1
            messages.append(clean)

        used = self.estimate_tokens(messages)
        logger.info(f"[CONTEXT] Built {len(messages)} msgs, ~{used}tok ({used/self._max_tokens*100:.0f}% of {self._max_tokens})")
        if collapsed:
            logger.info(f"[CONTEXT] {collapsed} older tool result(s) sent as summary")
        return messages

    @staticmethod
    def _last_user_index(events: list[dict]) -> int:
        """Index of the newest user event, or -1 when there is none.

        Everything from here on belongs to the turn in flight: the model still
        needs the raw output of the tools it called a moment ago, even across the
        several model round-trips one user request can take.
        """
        for idx in range(len(events) - 1, -1, -1):
            if events[idx].get("role") == "user":
                return idx
        return -1

    @staticmethod
    def _collapse_tool_result(event: dict) -> str:
        """Short stand-in for a tool result from an earlier turn.

        The `summary` a tool supplies is already sized for this job, so it is
        replayed as-is: for the disk scanner that is the heavy-folder tree, which
        is what lets the model answer "which folder should I delete?" several
        turns later. Only results with no summary of their own get truncated.
        """
        content = event.get("content") or ""
        summary = event.get("summary")
        name = event.get("name", "tool")
        if summary:
            return (
                f"[{name} result from an earlier turn - the full output is no "
                f"longer in context; re-run the tool if you need it.]\n"
                f"{summary}"
            )
        if len(content) > 4000:
            # No digest available. Keeping 4000 characters is enough to recognise
            # the result and to see its shape; anything past that only costs
            # tokens on every later request.
            return (
                f"[{name} result from an earlier turn - first 4000 of "
                f"{len(content):,} characters. Re-run the tool if you need it.]\n"
                f"{content[:4000]}"
            )
        return content

    def _compact_old_events(self, old: list[dict]) -> dict | None:
        """Compact old events beyond the recent window.
        
        Keeps user requests, assistant replies, tool calls, and tool results.
        Drops only truly empty entries. No extra LLM call.
        """
        lines = []
        for e in old:
            role = e.get("role")
            if role == "user":
                content = e.get("content", "")
                if isinstance(content, list):
                    content = "[media]"
                elif isinstance(content, str):
                    content = content.strip()
                if content:
                    lines.append(f"User: {content[:500]}")
            elif role == "assistant":
                content = e.get("content")
                if content and isinstance(content, str):
                    lines.append(f"Assistant: {content.strip()[:500]}")
            elif role == "tool":
                # Preserve tool calls and their results
                tool_name = e.get("name", e.get("tool", "unknown"))
                tool_content = e.get("content", "")
                tool_status = e.get("status", "success")
                lines.append(f"Tool {tool_name}: {tool_content[:300]} (status: {tool_status})")
        if not lines:
            return None
        return {
            "role": "system",
            "content": "Earlier in conversation (compressed, tools preserved):\n" + "\n".join(lines),
        }

    def _sanitize_for_llm(self, event: dict, provider: str | None = None) -> dict:
        """Strip extra fields not allowed by OpenAI / Groq spec."""
        role = event.get("role")
        clean = {}

        if role == "system":
            clean["role"] = "system"
            clean["content"] = event.get("content", "")
        elif role == "user":
            clean["role"] = "user"
            content = event.get("content", "")
            clean["content"] = content
        elif role == "assistant":
            clean["role"] = "assistant"
            if event.get("content"):
                clean["content"] = event["content"]
            if event.get("tool_calls"):
                calls = []
                for tc in event["tool_calls"]:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    entry = {
                        "id": tc.get("id", ""),
                        "type": tc.get("type", "function"),
                        "function": {
                            "name": fn.get("name", ""),
                            "arguments": fn.get("arguments", "{}"),
                        },
                    }
                    # Gemini-only field; sending it to OpenAI-shaped APIs is noise
                    # and some providers reject unknown fields. Kept only for
                    # Google, so history replayed to another model never breaks.
                    if fn.get("thought_signature") and (provider is None or provider == "google"):
                        entry["function"]["thought_signature"] = fn["thought_signature"]
                    calls.append(entry)
                if calls:
                    clean["tool_calls"] = calls
        elif role == "tool":
            clean["role"] = "tool"
            clean["tool_call_id"] = event.get("tool_call_id", "")
            clean["content"] = event.get("content", "")

        return clean

    def calc_max_tokens(self, min_response: int = 1024) -> int:
        # With unlimited history, just compute based on current events
        events = self._log.events
        messages = [{"role": "system", "content": self._system_prompt}] + [
            dict(e) for e in events
        ]
        used = self.estimate_tokens(messages)
        # Return large headroom since we have plenty of tokens
        return 500000


class AgentContext:
    """Helper class to manage agent context separately from the builder."""
    def __init__(self, event_log: EventLog, system_prompt: str):
        self._log = event_log
        self._system_prompt = system_prompt
        self._all_events: list[dict] = list(event_log.events) if event_log else []

    def get_recent_events(self, n: int = 100) -> list[dict]:
        """Get recent events - configurable but not forced."""
        return list(self._all_events[-n:])

    def get_all_events(self) -> list[dict]:
        """Return all events in history - for unlimited context."""
        return list(self._all_events)

    def add_event(self, event: dict):
        """Add a new event to history."""
        self._log.add(event)
        self._all_events.append(event)