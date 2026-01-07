from typing import Optional
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import aiohttp
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from tmautils.common import LogHelper, get_logger_from_helper


class _RetryableHTTPStatus(aiohttp.ClientError):
    def __init__(self, status: int, retry_after: Optional[float] = None):
        super().__init__(f"Retryable HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None

    # Try to parse as delta-seconds
    try:
        return max(0.0, float(value))
    except ValueError:
        pass  # Fall through

    # Try to parse as HTTP-date
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (dt - now).total_seconds())
    except Exception:
        return None


class _WaitRetryAfterOrRandomExp:
    def __init__(self, multiplier: float = 1.0, max_: float = 60.0):
        self._exp = wait_random_exponential(
            multiplier=multiplier, max=max_
        )

    def __call__(self, retry_state) -> float:
        # If retry_after is available, use that
        if retry_state.outcome and retry_state.outcome.failed:
            exc = retry_state.outcome.exception()
            if isinstance(exc, _RetryableHTTPStatus) and exc.retry_after is not None:
                return exc.retry_after

        # Otherwise, use exponential backoff with jitter
        return self._exp(retry_state)


@asynccontextmanager
async def aget_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    attempt_timeout: float = 10.0,
    max_attempts: int = 3,
    retry_statuses: frozenset[int] = frozenset({429, 503}),
    retry_multiplier: float = 0.5,
    retry_max_wait: float = 30.0,
    respect_retry_after: bool = True,
    max_retry_after: float = 60.0,
    log_helper: Optional[LogHelper] = None,
    **request_kwargs,
):
    """
    Asynchronous HTTP GET with retry mechanism.

    Args:
        session:
            aiohttp ClientSession to use for requests.

        url:
            URL to send the GET request to.

        attempt_timeout:
            Timeout for each individual attempt in seconds.
            Default is 10.0 seconds.

        max_attempts:
            Maximum number of attempts (including the initial attempt).
            Default is 3 attempts.

        retry_statuses:
            Set of HTTP status codes that should trigger a retry.
            Default is {429, 503}.

        retry_multiplier:
            Multiplier for exponential backoff calculation.
            Default is 0.5.

        retry_max_wait:
            Maximum wait time between retries in seconds.
            Default is 30.0 seconds.

        respect_retry_after:
            Whether to respect the 'Retry-After' header from server responses.
            Default is True.

        max_retry_after:
            Maximum wait time to respect from 'Retry-After' header in seconds.
            Default is 60.0 seconds.

        log_helper:
            Optional LogHelper for logging.

        **request_kwargs:
            Additional keyword arguments to pass to `aiohttp.ClientSession.get()`.

    Yields:
        aiohttp.ClientResponse:
            The HTTP response object.

    Raises:
        aiohttp.ClientError:
            If all retry attempts fail.

        asyncio.TimeoutError:
            If a timeout occurs during the request.
    """

    logger = get_logger_from_helper(log_helper)

    # Retry config
    retrying = AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=_WaitRetryAfterOrRandomExp(
            multiplier=retry_multiplier, max_=retry_max_wait
        ),
        retry=retry_if_exception_type(
            (_RetryableHTTPStatus, aiohttp.ClientError, asyncio.TimeoutError)
        ),
        reraise=True,
    )

    timeout = aiohttp.ClientTimeout(total=attempt_timeout)
    resp: Optional[aiohttp.ClientResponse] = None
    async for attempt in retrying:
        with attempt:
            try:
                # Send HTTP Request
                resp = await session.get(
                    url,
                    timeout=timeout,
                    **request_kwargs
                )

                # Check for retryable status
                if resp.status in retry_statuses:
                    # Use Retry-After if applicable
                    status = resp.status
                    retry_after = None
                    if respect_retry_after:
                        retry_after = _parse_retry_after(
                            resp.headers.get("Retry-After")
                        )
                        if retry_after is not None:
                            retry_after = min(retry_after, max_retry_after)

                    logger.info(
                        "Retryable HTTP %s for %s (retry_after=%s)",
                        status, url, retry_after
                    )

                    # Release connection before retrying
                    resp.release()
                    resp = None

                    raise _RetryableHTTPStatus(status, retry_after=retry_after)

                # Success or non-retryable status - break out of retry loop
                break

            except _RetryableHTTPStatus:
                raise  # Trigger retry
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if resp is not None:
                    resp.release()
                    resp = None
                logger.info("GET error for %s", url, exc_info=True)
                raise

    # Yield response to caller outside retry mechanism
    # This may be a successful response or a non-retryable status
    try:
        # This is here to satisfy the type checker
        resp: aiohttp.ClientResponse
        yield resp
    finally:
        if resp is not None:
            resp.release()
