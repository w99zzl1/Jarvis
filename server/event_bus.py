"""A bounded, non-blocking hand-off between the agent thread and the SSE loop.

Why this exists: progress samples come from the native scanner's worker threads.
Those threads hold the GIL while calling back into Python, so *anything* that
blocks them stalls the walk itself. The old hand-off was an unbounded
`queue.Queue` plus `run_in_executor(None, q.get)`, which has three failure modes
on a slow or vanished client:

* `q.put` never blocks, so a client that stopped reading grows the queue without
  limit - the scan keeps walking for nobody.
* `run_in_executor` parks a thread-pool worker in `q.get` forever when the
  connection dies mid-stream; a few of those and every later stream starves,
  because they all share the default executor.
* Nothing tells the producer the client is gone, so a 29-second scan runs to
  completion against a closed socket.

This bus fixes all three: bounded capacity, drop-oldest for *lossy* events
(progress ticks, stream deltas - a newer sample makes an older one worthless),
never dropped for *terminal* events (tool start/finish, final, errors), and a
`close()` flag the producer checks so work stops early when the UI is gone.

Future work will move this stream onto native Tauri events emitted straight from
Rust. The producer side does not care which transport it writes to, so that swap
touches the routes and the frontend only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator

log = logging.getLogger("server.events")

# Types that are safe to lose: a newer event of the same kind supersedes them.
LOSSY = frozenset({"tool_progress", "stream", "token", "thought"})

# Enough for a second of progress ticks with room to spare. A 29s scan at ~8
# samples/s is ~230 events; 512 never trips on a healthy client.
DEFAULT_CAPACITY = 512

# Seconds of silence after which the SSE generator emits a comment frame, so
# proxies and EventSource do not treat the gap between two samples as a dead
# connection.
HEARTBEAT_SECONDS = 8.0

# Consumer poll interval. Small enough that a sample is not visibly late, large
# enough not to spin a core during a 29-second scan.
POLL_SECONDS = 0.01


class EventBus:
    """Thread-safe bounded queue with a lossy/terminal split."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        self._capacity = max(8, capacity)
        self._items: deque[dict] = deque()
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._closed = False
        self.dropped = 0
        self.overflow = 0
        self.pushed = 0

    # -- producer side (agent thread, native callback threads) ----------------

    def push(self, event: dict) -> bool:
        """Hand an event over. Never blocks; returns False once closed."""
        if not isinstance(event, dict):
            return False
        etype = event.get("type")
        with self._lock:
            if self._closed:
                return False
            self.pushed += 1
            if len(self._items) >= self._capacity:
                # Shed a lossy event, never a terminal one: dropping the oldest
                # item outright would throw away the tool_started that tells the
                # UI a scan is even running.
                if not self._drop_oldest_lossy():
                    # Nothing lossy to shed - all terminal events. Let the queue
                    # grow rather than lose one; it is bounded by the number of
                    # tool calls in a turn.
                    self.overflow += 1
                    log.warning("[events] bus full of terminal events (%d)", len(self._items))
            self._items.append(event)
            self._not_empty.notify()
            return True

    def _drop_oldest_lossy(self) -> bool:
        for idx, queued in enumerate(self._items):
            if queued.get("type") in LOSSY:
                del self._items[idx]
                self.dropped += 1
                return True
        return False

    def close(self) -> None:
        """Tell the producer nobody is listening any more."""
        with self._lock:
            self._closed = True
            self._not_empty.notify_all()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def should_stop(self) -> bool:
        """Cheap check for the producer loop: stop working for a dead client."""
        return self.closed

    # -- consumer side (asyncio loop) -----------------------------------------

    def drain(self) -> list[dict]:
        """Take everything queued, oldest first. Cheap and non-blocking."""
        with self._lock:
            out = list(self._items)
            self._items.clear()
            return out

    def wait(self, timeout: float = POLL_SECONDS) -> list[dict]:
        """Like `drain`, but parks briefly so the loop is not a busy-wait."""
        with self._not_empty:
            if not self._items:
                self._not_empty.wait(timeout)
            out = list(self._items)
            self._items.clear()
            return out

    def stats(self) -> dict:
        with self._lock:
            return {
                "pushed": self.pushed,
                "dropped": self.dropped,
                "overflow": self.overflow,
                "queued": len(self._items),
                "capacity": self._capacity,
                "closed": self._closed,
            }


def heartbeat_elapsed(last_event_at: float, now: float | None = None) -> bool:
    """True when the stream has been quiet long enough to need a comment."""
    now = time.monotonic() if now is None else now
    return (now - last_event_at) >= HEARTBEAT_SECONDS


# Pushed by the producer's `finally` so the consumer wakes even if the agent
# died without producing a final event.
DONE = "__done__"


async def sse_stream(
    bus: EventBus,
    mapper: Callable[[dict], dict | None],
    poll: float = POLL_SECONDS,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> Iterator[str]:
    """Turn a bus into an SSE body.

    Polls the bus on the event loop instead of parking an executor thread in
    `get()`: a client that vanished mid-scan used to leave one thread-pool
    worker blocked forever per stream, and they all share the default executor.
    A worker is now held for at most `poll`, so a disconnect costs nothing.

    `mapper` turns an internal event into an SSE payload, or None to skip it.
    `final` is passed through untouched - it is the response body the frontend
    resolves the request with.
    """
    loop = asyncio.get_running_loop()
    last_beat = time.monotonic()
    try:
        while True:
            for event in await loop.run_in_executor(None, bus.wait, poll):
                etype = event.get("type")
                if etype == DONE:
                    return
                if etype == "final":
                    yield f"data: {json.dumps(event)}\n\n"
                    return
                sse = mapper(event)
                if sse:
                    last_beat = time.monotonic()
                    yield f"data: {json.dumps(sse)}\n\n"
            if time.monotonic() - last_beat >= heartbeat:
                # A comment frame keeps proxies and EventSource from calling the
                # connection dead during a 29-second silent scan.
                last_beat = time.monotonic()
                yield ": keepalive\n\n"
    except asyncio.CancelledError:
        log.info("[events] client gone, bus=%s", bus.stats())
        raise
    finally:
        # Tell the agent thread to stop working for a socket nobody reads.
        bus.close()
        log.info("[events] stream finished, bus=%s", bus.stats())
