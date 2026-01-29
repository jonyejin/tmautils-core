# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import AsyncIterator, Awaitable, Optional, TypeVar
import asyncio
from contextlib import asynccontextmanager
from enum import StrEnum

from aiolimiter import AsyncLimiter


_T = TypeVar("_T")


class RateLimitScope(StrEnum):
    """Scope for rate limiting."""
    GLOBAL = "global"
    PER_KEY = "per_key"


class AsyncRateLimiter:
    """
    Rate limiter with concurrency (semaphore) and/or throughput (token bucket) limits.

    Limits can be applied globally or per-key. Use `acquire(key)` to acquire limits
    before performing rate-limited operations.

    Args:
        max_concurrent:
            Maximum concurrent operations (semaphore).
            If None, no concurrency limit is applied.
            Default is None.
        max_rate:
            Maximum operations per time_period (token bucket).
            If None, no throughput limit is applied.
            Default is None.
        time_period:
            Time period in seconds for max_rate.
            Default is 1.0 (per second).
        scope:
            Whether limits apply globally or per-key.

    Examples:
        ```python
        # No more than 5 concurrent operations
        limiter = AsyncRateLimiter(max_concurrent=5)

        # No more than 60 ops/minute
        limiter = AsyncRateLimiter(max_rate=60, time_period=60)

        # Per-key: No more than 2 ops/second per key
        limiter = AsyncRateLimiter(max_rate=2, scope=RateLimitScope.PER_KEY)

        # Usage
        async with limiter.acquire("my_key"):
            await do_something()
        ```
    """

    def __init__(
        self,
        max_concurrent: int | None = None,
        max_rate: float | None = None,
        time_period: float = 1.0,
        scope: RateLimitScope = RateLimitScope.GLOBAL,
    ):
        self._max_concurrent = max_concurrent
        self._max_rate = max_rate
        self._time_period = time_period
        self._scope = scope

        # Keyed limiters (key = "__global__" or user-provided key)
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._rate_limiters: dict[str, AsyncLimiter] = {}

    @property
    def config_string(self) -> str:
        """String representation of the rate limiter configuration."""
        parts = []
        if self._max_concurrent is not None:
            parts.append(f"max_concurrent={self._max_concurrent}")
        if self._max_rate is not None:
            parts.append(
                f"max_rate={self._max_rate}/{self._time_period}s"
            )
        parts.append(f"scope={self._scope.value}")
        return ", ".join(parts)

    def _get_effective_key(self, key: str | None) -> str:
        """Get effective key based on scope and provided key."""
        if self._scope == RateLimitScope.GLOBAL:
            return "__global__"
        return key or "__global__"

    def _get_semaphore(self, key: str) -> asyncio.Semaphore | None:
        if self._max_concurrent is None:
            return None
        if key not in self._semaphores:
            self._semaphores[key] = asyncio.Semaphore(self._max_concurrent)
        return self._semaphores[key]

    def _get_rate_limiter(self, key: str) -> AsyncLimiter | None:
        if self._max_rate is None:
            return None
        if key not in self._rate_limiters:
            self._rate_limiters[key] = AsyncLimiter(
                self._max_rate,
                self._time_period,
            )
        return self._rate_limiters[key]

    @asynccontextmanager
    async def acquire(self, key: str | None = None) -> AsyncIterator[None]:
        """
        Acquire rate limits.

        Use once per operation to ensure limits are released during backoff waits.

        Args:
            key:
                Key for per-key limiting. If None, uses global key.
                Ignored when scope=GLOBAL (all operations share same limits).

        Yields:
            None: Context manager for holding the limits.
        """
        effective_key = self._get_effective_key(key)
        sem = self._get_semaphore(effective_key)
        rate = self._get_rate_limiter(effective_key)

        match (sem, rate):
            case (s, r) if s and r:
                async with s:
                    async with r:
                        yield
            case (s, None) if s:
                async with s:
                    yield
            case (None, r) if r:
                async with r:
                    yield
            case _:
                yield


def run_coro_sync(
    coro: Awaitable[_T],
    *,
    timeout: Optional[float] = None,
) -> _T:
    """
    Run a coroutine from synchronous code.

    This function is for calling async code from sync contexts (e.g., scripts, sync functions).
    If called from within an existing event loop (e.g., Jupyter notebook), it raises an error.

    Args:
        coro (Awaitable):
            The coroutine to run.

        timeout (Optional[float]):
            Optional timeout in seconds to wait for the coroutine to complete.

    Returns:
        The result of the coroutine.

    Raises:
        RuntimeError: If called from within a running event loop.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # No loop running - expected case
    else:
        raise RuntimeError(
            "run_coro_sync() cannot be called from async code. "
            "Use 'await' on the async method instead. "
            "If using Jupyter, consider using 'nest_asyncio' or similar tools."
        )

    if timeout is not None:
        # Wrap with timeout
        coro = asyncio.wait_for(coro, timeout)

    return asyncio.run(coro)
