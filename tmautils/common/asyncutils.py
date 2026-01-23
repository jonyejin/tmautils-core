# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import Any, AsyncIterator, Awaitable, Callable, Optional, TypeVar
import asyncio
import concurrent.futures
from contextlib import asynccontextmanager
import contextvars
from enum import StrEnum
import multiprocessing as mp

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
    ex: Optional[concurrent.futures.Executor] = None,
) -> _T:
    """
    Run a coroutine from synchronous code, even if already on an event loop.
    If no event loop is running in this thread, this will simply call `asyncio.run()`.
    If already on an event loop, a worker thread will be used to run the coroutine.

    Args:
        coro:
            The coroutine to run.

        timeout (float | None):
            Optional timeout in seconds to wait for the coroutine to complete.

        ex (concurrent.futures.Executor | None):
            Optional executor to use when offloading to a worker thread.
            If None, a new two-thread ThreadPoolExecutor will be created for this call.

    Returns:
        The result of the coroutine.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop in this thread -> start one
        if timeout is not None:
            coro = asyncio.wait_for(coro, timeout)
        return asyncio.run(coro)

    # We are on a running loop in THIS thread -> hop to a worker thread
    ctx = contextvars.copy_context()

    def _thread_entry():
        new_loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(new_loop)
            return new_loop.run_until_complete(asyncio.wait_for(coro, timeout))
        finally:
            new_loop.close()

    if ex is not None:
        return ex.submit(ctx.run, _thread_entry).result()

    # Use max_workers=2 to avoid deadlocks in case of re-entrancy
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as temp_ex:
        return temp_ex.submit(ctx.run, _thread_entry).result()


class AsyncHelper:
    """
    Helper class for running blocking code asynchronously using a ThreadPoolExecutor.
    Also provides async versions of multiprocessing.Queue put/get operations.

    Args:
        max_workers (int | None):
            Maximum number of worker threads in the ThreadPoolExecutor.
            If None, the default value from ThreadPoolExecutor is used.

        thread_name_prefix (str):
            Prefix for naming worker threads.
            Default is "AsyncHelper".

        register_atexit (bool):
            If True, register an atexit handler to shutdown the executor on program exit.
            Default is True.
    """

    def __init__(
        self,
        *,
        max_workers: Optional[int] = None,
        thread_name_prefix: str = "AsyncHelper",
        register_atexit: bool = True,
    ):
        self._ex = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        self._closed = False
        if register_atexit:
            # Best effort cleanup on exit
            import atexit
            atexit.register(self.shutdown, wait=False, cancel_futures=True)

    async def offload(
        self,
        func: Callable[..., _T],
        /,
        *args,
        **kwargs,
    ) -> _T:
        """
        Offload a blocking function to our ThreadPoolExecutor and await its result.
        Useful for running blocking code without blocking the event loop.

        Args:
            func:
                The blocking function to run.

            *args:
                Positional arguments to pass to the function.

            **kwargs:
                Keyword arguments to pass to the function.

        Returns:
            The result of the function.
        """

        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()
        return await loop.run_in_executor(self._ex, lambda: ctx.run(func, *args, **kwargs))

    async def mpq_put(
        self,
        q: mp.Queue,
        item: Any,
        *,
        timeout: Optional[float] = None,
    ):
        """
        Asynchronously put an item into a multiprocessing.Queue.
        This function offloads the blocking put operation to our ThreadPoolExecutor,
        so it doesn't block the event loop.
        However, note that the put operation may still block if the queue is full.
        """
        return await self.offload(q.put, item, timeout=timeout)

    async def mpq_get(
        self,
        q: mp.Queue,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Asynchronously get an item from a multiprocessing.Queue.
        This function offloads the blocking get operation to a ThreadPoolExecutor,
        so it doesn't block the event loop.
        However, note that the get operation may still block if the queue is empty.
        """
        return await self.offload(q.get, timeout=timeout)

    def run_coro_sync(
        self,
        coro: Awaitable[_T],
        *,
        timeout: Optional[float] = None,
    ) -> _T:
        """
        Run a coroutine from synchronous code, even if already on an event loop.
        If no event loop is running in this thread, this will simply call `asyncio.run()`.
        If already on an event loop, our ThreadPoolExecutor will be used to run the coroutine.

        Args:
            coro:
                The coroutine to run.

            timeout (float | None):
                Optional timeout in seconds to wait for the coroutine to complete.

        Returns:
            The result of the coroutine.
        """

        return run_coro_sync(coro, timeout=timeout, ex=self._ex)

    def mpq_put_sync(
        self,
        q: mp.Queue,
        item: Any,
        *,
        timeout: Optional[float] = None,
    ):
        """
        Synchronous version of `mpq_put()`.
        """
        return self.run_coro_sync(self.mpq_put(q, item, timeout=timeout))

    def mpq_get_sync(
        self,
        q: mp.Queue,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Synchronous version of `mpq_get()`.
        """
        return self.run_coro_sync(self.mpq_get(q, timeout=timeout))

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False):
        if self._closed:
            return
        self._closed = True
        self._ex.shutdown(wait=wait, cancel_futures=cancel_futures)
