"""Rate limit and daily token budget for expensive endpoints (``POST /ask`` calls the LLM).

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


class TokenBudget:
    """LLM tokens (prompt + completion) per client key and UTC day, capped at ``limit`` (0: unlimited).

    With a Redis ``client`` the counters are shared by all API processes (``plujka:tokens:<key>:<day>``,
    kept for two days); without it, or when Redis fails, each process counts on its own.
    """

    def __init__(self, limit: int, client: Any = None, prefix: str = "plujka:tokens") -> None:
        self.limit = limit
        self.client = client
        self.prefix = prefix
        self._local: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _day(now: float | None) -> int:
        return int((time.time() if now is None else now) // 86400)

    def used(self, key: str, now: float | None = None) -> int:
        """Tokens ``key`` used today."""
        day = self._day(now)
        if self.client is not None:
            try:
                return int(self.client.get(f"{self.prefix}:{key}:{day}") or 0)
            except Exception as exc:
                log.warning("redis token budget unavailable, using the in-process count: %s", exc)
        with self._lock:
            return self._local.get((key, day), 0)

    def exceeded(self, key: str, now: float | None = None) -> bool:
        """Whether ``key`` has used up today's budget."""
        return self.limit > 0 and self.used(key, now) >= self.limit

    def add(self, key: str, tokens: int, now: float | None = None) -> None:
        """Count ``tokens`` for ``key`` today."""
        if self.limit <= 0 or tokens <= 0:
            return
        day = self._day(now)
        if self.client is not None:
            try:
                pipe = self.client.pipeline()
                pipe.incrby(f"{self.prefix}:{key}:{day}", tokens)
                pipe.expire(f"{self.prefix}:{key}:{day}", 2 * 86400)
                pipe.execute()
                return
            except Exception as exc:
                log.warning("redis token budget unavailable, using the in-process count: %s", exc)
        with self._lock:
            self._local[(key, day)] = self._local.get((key, day), 0) + tokens
            for old in [k for k in self._local if k[1] < day]:  # keep only today
                del self._local[old]


def _redis_client(redis_url: str) -> Any:
    """Redis client with short timeouts, so a slow Redis cannot stall requests."""
    import redis  # imported only when REDIS_URL is set

    return redis.Redis.from_url(redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)


def make_limiter(limit: int, redis_url: str = "") -> SlidingWindowLimiter | RedisWindowLimiter:
    """Redis-backed limiter when ``redis_url`` is set, in-process otherwise."""
    if not redis_url:
        return SlidingWindowLimiter(limit)
    log.info("rate limit shared through redis")
    return RedisWindowLimiter(_redis_client(redis_url), limit)


def make_token_budget(limit: int, redis_url: str = "") -> TokenBudget:
    """Daily token budget, shared through Redis when ``redis_url`` is set."""
    return TokenBudget(limit, _redis_client(redis_url) if redis_url and limit > 0 else None)
