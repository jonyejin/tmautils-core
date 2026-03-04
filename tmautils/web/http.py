# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from typing import Any, AsyncIterator, Callable
import asyncio
from contextlib import asynccontextmanager, AbstractAsyncContextManager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.parse

import aiohttp
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from tmautils.common import (
    LogHelper,
    get_logger_from_helper,
    AsyncRateLimiter,
    RateLimitScope,
)


def url_to_rate_limit_key(url: str) -> str:
    """Extract hostname from URL for use as rate limit key."""
    return (urllib.parse.urlparse(url).hostname or "").lower()


@asynccontextmanager
async def _noop_acquire() -> AsyncIterator[None]:
    """No-op context manager when no rate limiter provided."""
    yield


def _get_acquire_func(
    url: str,
    rate_limiter: AsyncRateLimiter | None,
) -> Callable[[], AbstractAsyncContextManager[None]]:
    """Get acquire function from rate limiter or return no-op."""
    if rate_limiter is not None:
        # Extract key at call site for per-key limiting
        key = (
            url_to_rate_limit_key(url)
            if rate_limiter._scope == RateLimitScope.PER_KEY else None
        )
        return lambda: rate_limiter.acquire(key)
    return _noop_acquire


class _RetryableHTTPStatus(aiohttp.ClientError):
    def __init__(
        self,
        status: int,
        retry_after: float | None = None,
        is_rate_limited: bool = False,
    ):
        super().__init__(f"Retryable HTTP {status}")
        self.status = status
        self.retry_after = retry_after
        self.is_rate_limited = is_rate_limited


def _parse_retry_after(value: str | None) -> float | None:
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
    def __init__(
        self,
        # Retryable error params
        retryable_error_multiplier: float = 1.0,
        retryable_error_min: float = 1.0,
        retryable_error_max: float = 60.0,
        # Rate limited params
        rate_limited_multiplier: float = 2.0,
        rate_limited_min: float = 5.0,
        rate_limited_max: float = 60.0,
    ):
        self._retryable_error_wait = wait_random_exponential(
            multiplier=retryable_error_multiplier,
            min=retryable_error_min,
            max=retryable_error_max,
        )
        self._rate_limited_wait = wait_random_exponential(
            multiplier=rate_limited_multiplier,
            min=rate_limited_min,
            max=rate_limited_max,
        )

    def __call__(self, retry_state) -> float:
        if retry_state.outcome and retry_state.outcome.failed:
            exc = retry_state.outcome.exception()
            if isinstance(exc, _RetryableHTTPStatus):
                # If retry_after is available, use that
                if exc.retry_after is not None:
                    return exc.retry_after
                # If rate limited, use rate limited wait strategy
                if exc.is_rate_limited:
                    return self._rate_limited_wait(retry_state)

        # Default to retryable error wait strategy
        return self._retryable_error_wait(retry_state)


