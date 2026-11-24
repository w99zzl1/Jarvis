import json
import logging
import platform
import re
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

logger = logging.getLogger("history")

# A tool result is never shrunk below this many characters when the prompt has
# to be squeezed into a provider's token window: shorter than this and the
# output stops being actionable, so a hard trim of whole events is preferable.
_MIN_TOOL_RESULT_CHARS = 1200


def _runtime_context() -> str:
    """Ambient facts the model must never spend a tool call on."""
    now = datetime.now().astimezone()
    offset = now.strftime("%z")
    tz = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local"
    lines = [
        "[Runtime context — trust this, do NOT call tools for it]",
        # Minute precision is enough for the model and keeps the prompt prefix
        # byte-stable between tool round-trips (prefix caching).
        f"Current local date and time: {now.strftime('%Y-%m-%d %H:%M')} "
        f"({now.strftime('%A')}, {tz})",
        f"Platform: {platform.system()} {platform.release()}",
        "Time, date and platform are provided here. Use them directly instead of "
        "running shell commands to discover them.",
    ]

    # The workspace is a hint, not a constraint: it tells the agent where the
    # user's project usually lives so phrases like "find the bug" have a
    # starting point. It must never be described as an access restriction.
    try:
        from shared.paths import workspace_dir

        workspace = workspace_dir()
    except Exception:
        workspace = None
    if workspace:
        lines.append(
            f"User's current workspace: {workspace}\n"
            "This is a hint for where the project usually is, not an access "
            "restriction — you may read, write and search anywhere the task "
            "requires, using absolute paths when unsure."
        )

    return "\n".join(lines)


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
        # Ambient text is rebuilt once per (user turn, minute) instead of on
        # every model call. A timestamp with second precision changed the very
        # first bytes of the prompt each iteration, which defeats prefix
        # caching on every provider that supports it (and cost a location
        # lookup per tool round-trip).
        self._ambient_key: tuple | None = None
        self._ambient_parts: list[str] = []

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

    def build(self, query_for_retrieval: str = "", provider: str | None = None, aggressive: bool = False,
              token_budget: int | None = None) -> list[dict]:
        # `token_budget` overrides the configured ceiling for this call. The
        # agent lowers it after a provider rejects the request as too large
        # (e.g. Groq free tier: 8k tokens/minute) — the window is a hard limit
        # that our own generous `max_context_tokens` knows nothing about.
        budget = token_budget if token_budget else self._max_tokens
        messages = []
        # The newest user message is what we check for a self-reported country.
        latest_user = ""
        for ev in reversed(self._log.events):
            if ev.get("role") == "user":
                content = ev.get("content")
                if isinstance(content, str):
                    latest_user = content
                break
        # Prefix stability: rebuild ambient facts only when the user turn or
        # the minute changes. A per-second timestamp used to alter the system
        # prompt on every single model call, so no provider could cache the
        # (large, identical) prefix.
        ambient_key = (latest_user or query_for_retrieval, int(time.time() // 60))
        if ambient_key != self._ambient_key:
            parts = [_runtime_context()]
            loc = _location_context(latest_user or query_for_retrieval)
            if loc:
                parts.append(loc)
            # Ambient knowledge, not a tool result: the paths the last disk
            # scan knows about, as a fact. Costs ~60 tokens and saves the model
            # a re-scan (or a wrong guess) when the user says "delete that folder".
            try:
                from shared.scan_cache import LAST_SCAN_REGISTRY
                hint = LAST_SCAN_REGISTRY.describe()
                if hint:
                    parts.append(hint)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("[history] scan cache hint unavailable: %s", e)
            for src in self._sources:
                parts.append(f"[{src.describe()}]")
            self._ambient_parts = parts
            self._ambient_key = ambient_key

        system_parts = [self._system_prompt] + list(self._ambient_parts)
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

        def render(events: list[dict]) -> list[dict]:
            last_user = self._last_user_index(events)
            out = [system_entry]
            for idx, event in enumerate(events):
                clean = self._sanitize_for_llm(event, provider)
                is_tool = event.get("role") == "tool"
                # Aggressive mode (enabled only after repeated rate-limit hits):
                # collapse every tool result, not just those older than the last
                # user turn. Less aggressive by default to keep full context.
                if is_tool and (aggressive or idx < last_user):
                    clean["content"] = self._collapse_tool_result(event)
                out.append(clean)
            return out

        events = self._log.events
        messages = render(events)
        used = self.estimate_tokens(messages)
        logger.info(f"[CONTEXT] Built {len(messages)} msgs, ~{used}tok ({used/budget*100:.0f}% of {budget})")

        # Compaction runs BEFORE any character-level truncation. Crude cutting
        # used to slice through a previous task mid-thought and erase what the
        # agent had already decided; folding whole old turns into a durable
        # system summary keeps that memory instead.
        if used > budget:
            summary, compacted = self._compact_prefix(events)
            if summary:
                dropped = len(events) - len(compacted)
                system_entry["content"] += "\n\n" + summary
                events = compacted
                messages = render(events)
                used = self.estimate_tokens(messages)
                logger.info(
                    "[CONTEXT] Compacted %d old event(s) into system summary: ~%dtok",
                    dropped, used,
                )

        # Over budget: shrink the tool results first. Truncating keeps the
        # transcript valid (every tool_call still has its tool message) and
        # preserves the head of each result, which is where the model's useful
        # data normally is. Dropping whole events — the older behaviour — could
        # orphan a tool_call and get the history rejected.
        if used > budget:
            used = self._truncate_tool_results(messages, budget)
            logger.info(f"[CONTEXT] Truncated tool output to fit budget: ~{used}tok / {budget}")

        # Hard trim: if truncating tool output was not enough, drop whole old
        # units until we fit or run out of things to drop. A unit is an
        # assistant message together with the tool results that answer it:
        # dropping the assistant alone would leave tool messages with no
        # preceding tool_calls, which providers reject as malformed history.
        while used > budget and len(messages) > 2:
            # Never drop the system message (index 0) or a user message.
            drop_start = None
            for i in range(1, len(messages) - 1):
                if messages[i].get("role") != "user":
                    drop_start = i
                    break
            if drop_start is None:
                break
            drop_end = drop_start + 1
            while drop_end < len(messages) and messages[drop_end].get("role") == "tool":
                drop_end += 1
            removed = messages[drop_start:drop_end]
            del messages[drop_start:drop_end]
            removed_chars = sum(
                len(m.get("content") or "") for m in removed if isinstance(m.get("content"), str)
            )
            used = self.estimate_tokens(messages)
            logger.info(
                f"[CONTEXT] Trimmed {len(removed)} old event(s) "
                f"({removed_chars} chars) to fit context window"
            )

        logger.info(f"[CONTEXT] Final {len(messages)} msgs, ~{used}tok ({used/budget*100:.0f}% of {budget})")
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

    def _truncate_tool_results(self, messages: list[dict], budget: int) -> int:
        """Shrink the largest tool results in place until the prompt fits.

        Runs whenever the assembled prompt exceeds the effective budget — our
        own soft ceiling, or the tighter one the agent supplies after a provider
        rejects the request as too large. Returns the new estimated token count.
        Truncating keeps the transcript valid and preserves the head of each
        result, which is where the model's useful data normally is.
        """
        used = self.estimate_tokens(messages)
        if used <= budget:
            return used
        # Candidates: tool results big enough to be worth trimming. `orig` keeps
        # the full text so each pass rebuilds from it; `keepn` is how much of it
        # currently survives. Cutting from the original (not from the already
        # suffixed text) is what makes the loop terminate at the floor.
        cand = [
            i for i, m in enumerate(messages)
            if m.get("role") == "tool"
            and isinstance(m.get("content"), str)
            and len(m["content"]) > _MIN_TOOL_RESULT_CHARS
        ]
        orig = {i: messages[i]["content"] for i in cand}
        keepn = {i: len(orig[i]) for i in cand}

        while used > budget and cand:
            i = max(cand, key=lambda j: keepn[j])
            overage = used - budget
            room = keepn[i] - _MIN_TOOL_RESULT_CHARS
            if room <= 0:
                cand.remove(i)
                continue
            # Cut at least half (so the loop converges even if the estimate is
            # off) but never below the floor, and never more than needed.
            cut = min(room, max(overage * 3, keepn[i] // 2))
            keepn[i] -= cut
            dropped = len(orig[i]) - keepn[i]
            messages[i]["content"] = orig[i][:keepn[i]] + (
                f"\n[... truncated {dropped:,} of {len(orig[i]):,} characters to "
                "fit the model's token budget; re-run the tool for the full "
                "output ...]"
            )
            if keepn[i] <= _MIN_TOOL_RESULT_CHARS:
                cand.remove(i)
            used = self.estimate_tokens(messages)
        return used

    def _compact_prefix(self, events: list[dict], keep_turns: int = 2) -> tuple[str | None, list[dict]]:
        """Fold the oldest completed turns into a summary for the system prompt.

        Returns ``(summary, remaining_events)``. The cut is placed on a user
        message boundary so an assistant `tool_calls` block is never separated
        from the tool results that answer it — that would make the replayed
        transcript invalid for OpenAI-compatible providers.
        """
        last_user = self._last_user_index(events)
        if last_user <= 0:
            return None, events
        user_idxs = [
            i for i, e in enumerate(events[:last_user]) if e.get("role") == "user"
        ]
        if len(user_idxs) <= keep_turns:
            return None, events
        cut = user_idxs[-keep_turns]
        summary_event = self._compact_old_events(events[:cut])
        if not summary_event:
            return None, events
        return summary_event["content"], events[cut:]

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