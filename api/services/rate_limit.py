"""In-process sliding-window rate limit for expensive endpoints (``POST /ask`` calls the LLM).

The limit is per client key (IP address) and per API process. Behind several workers or replicas
each process counts separately; put a shared limiter (reverse proxy, Redis) in front for strict
global limits.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """Allow at most ``limit`` events per ``window_seconds`` for each key."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        """Record an event for ``key`` and return False when it exceeds the limit (limit <= 0: always True)."""
        if self.limit <= 0:
            return True
        now = time.monotonic() if now is None else now
        with self._lock:
            events = self._events[key]
            while events and events[0] <= now - self.window:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            if len(self._events) > 10_000:  # drop idle keys so memory stays bounded
                for idle in [k for k, v in self._events.items() if not v or v[-1] <= now - self.window]:
                    del self._events[idle]
            return True