@asynccontextmanager
async def request_with_retry(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    rate_limiter: AsyncRateLimiter | None = None,
    data: Any = None,
    json: Any = None,
    attempt_timeout: float = 10.0,
    max_attempts: int = 3,
    retryable_error_statuses: frozenset[int] | None = None,
    rate_limited_statuses: frozenset[int] = frozenset({429}),
    # Retryable error wait params
    retryable_error_min_wait: float = 1.0,
    retryable_error_multiplier: float = 1.0,
    retryable_error_max_wait: float = 60.0,
    # Rate limited wait params
    rate_limited_min_wait: float = 5.0,
    rate_limited_multiplier: float = 2.0,
    rate_limited_max_wait: float = 60.0,
    respect_retry_after: bool = True,
    max_retry_after: float = 60.0,
    log_helper: LogHelper | None = None,
    **request_kwargs,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """
    Asynchronous HTTP request with retry mechanism.

    Args:
        session:
            aiohttp ClientSession to use for requests.

        method:
            HTTP method (e.g., "GET", "POST", "HEAD", "PUT", "PATCH", "DELETE").
            Case-insensitive.

        url:
            URL to send the request to.

        rate_limiter:
            Optional AsyncRateLimiter for rate limiting.
            When provided, rate limits are acquired per-attempt
            and released during retry backoffs.

        data:
            Request body data.
            Forwarded to aiohttp.ClientSession.request().
            See aiohttp documentation for supported types (bytes, str, FormData, etc.).
            Cannot be used together with json parameter.

        json:
            JSON-serializable data to send in request body.
            Forwarded to aiohttp.ClientSession.request().
            Cannot be used together with data parameter.

        attempt_timeout:
            Timeout for each individual attempt in seconds.
            Default is 10.0 seconds.

        max_attempts:
            Maximum number of attempts (including the initial attempt).
            Default is 3 attempts.

        retryable_error_statuses:
            Set of HTTP status codes for transient errors that should trigger a retry.
            If None (default), uses method-specific defaults:
            - Safe methods (GET, HEAD, OPTIONS, TRACE): {500, 502, 503, 504}
            - Unsafe methods (POST, PUT, PATCH, DELETE): {503}

            Unsafe methods default to conservative retry due to idempotency concerns.
            Override with custom frozenset if your endpoint is idempotent.

        rate_limited_statuses:
            Set of HTTP status codes indicating rate limiting.
            These use a stricter backoff strategy than transient errors.
            Default is {429}.

        retryable_error_min_wait:
            Minimum wait time between retries for transient errors in seconds.
            Default is 1.0 seconds.

        retryable_error_multiplier:
            Multiplier for exponential backoff calculation for transient errors.
            Default is 1.0.

        retryable_error_max_wait:
            Maximum wait time between retries for transient errors in seconds.
            Default is 60.0 seconds.

        rate_limited_min_wait:
            Minimum wait time between retries for rate limited responses in seconds.
            Default is 5.0 seconds.

        rate_limited_multiplier:
            Multiplier for exponential backoff calculation for rate limited responses.
            Default is 2.0.

        rate_limited_max_wait:
            Maximum wait time between retries for rate limited responses in seconds.
            Default is 60.0 seconds.

        respect_retry_after:
            Whether to respect the 'Retry-After' header from server responses.
            Default is True.

        max_retry_after:
            Maximum wait time to respect from 'Retry-After' header in seconds.
            Default is 60.0 seconds.

        log_helper:
            Optional LogHelper for logging.

        **request_kwargs:
            Additional keyword arguments to pass to `aiohttp.ClientSession.request()`.

    Yields:
        aiohttp.ClientResponse:
            The HTTP response object.

    Raises:
        ValueError:
            If both data and json parameters are specified.

        aiohttp.ClientError:
            If all retry attempts fail.

        asyncio.TimeoutError:
            If a timeout occurs during the request.

    Examples:
        GET request with plain session:
        ```python
        async with request_with_retry(session, "GET", url) as resp:
            data = await resp.json()
        ```

        GET request with rate limiter:
        ```python
        limiter = AsyncRateLimiter(max_concurrent=10)
        async with aiohttp.ClientSession() as session:
            async with request_with_retry(
                session, "GET", url, rate_limiter=limiter
            ) as resp:
                data = await resp.json()
        ```

        HEAD request:
        ```python
        async with request_with_retry(session, "HEAD", url) as resp:
            content_length = resp.headers.get("Content-Length")
        ```

        POST request with JSON:
        ```python
        async with request_with_retry(
            session, "POST", url, json={"key": "value"}
        ) as resp:
            result = await resp.json()
        ```

        POST request with data:
        ```python
        async with request_with_retry(
            session, "POST", url, data=b"raw bytes"
        ) as resp:
            result = await resp.text()
        ```
    """
    # Validate parameters
    if data is not None and json is not None:
        raise ValueError("Cannot specify both 'data' and 'json' parameters")

    # Get acquire function from rate limiter
    acquire = _get_acquire_func(url, rate_limiter)

    # Set method-specific default retryable error statuses
    # (rate limited statuses handled separately)
    method = method.upper()
    if retryable_error_statuses is None:
        if method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            # Safe/idempotent methods - can retry more aggressively
            retryable_error_statuses = frozenset({500, 502, 503, 504})
        else:
            # More conservative to avoid duplicate operations
            retryable_error_statuses = frozenset({503})

    logger = get_logger_from_helper(log_helper)

    # Retry config
    retrying = AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=_WaitRetryAfterOrRandomExp(
            retryable_error_multiplier=retryable_error_multiplier,
            retryable_error_min=retryable_error_min_wait,
            retryable_error_max=retryable_error_max_wait,
            rate_limited_multiplier=rate_limited_multiplier,
            rate_limited_min=rate_limited_min_wait,
            rate_limited_max=rate_limited_max_wait,
        ),
        retry=retry_if_exception_type(
            (_RetryableHTTPStatus, aiohttp.ClientError, asyncio.TimeoutError)
        ),
        reraise=True,
    )

    timeout = aiohttp.ClientTimeout(total=attempt_timeout)
    resp: aiohttp.ClientResponse | None = None
    async for attempt in retrying:
        with attempt:
            try:
                # Acquire rate limits per attempt (released during backoff)
                async with acquire():
                    # Send HTTP Request
                    resp = await session.request(
                        method=method,
                        url=url,
                        data=data,
                        json=json,
                        timeout=timeout,
                        **request_kwargs
                    )

                    # Debug outputs
                    status = resp.status
                    headers = resp.headers
                    logger.debug("Response status: %s, headers: %s", status, dict(headers))

                    # Check for retryable status
                    is_rate_limited = status in rate_limited_statuses
                    is_retryable_error = status in retryable_error_statuses

                    if is_rate_limited or is_retryable_error:
                        # Use Retry-After if applicable
                        retry_after = None
                        if respect_retry_after:
                            retry_after = _parse_retry_after(
                                headers.get("Retry-After")
                            )
                            if retry_after is not None:
                                retry_after = min(retry_after, max_retry_after)

                        logger.info(
                            "Retryable HTTP %s for %s %s (retry_after=%s, rate_limited=%s)",
                            status, method, url, retry_after, is_rate_limited
                        )

                        # Release connection before retrying
                        resp.release()
                        resp = None

                        raise _RetryableHTTPStatus(
                            status,
                            retry_after=retry_after,
                            is_rate_limited=is_rate_limited,
                        )

                # Success or non-retryable status - break out of retry loop
                break

            except _RetryableHTTPStatus:
                raise  # Trigger retry
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if resp is not None:
                    resp.release()
                    resp = None
                logger.info(
                    "%s error for %s %s: %s",
                    type(e).__name__, method, url, e
                )
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


