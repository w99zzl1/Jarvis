"""Cooperative cancellation for in-flight agent turns.

The UI's Stop button POSTs `/agent/stop` with the conversation id. That has to
actually stop the agent thread, not merely abandon the SSE stream: otherwise the
model keeps calling tools (and spending provider quota) after the user asked it
to stop, and a second turn on the same conversation races the first to persist.

Cancellation is *cooperative*. A turn holds a `CancelToken`; the agent loop
checks it between iterations, between streamed deltas, and before each tool
call, and unwinds with `AgentCancelled`. A tool that is already inside a blocking
syscall (a shell command) cannot be interrupted from here - the shell tools poll
the same token and kill their child process, and everything else unwinds on the
next check.

Nothing in this module imports the agents or the server, so both layers can use
it without a cycle.
"""
from __future__ import annotations

import threading
import time


class AgentCancelled(Exception):
    """Raised inside the agent thread when the current turn is cancelled."""


class CancelToken:
    """One turn's cancellation flag. Cheap to poll, safe across threads."""

    def __init__(self, conversation_id: str = ""):
        self.conversation_id = conversation_id
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        """Raise `AgentCancelled` if this turn has been cancelled."""
        if self._event.is_set():
            raise AgentCancelled()


class CancellationRegistry:
    """Conversation id -> the token of the turn currently running for it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tokens: dict[str, CancelToken] = {}

    def register(self, conversation_id: str) -> CancelToken:
        """Start a turn. Any earlier turn for the same conversation is cancelled.

        A new message on a conversation that is still running means the old turn
        is stale: its result would race the new one to overwrite the saved
        transcript, and its tools would keep mutating disk. Cancelling it here
        makes "send while running" behave like Stop followed by Send.
        """
        token = CancelToken(conversation_id)
        with self._lock:
            previous = self._tokens.get(conversation_id)
            self._tokens[conversation_id] = token
        if previous is not None:
            previous.cancel()
        return token

    def cancel(self, conversation_id: str) -> bool:
        """Cancel the running turn for this conversation. False if none is running."""
        with self._lock:
            token = self._tokens.get(conversation_id)
        if token is None:
            return False
        token.cancel()
        return True

    def unregister(self, conversation_id: str, token: CancelToken | None = None) -> None:
        """Drop a finished turn, unless a newer one has already replaced it."""
        with self._lock:
            current = self._tokens.get(conversation_id)
            if current is not None and (token is None or current is token):
                del self._tokens[conversation_id]

    def cancel_all(self) -> int:
        with self._lock:
            tokens = list(self._tokens.values())
            self._tokens.clear()
        for token in tokens:
            token.cancel()
        return len(tokens)


# Process-wide registry. Routes register a turn, the Stop endpoint and the SSE
# transport cancel it, the agent loop and shell tools poll it.
registry = CancellationRegistry()


def sleep_interruptible(seconds: float, token: CancelToken | None = None,
                        step: float = 0.25) -> None:
    """Sleep, but wake immediately and raise if the turn is cancelled.

    Rate-limit waits can be minutes long; a plain `time.sleep` would keep the
    turn alive long after the user pressed Stop.
    """
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    while True:
        if token is not None:
            token.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(step, remaining))
