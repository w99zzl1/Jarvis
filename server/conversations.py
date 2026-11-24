"""Persistent conversation memory.

History used to live only inside a live `Agent` instance, so it vanished the
moment the agent was rebuilt (model switch) or the process restarted. Here the
event log is written to disk keyed by `conversation_id`, so a turn can always be
replayed into a freshly built agent regardless of which model is selected.

The frontend owns the id: it stores it and reuses it until the user starts a new
chat, which is the only thing that resets context.

Schema versioning:
- v1: Original format - list of events or {"events": [...]}
- v2: Added schema_version field, explicit structure
"""

import base64
import json
import logging
import os
import re
import threading
from pathlib import Path

log = logging.getLogger("conversations")

# Current schema version - increment when making breaking changes to conversation format
CONVERSATION_SCHEMA_VERSION = 2

# conversation ids come from the client (uuid or "conv-..."), but never trust
# them as a path component.
_SAFE_ID = re.compile(r"[^0-9A-Za-z_.-]")


def _default_dir() -> Path:
    override = os.environ.get("JARVIS_CONVERSATIONS_DIR")
    if override:
        return Path(override)
    from shared import paths

    return paths.conversations_dir()


def _json_default(obj):
    """Make otherwise-unserialisable event fields storable.

    Gemini tool calls carry a `thought_signature` as raw bytes; base64 keeps it
    intact and `_build_contents` already base64-decodes a string on replay.
    """
    if isinstance(obj, (bytes, bytearray)):
        return base64.b64encode(bytes(obj)).decode("ascii")
    return str(obj)


def _migrate_conversation(data, from_version: int) -> dict:
    """Migrate conversation data from older schema versions to current."""
    if from_version >= CONVERSATION_SCHEMA_VERSION:
        return data

    # v1 files were a bare list of events. Wrap them before treating the data
    # as a mapping; `dict(list_of_events)` raises and used to abort the load.
    if isinstance(data, list):
        return {"events": data, "schema_version": CONVERSATION_SCHEMA_VERSION}

    migrated = dict(data)

    if from_version < 2:
        # v1 -> v2: ensure the explicit structure with a schema_version.
        migrated["schema_version"] = CONVERSATION_SCHEMA_VERSION

    # Future migrations would go here:
    # if from_version < 3:
    #     migrated = _migrate_v2_to_v3(migrated)

    return migrated


def _content_text(content) -> str:
    """Flatten an event's content to plain text.

    User turns are stored as a list of multimodal blocks
    (`[{"type": "text", "text": "..."}]`), so a naive `isinstance(content, str)`
    check used to leave every real conversation titled "Untitled conversation".
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


class ConversationStore:
    def __init__(self, directory: Path | None = None):
        self._dir = Path(directory) if directory else _default_dir()
        self._lock = threading.Lock()

    def _path(self, conversation_id: str) -> Path:
        safe = _SAFE_ID.sub("_", conversation_id)[:200] or "default"
        return self._dir / f"{safe}.json"

    def load(self, conversation_id: str | None) -> list[dict]:
        if not conversation_id:
            return []
        path = self._path(conversation_id)
        with self._lock:
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except FileNotFoundError:
                return []
            except Exception as e:  # pragma: no cover - defensive
                log.warning("[conversations] load failed for %s: %s", conversation_id, e)
                return []

        # Handle both old format (list) and new format (dict with events)
        if isinstance(data, list):
            # v1 format - migrate to v2
            data = _migrate_conversation(data, 1)
            # Save migrated version back
            self.save(conversation_id, data.get("events", []))
            return data.get("events", [])

        if isinstance(data, dict):
            # Check schema version and migrate if needed
            schema_version = data.get("schema_version", 1)
            if schema_version < CONVERSATION_SCHEMA_VERSION:
                data = _migrate_conversation(data, schema_version)
                # Save migrated version
                self.save(conversation_id, data.get("events", []))
            events = data.get("events")
            return events if isinstance(events, list) else []

        return data if isinstance(data, list) else []

    def save(self, conversation_id: str | None, events: list[dict]) -> None:
        if not conversation_id:
            return
        path = self._path(conversation_id)
        payload = {
            "conversation_id": conversation_id,
            "events": events,
            "schema_version": CONVERSATION_SCHEMA_VERSION,
        }
        with self._lock:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".json.tmp")
                with tmp.open("w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, default=_json_default)
                os.replace(tmp, path)
            except Exception as e:  # pragma: no cover - defensive
                log.warning("[conversations] save failed for %s: %s", conversation_id, e)

    def delete(self, conversation_id: str | None) -> None:
        if not conversation_id:
            return
        with self._lock:
            try:
                self._path(conversation_id).unlink()
            except FileNotFoundError:
                pass
            except Exception as e:  # pragma: no cover - defensive
                log.warning("[conversations] delete failed for %s: %s", conversation_id, e)

    def list(self) -> list[dict]:
        """Summaries of every stored conversation, newest first.

        Powers the chat-history drawer: each entry carries the id (to reload it),
        a title derived from the first user turn, a turn count and the last
        modification time.
        """
        summaries: list[dict] = []
        try:
            files = list(self._dir.glob("*.json"))
        except (FileNotFoundError, NotADirectoryError):
            return summaries

        for path in files:
            try:
                with self._lock:
                    with path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
            except Exception:  # pragma: no cover - skip unreadable files
                continue

            events = data.get("events") if isinstance(data, dict) else data
            if not isinstance(events, list):
                continue

            title = ""
            turns = 0
            for e in events:
                if not isinstance(e, dict):
                    continue
                # Retry prompts and other synthetic turns are not part of the
                # conversation the user sees.
                if e.get("internal"):
                    continue
                role = e.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = _content_text(e.get("content")).strip()
                if not text:
                    continue
                turns += 1
                if role == "user" and not title:
                    title = text.splitlines()[0][:80]
            # Do not list empty/synthetic conversations; an "Untitled
            # conversation / 0 сообщ." entry is just noise in the drawer.
            if not title:
                continue

            try:
                updated = path.stat().st_mtime
            except OSError:
                updated = 0.0

            summaries.append(
                {
                    "id": path.stem,
                    "title": title,
                    "turns": turns,
                    "updated": updated,
                }
            )

        summaries.sort(key=lambda s: s["updated"], reverse=True)
        return summaries


store = ConversationStore()
