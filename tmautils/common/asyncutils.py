# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import AsyncIterator, Awaitable, Optional, TypeVar
import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from aiolimiter import AsyncLimiter


_T = TypeVar("_T")

_DEFAULT_KEY = "__default__"


@dataclass
class _KeyState:
    """Per-key state for AsyncRateLimiter."""
    semaphore: asyncio.Semaphore | None = None
    rate_limiter: AsyncLimiter | None = None
    backoff_until: float = 0.0
    active_count: int = 0
    last_access: float = field(default_factory=time.monotonic)


class AsyncRateLimiter:
    """
    Rate limiter with concurrency (semaphore) and/or throughput (token bucket) limits.

    All limits are applied per-key. Pass a key string to `acquire()`, `hold_slot()`,
    or `acquire_token()` for per-key limiting. Pass `None` (or omit) for a shared
    global pool.

    Args:
        max_concurrent:
            Maximum concurrent operations (semaphore) per key.
            If None, no concurrency limit is applied.
        max_rate:
            Maximum operations per time_period (token bucket) per key.
            If None, no throughput limit is applied.
        time_period:
            Time period in seconds for max_rate.
            Default is 1.0 (per second).
        max_idle_seconds:
            Per-key state is lazily cleaned up after this many seconds of inactivity.
            Default is 3600.0 (1 hour).

    Examples:
        ```python
        limiter = AsyncRateLimiter(max_concurrent=5)

        # Global limiting (shared pool, no key)
        async with limiter.acquire():
            await do_something()

        # Per-key limiting
        async with limiter.acquire("my_key"):
            await do_something()

        # Split usage for retry loops (see )
        async with limiter.hold_slot("key"):       # hold semaphore across retries
            for attempt in retries:
                async with limiter.acquire_token("key"):  # token per attempt
                    await do_request()
        ```
    """

    _CLEANUP_EVERY_OPS = 1000

    def __init__(
        self,
        max_concurrent: int | None = None,
        max_rate: float | None = None,
        time_period: float = 1.0,
        max_idle_seconds: float = 3600.0,
    ):
        self._max_concurrent = max_concurrent
        self._max_rate = max_rate
        self._time_period = time_period
        self._max_idle_seconds = max_idle_seconds

        self._keys: dict[str, _KeyState] = {}
        self._ops_since_cleanup = 0

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
        return ", ".join(parts) if parts else "no limits"

    def _get_key_state(self, key: str | None) -> _KeyState:
        effective_key = key if key is not None else _DEFAULT_KEY
        if effective_key not in self._keys:
            self._keys[effective_key] = _KeyState(
                semaphore=(
                    asyncio.Semaphore(self._max_concurrent)
                    if self._max_concurrent else None
                ),
                rate_limiter=(
                    AsyncLimiter(self._max_rate, self._time_period)
                    if self._max_rate else None
                ),
            )
        state = self._keys[effective_key]
        state.last_access = time.monotonic()
        return state

    def signal_backoff(self, key: str | None = None, duration: float = 0) -> None:
        """
        Signal that operations for this key should back off.

        Other tasks calling `acquire_token()` or `acquire()` for the same
        key will sleep until the backoff expires.

        Calling with a shorter duration while a longer backoff is active will not shorten it.

        Args:
            key: Rate limit key. None for the shared global pool.
            duration: Backoff duration in seconds from now.
        """
        if duration <= 0:
            return
        state = self._get_key_state(key)
        deadline = time.monotonic() + duration
        state.backoff_until = max(state.backoff_until, deadline)

    @asynccontextmanager
    async def hold_slot(self, key: str | None = None) -> AsyncIterator[None]:
        """
        Acquire a semaphore slot only.

        Use this to wrap retry loops so the semaphore slot is held across all
        attempts, preventing starvation when retrying after rate limiting.

        Args:
            key: Rate limit key. None for the shared global pool.
        """
        state = self._get_key_state(key)
        state.active_count += 1
        try:
            if state.semaphore:
                async with state.semaphore:
                    yield
            else:
                yield
        finally:
            state.active_count -= 1
            self._maybe_cleanup()

    @asynccontextmanager
    async def acquire_token(self, key: str | None = None) -> AsyncIterator[None]:
        """
        Wait for backoff to expire, then acquire a rate token.

        Use this per-attempt inside a retry loop (nested inside `hold_slot()`).
        Checks for active backoff (set via `signal_backoff()`) and sleeps until
        it expires before acquiring the rate token.

        Args:
            key: Rate limit key. None for the shared global pool.
        """
        state = self._get_key_state(key)
        state.active_count += 1
        try:
            # Recheck loop: handles backoff extensions while sleeping
            while True:
                remaining = state.backoff_until - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            # Acquire rate token
            if state.rate_limiter:
                async with state.rate_limiter:
                    yield
            else:
                yield
        finally:
            state.active_count -= 1
            self._maybe_cleanup()

    @asynccontextmanager
    async def acquire(self, key: str | None = None) -> AsyncIterator[None]:
        """
        Acquire both semaphore slot and rate token.

        Convenience wrapper combining `hold_slot()` and `acquire_token()`.
        Suitable for simple operations that don't need split retry semantics.

        Args:
            key: Rate limit key. None for the shared global pool.
        """
        async with self.hold_slot(key):
            async with self.acquire_token(key):
                yield

    def cleanup(self) -> None:
        """
        Remove per-key state for idle keys not accessed recently.

        Only evicts keys with `active_count == 0`. Safe to call at any time.
        Called automatically every `_CLEANUP_EVERY_OPS` operations.
        """
        now = time.monotonic()
        to_remove = [
            k for k, s in self._keys.items()
            if s.active_count == 0
            and now - s.last_access > self._max_idle_seconds
        ]
        for k in to_remove:
            del self._keys[k]

    def _maybe_cleanup(self) -> None:
        self._ops_since_cleanup += 1
        if self._ops_since_cleanup >= self._CLEANUP_EVERY_OPS:
            self._ops_since_cleanup = 0
            self.cleanup()


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
