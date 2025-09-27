import asyncio
import concurrent.futures
import contextvars

from .types import *

_T = TypeVar("_T")

def run_coro_sync(
    coro: Awaitable[_T],
    *,
    timeout: Optional[float] = None,
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

    Returns:
        The result of the coroutine.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop here -> start one
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

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(ctx.run, _thread_entry).result()
