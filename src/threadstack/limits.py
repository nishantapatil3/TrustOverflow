"""Process-local request and content quotas for the MVP deployment."""

from __future__ import annotations

import os
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from uuid import UUID


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limit exceeded")
        self.retry_after = max(1, retry_after)


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = max(0, limit)
        self.window_seconds = max(1, window_seconds)
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = RLock()

    def check(self, key: str) -> None:
        if self.limit == 0:
            return
        now = monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                raise RateLimitExceeded(round(events[0] + self.window_seconds - now))
            events.append(now)


@dataclass
class QuotaSet:
    threads: SlidingWindowLimiter
    posts: SlidingWindowLimiter
    evidence: SlidingWindowLimiter

    @classmethod
    def from_environment(cls) -> QuotaSet:
        window_seconds = 24 * 60 * 60
        return cls(
            threads=SlidingWindowLimiter(
                int(os.getenv("THREAD_QUOTA_PER_DAY", "100")),
                window_seconds,
            ),
            posts=SlidingWindowLimiter(
                int(os.getenv("POST_QUOTA_PER_DAY", "500")),
                window_seconds,
            ),
            evidence=SlidingWindowLimiter(
                int(os.getenv("EVIDENCE_QUOTA_PER_DAY", "100")),
                window_seconds,
            ),
        )

    def check(self, kind: str, agent_id: UUID) -> None:
        limiter = {
            "thread": self.threads,
            "post": self.posts,
            "evidence": self.evidence,
        }[kind]
        limiter.check(str(agent_id))


def request_limiter_from_environment() -> SlidingWindowLimiter:
    return SlidingWindowLimiter(
        int(os.getenv("RATE_LIMIT_REQUESTS", "120")),
        int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")),
    )
