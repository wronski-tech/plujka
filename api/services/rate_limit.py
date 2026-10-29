"""Rate limit for expensive endpoints (``POST /ask`` calls the LLM).

The limit is per client key (API key or IP address). Without ``REDIS_URL`` each API process counts
on its own (sliding window in memory), so N workers allow up to N times the limit. With
``REDIS_URL`` all processes share one fixed one-minute window per key in Redis. When Redis is
unreachable the process falls back to its own in-memory window and logs a warning, so an outage
of Redis never blocks questions.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any

log = logging.getLogger(__name__)


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


class RedisWindowLimiter:
    """Shared limit across processes: one counter per key and window in Redis (INCR + EXPIRE)."""

    def __init__(self, client: Any, limit: int, window_seconds: int = 60, prefix: str = "plujka:ratelimit") -> None:
        self.client = client
        self.limit = limit
        self.window = window_seconds
        self.prefix = prefix
        self.fallback = SlidingWindowLimiter(limit, window_seconds)

    def allow(self, key: str, now: float | None = None) -> bool:
        """Count an event for ``key`` in the current window; use the local window if Redis fails."""
        if self.limit <= 0:
            return True
        now = time.time() if now is None else now
        bucket = f"{self.prefix}:{key}:{int(now // self.window)}"
        try:
            pipe = self.client.pipeline()
            pipe.incr(bucket)
            pipe.expire(bucket, self.window * 2)
            count = pipe.execute()[0]
        except Exception as exc:
            log.warning("redis rate limit unavailable, using the in-process limit: %s", exc)
            return self.fallback.allow(key)
        return int(count) <= self.limit


def make_limiter(limit: int, redis_url: str = "") -> SlidingWindowLimiter | RedisWindowLimiter:
    """Redis-backed limiter when ``redis_url`` is set, in-process otherwise."""
    if not redis_url:
        return SlidingWindowLimiter(limit)
    import redis  # imported only when REDIS_URL is set

    log.info("rate limit shared through redis")
    return RedisWindowLimiter(redis.Redis.from_url(redis_url, socket_timeout=0.5, socket_connect_timeout=0.5), limit)
