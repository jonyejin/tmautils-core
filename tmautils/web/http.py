# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Sulyab Thottungal Valapu

from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable
import asyncio
from contextlib import asynccontextmanager, AbstractAsyncContextManager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.parse

import aiohttp
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from tmautils.common import (
    LogHelper,
    get_logger_from_helper,
    AsyncRateLimiter,
)


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """Configuration for retry behavior in request_with_retry.

    Statuses are split into two categories with different retry strategies:

    - ``retryable_error_statuses``: Transient server errors.
      Only the individual request retries with exponential backoff
      (configured via `retryable_error_min_wait`, `retryable_error_multiplier`
      and `retryable_error_max_wait`.)
      Other concurrent requests to the same host are NOT slowed down.

    - ``rate_limited_statuses``: Rate limiting signals. The individual request
      retries with a stricter backoff strategy (configured via `rate_limited_min_wait`,
      `rate_limited_multiplier` and `rate_limited_max_wait`),
      AND when a ``rate_limiter`` is provided, all other concurrent requests
      to the same host are signaled to back off via ``signal_backoff()``.
      Also respects the ``Retry-After`` header by default.

    If a server uses a non-standard status code (e.g. 403) as a rate
    limiting signal, move it from ``retryable_error_statuses`` to
    ``rate_limited_statuses`` to get cross-request backoff::

        RetryConfig(rate_limited_statuses=frozenset({429, 403}))

    Args:
        max_attempts: Maximum number of attempts (including the initial attempt).
        retryable_error_statuses: HTTP status codes for transient errors that
            should trigger a retry. If None, ``request_with_retry`` applies
            method-specific defaults:
            safe methods (GET, HEAD, OPTIONS, TRACE) retry on {500, 502, 503, 504},
            unsafe methods (POST, PUT, PATCH, DELETE) retry only on {503}.
        rate_limited_statuses: HTTP status codes indicating rate limiting.
            Triggers stricter backoff and cross-request backoff signaling
            when a `rate_limiter` is provided.
        retryable_error_min_wait: Minimum wait between retries for transient errors (seconds).
        retryable_error_multiplier: Multiplier for exponential backoff on transient errors.
        retryable_error_max_wait: Maximum wait between retries for transient errors (seconds).
        rate_limited_min_wait: Minimum wait between retries for rate limited responses (seconds).
        rate_limited_multiplier: Multiplier for exponential backoff on rate limited responses.
        rate_limited_max_wait: Maximum wait between retries for rate limited responses (seconds).
        respect_retry_after: Whether to respect the Retry-After header from server responses.
        max_retry_after: Maximum wait time to respect from Retry-After header (seconds).
    """

    max_attempts: int = 3
    retryable_error_statuses: frozenset[int] | None = None
    rate_limited_statuses: frozenset[int] = frozenset({429})
    retryable_error_min_wait: float = 1.0
    retryable_error_multiplier: float = 1.0
    retryable_error_max_wait: float = 60.0
    rate_limited_min_wait: float = 5.0
    rate_limited_multiplier: float = 2.0
    rate_limited_max_wait: float = 60.0
    respect_retry_after: bool = True
    max_retry_after: float = 60.0


def url_to_rate_limit_key(url: str) -> str:
    """Extract hostname from URL for use as rate limit key."""
    return (urllib.parse.urlparse(url).hostname or "").lower()


@asynccontextmanager
async def _noop_acquire() -> AsyncIterator[None]:
    """No-op context manager when no rate limiter provided."""
    yield


def _get_acquire_info(
    url: str,
    rate_limiter: AsyncRateLimiter | None,
) -> tuple[
    Callable[[], AbstractAsyncContextManager[None]],
    Callable[[], AbstractAsyncContextManager[None]],
    str | None,
]:
    """Get hold_slot and acquire_token functions, plus the rate limit key."""
    if rate_limiter is not None:
        key = url_to_rate_limit_key(url)
        return (
            lambda: rate_limiter.hold_slot(key),
            lambda: rate_limiter.acquire_token(key),
            key,
        )
    return _noop_acquire, _noop_acquire, None