@asynccontextmanager
async def get_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    rate_limiter: AsyncRateLimiter | None = None,
    attempt_timeout: float = 10.0,
    max_attempts: int = 3,
    retryable_error_statuses: frozenset[int] = frozenset({500, 502, 503, 504}),
    rate_limited_statuses: frozenset[int] = frozenset({429}),
    # Retryable error wait params
    retryable_error_min_wait: float = 1.0,
    retryable_error_multiplier: float = 1.0,
    retryable_error_max_wait: float = 60.0,
    # Rate limited wait params
    rate_limited_min_wait: float = 5.0,
    rate_limited_multiplier: float = 2.0,
    rate_limited_max_wait: float = 60.0,
    respect_retry_after: bool = True,
    max_retry_after: float = 60.0,
    log_helper: LogHelper | None = None,
    **request_kwargs,
):
    """
    Asynchronous HTTP GET with retry mechanism.

    This function is a convenience wrapper around `request_with_retry()` for GET
    requests.

    Refer to `request_with_retry()` for detailed parameter descriptions.
    """
    async with request_with_retry(
        session=session,
        method="GET",
        url=url,
        rate_limiter=rate_limiter,
        attempt_timeout=attempt_timeout,
        max_attempts=max_attempts,
        retryable_error_statuses=retryable_error_statuses,
        rate_limited_statuses=rate_limited_statuses,
        retryable_error_min_wait=retryable_error_min_wait,
        retryable_error_multiplier=retryable_error_multiplier,
        retryable_error_max_wait=retryable_error_max_wait,
        rate_limited_min_wait=rate_limited_min_wait,
        rate_limited_multiplier=rate_limited_multiplier,
        rate_limited_max_wait=rate_limited_max_wait,
        respect_retry_after=respect_retry_after,
        max_retry_after=max_retry_after,
        log_helper=log_helper,
        **request_kwargs,
    ) as resp:
        yield resp
