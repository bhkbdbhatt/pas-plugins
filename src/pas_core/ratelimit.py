"""Per-tenant rate limiting and concurrency guards.

Token bucket, evaluated locally when Redis is absent.  Keys are always namespaced
by tenant so one carrier can never exhaust another's budget.  Two dimensions are
enforced because both matter commercially:

* **rate** - requests per second / per day (partner API tiers, MCP session caps)
* **concurrency** - in-flight workflows and long-running calculations
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pas_core.errors import RateLimitedError


@dataclass(slots=True)
class _Bucket:
    """A single token bucket."""

    capacity: float
    refill_rate: float
    tokens: float
    updated_at: float

    def consume(self, amount: float = 1.0) -> tuple[bool, float]:
        now = time.monotonic()
        elapsed = now - self.updated_at
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.updated_at = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True, 0.0
        deficit = amount - self.tokens
        return False, deficit / self.refill_rate if self.refill_rate > 0 else float("inf")


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """A limit expressed per subject scope."""

    name: str
    requests_per_second: float
    burst: int = 0

    @property
    def capacity(self) -> float:
        return float(self.burst or max(1, round(self.requests_per_second)))


class InMemoryRateLimiter:
    """Process-local limiter. Correct for a single replica and for tests."""

    def __init__(self, policies: dict[str, RateLimitPolicy] | None = None) -> None:
        self._policies: dict[str, RateLimitPolicy] = dict(policies or {})
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    def register(self, policy: RateLimitPolicy) -> None:
        self._policies[policy.name] = policy

    async def check(self, key: str, policy_name: str, *, cost: float = 1.0) -> None:
        """Raise :class:`RateLimitedError` when the budget is exhausted."""
        policy = self._policies.get(policy_name)
        if policy is None:
            return
        async with self._lock:
            now = time.monotonic()
            bucket = self._buckets.get(key)
            if bucket is None or bucket.capacity != policy.capacity:
                bucket = _Bucket(policy.capacity, policy.requests_per_second, policy.capacity, now)
                self._buckets[key] = bucket
            allowed, retry_after = bucket.consume(cost)
        if not allowed:
            raise RateLimitedError(
                retry_after=round(retry_after, 3),
                limit=int(policy.requests_per_second),
                scope=f"{policy_name}:{key}",
            )

    def remaining(self, key: str, policy_name: str) -> int:
        policy = self._policies.get(policy_name)
        bucket = self._buckets.get(key)
        if policy is None or bucket is None:
            return 0
        return max(0, int(bucket.tokens))

    def reset(self) -> None:
        self._buckets.clear()


class RedisRateLimiter:
    """Distributed limiter using an atomic Lua script for decrement-or-reject."""

    SCRIPT = """
    local key = KEYS[1]
    local capacity = tonumber(ARGV[1])
    local refill = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    local cost = tonumber(ARGV[4])
    local state = redis.call('HMGET', key, 'tokens', 'ts')
    local tokens = tonumber(state[1]) or capacity
    local ts = tonumber(state[2]) or now
    tokens = math.min(capacity, tokens + (now - ts) * refill / 1000.0)
    local allowed = 0
    local retry = 0
    if tokens >= cost then
      tokens = tokens - cost
      allowed = 1
    else
      retry = (cost - tokens) / (refill / 1000.0)
    end
    redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
    redis.call('PEXPIRE', key, 10000)
    return {allowed, tostring(retry)}
    """

    def __init__(self, client: Any, policies: dict[str, RateLimitPolicy] | None = None) -> None:
        self._redis = client
        self._policies: dict[str, RateLimitPolicy] = dict(policies or {})

    def register(self, policy: RateLimitPolicy) -> None:
        self._policies[policy.name] = policy

    async def check(self, key: str, policy_name: str, *, cost: float = 1.0) -> None:
        policy = self._policies.get(policy_name)
        if policy is None:
            return
        now_ms = int(time.time() * 1000)
        result = await self._redis.eval(
            self.SCRIPT,
            1,
            key,
            policy.capacity,
            policy.requests_per_second,
            now_ms,
            cost,
        )
        allowed, retry_after = int(result[0]), float(result[1])
        if not allowed:
            raise RateLimitedError(
                retry_after=round(retry_after, 3),
                limit=int(policy.requests_per_second),
                scope=f"{policy_name}:{key}",
            )

    async def remaining(self, key: str, policy_name: str) -> int:
        policy = self._policies.get(policy_name)
        if policy is None:
            return 0
        state = await self._redis.hmget(key, "tokens")
        return int(float(state[0] or 0)) if state else 0


RateLimiter = InMemoryRateLimiter | RedisRateLimiter


DEFAULT_POLICIES: dict[str, RateLimitPolicy] = {
    # partner-facing quote/bind (plugin 5)
    "embed-quote": RateLimitPolicy("embed-quote", requests_per_second=50, burst=100),
    "embed-bind": RateLimitPolicy("embed-bind", requests_per_second=10, burst=20),
    # AI agent traffic through the gateway / MCP (plugin 1)
    "mcp-tool": RateLimitPolicy("mcp-tool", requests_per_second=10, burst=30),
    # underwriting decisions (plugin 3)
    "uw-decision": RateLimitPolicy("uw-decision", requests_per_second=25, burst=50),
    # IFRS 17 valuation is expensive: keep it deliberately scarce
    "ifrs17-valuation": RateLimitPolicy("ifrs17-valuation", requests_per_second=2, burst=4),
    # batch data exports (plugin 6)
    "data-export": RateLimitPolicy("data-export", requests_per_second=1, burst=2),
    # default tenant budget
    "tenant-default": RateLimitPolicy("tenant-default", requests_per_second=50, burst=100),
}


class ConcurrencyLimiter:
    """Caps simultaneous long-running work (valuations, batch scores, exports)."""

    def __init__(self, limit: int = 25) -> None:
        self._semaphore = asyncio.Semaphore(limit)
        self._limit = limit
        self._active = 0
        self._peak = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def peak(self) -> int:
        return self._peak

    async def __aenter__(self) -> ConcurrencyLimiter:
        await self._semaphore.acquire()
        self._active += 1
        self._peak = max(self._peak, self._active)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._active -= 1
        self._semaphore.release()


class SlidingWindowCounter:
    """Daily quota tracker (billing tier enforcement, e.g. per-GB ingestion)."""

    def __init__(self, window_seconds: float = 86_400) -> None:
        self._window = window_seconds
        self._events: dict[str, list[float]] = {}

    def increment(self, key: str, amount: float = 1.0, *, now: float | None = None) -> float:
        ts = now if now is not None else time.time()
        bucket = self._events.setdefault(key, [])
        cutoff = ts - self._window
        if bucket and bucket[0] < cutoff:
            bucket.clear()
        bucket.append(ts)
        self._events[key] = bucket
        return amount

    def total(self, key: str, *, now: float | None = None) -> float:
        ts = now if now is not None else time.time()
        cutoff = ts - self._window
        return float(sum(1 for t in self._events.get(key, []) if t >= cutoff))

    def would_exceed(self, key: str, limit: float, amount: float = 1.0) -> bool:
        return self.total(key) + amount > limit


def build_limiter(
    backend: str,
    policies: dict[str, RateLimitPolicy] | None = None,
    *,
    redis_factory: Callable[[], Any] | None = None,
) -> RateLimiter:
    """Select the limiter implementation matching the configured backend."""
    merged = dict(DEFAULT_POLICIES)
    merged.update(policies or {})
    if backend == "redis" and redis_factory is not None:
        return RedisRateLimiter(redis_factory(), merged)
    return InMemoryRateLimiter(merged)