def _make_backoff_signaler(
    rate_limiter: AsyncRateLimiter | None,
    key: str | None,
) -> Callable[[RetryCallState], None] | None:
    """Create a tenacity before_sleep callback that signals backoff."""
    if rate_limiter is None:
        return None

    def before_sleep(retry_state: RetryCallState) -> None:
        outcome = retry_state.outcome
        if outcome is None:
            return
        exc = outcome.exception()
        if isinstance(exc, _RetryableHTTPStatus) and exc.is_rate_limited:
            rate_limiter.signal_backoff(key, retry_state.upcoming_sleep)

    return before_sleep


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
    close_connection: bool = False,
    retry_config: RetryConfig | None = None,
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
            When provided, a semaphore slot is held across all retry attempts
            and rate tokens are acquired per attempt.

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

        close_connection:
            If True, close the underlying connection after the response is consumed
            instead of returning it to the connection pool.
            Default is False.

        retry_config:
            Retry behavior configuration. If None, uses default RetryConfig().
            See :class:`RetryConfig` for available options.

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

        Custom retry configuration:
        ```python
        cfg = RetryConfig(max_attempts=5, rate_limited_min_wait=10.0)
        async with request_with_retry(
            session, "POST", url, json=payload, retry_config=cfg
        ) as resp:
            result = await resp.json()
        ```
    """
    # Validate parameters
    if data is not None and json is not None:
        raise ValueError("Cannot specify both 'data' and 'json' parameters")

    cfg = retry_config or RetryConfig()

    # Get rate limiter functions and key
    hold_slot, acquire_token, rl_key = _get_acquire_info(url, rate_limiter)

    # Set method-specific default retryable error statuses
    # (rate limited statuses handled separately)
    method = method.upper()
    retryable_error_statuses = cfg.retryable_error_statuses
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
        stop=stop_after_attempt(cfg.max_attempts),
        wait=_WaitRetryAfterOrRandomExp(
            retryable_error_multiplier=cfg.retryable_error_multiplier,
            retryable_error_min=cfg.retryable_error_min_wait,
            retryable_error_max=cfg.retryable_error_max_wait,
            rate_limited_multiplier=cfg.rate_limited_multiplier,
            rate_limited_min=cfg.rate_limited_min_wait,
            rate_limited_max=cfg.rate_limited_max_wait,
        ),
        retry=retry_if_exception_type(
            (_RetryableHTTPStatus, aiohttp.ClientError, asyncio.TimeoutError)
        ),
        before_sleep=_make_backoff_signaler(rate_limiter, rl_key),
        reraise=True,
    )

    timeout = aiohttp.ClientTimeout(total=attempt_timeout)
    resp: aiohttp.ClientResponse | None = None
    async with hold_slot():  # Semaphore held across all retry attempts
        async for attempt in retrying:
            with attempt:
                try:
                    # Acquire rate token per attempt
                    async with acquire_token():
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
                        logger.debug(
                            "%s %s [attempt %d] returned HTTP %d; headers=%s",
                            method, url, attempt.retry_state.attempt_number,
                            status, dict(headers),
                        )

                        # Check for retryable status
                        is_rate_limited = status in cfg.rate_limited_statuses
                        is_retryable_error = status in retryable_error_statuses

                        if is_rate_limited or is_retryable_error:
                            # Use Retry-After if applicable
                            retry_after = eff_retry_after = None
                            if cfg.respect_retry_after:
                                retry_after = _parse_retry_after(
                                    headers.get("Retry-After")
                                )
                                if retry_after is not None:
                                    eff_retry_after = min(
                                        retry_after, cfg.max_retry_after
                                    )

                            logger.info(
                                "Retryable HTTP %s for %s %s "
                                "(retry_after=%s, eff_retry_after=%s, "
                                "is_rate_limited=%s, is_retryable_error=%s)",
                                status, method, url,
                                retry_after, eff_retry_after,
                                is_rate_limited, is_retryable_error
                            )

                            # Release connection before retrying
                            resp.release() if not close_connection else resp.close()
                            resp = None

                            if is_rate_limited and rate_limiter is not None:
                                # Signal backoff immediately on a rate-limited status.
                                # This covers max_attempts=1 and last-attempt 429s
                                # where tenacity's before_sleep won't fire,
                                # causing other requests to immediately go through
                                # even though the server is telling us to slow down.
                                # This backoff could be extended (but not shortened)
                                # by tenacity based on its retry wait computation.
                                backoff_duration = (
                                    eff_retry_after or cfg.rate_limited_min_wait
                                )
                                rate_limiter.signal_backoff(
                                    rl_key, backoff_duration
                                )

                            raise _RetryableHTTPStatus(
                                status,
                                retry_after=eff_retry_after,
                                is_rate_limited=is_rate_limited,
                            )

                    # Success or non-retryable status - break out of retry loop
                    break

                except _RetryableHTTPStatus:
                    raise  # Trigger retry
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    if resp is not None:
                        resp.release() if not close_connection else resp.close()
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
            resp.release() if not close_connection else resp.close()


_GET_DEFAULT_RETRY_CONFIG = RetryConfig(
    retryable_error_statuses=frozenset({500, 502, 503, 504}),
)


@asynccontextmanager
async def get_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    *,
    rate_limiter: AsyncRateLimiter | None = None,
    retry_config: RetryConfig | None = None,
    attempt_timeout: float = 10.0,
    log_helper: LogHelper | None = None,
    **request_kwargs,
) -> AsyncIterator[aiohttp.ClientResponse]:
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
        retry_config=retry_config or _GET_DEFAULT_RETRY_CONFIG,
        attempt_timeout=attempt_timeout,
        log_helper=log_helper,
        **request_kwargs,
    ) as resp:
        yield resp
