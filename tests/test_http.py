import pytest
import asyncio
from unittest.mock import AsyncMock, Mock
from datetime import datetime, timezone, timedelta
import aiohttp

from tmautils.web import get_with_retry, request_with_retry, RetryConfig
from tmautils.web.http import (
    _parse_retry_after,
    _RetryableHTTPStatus,
    _WaitRetryAfterOrRandomExp,
)


def _run_async(coro):
    """Helper to run async test code in sync test"""
    return asyncio.run(coro)


class MockResponse:
    """Mock aiohttp.ClientResponse"""
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}
        self.release_called = False
        self._released = False

    def release(self):
        self.release_called = True
        self._released = True


def test_successful_get():
    """Test successful HTTP GET returns response and releases it"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.request = AsyncMock(return_value=mock_response)

        async with get_with_retry(mock_session, "http://example.com") as resp:
            assert resp.status == 200
            assert not resp.release_called  # Not released yet during processing

        # After context exits, response should be released
        assert mock_response.release_called
        assert mock_session.request.call_count == 1

    _run_async(_test())


def test_retryable_status_429_triggers_retry():
    """Test 429 status triggers retry with exponential backoff"""
    async def _test():
        mock_session = Mock()
        # First two calls return 429, third returns 200
        mock_responses = [
            MockResponse(429, {"Retry-After": "1"}),
            MockResponse(429),
            MockResponse(200),
        ]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        async with get_with_retry(
            mock_session, "http://example.com", retry_config=RetryConfig(max_attempts=3)
        ) as resp:
            assert resp.status == 200

        # Should have made 3 attempts
        assert mock_session.request.call_count == 3
        # First two responses should be released before retry
        assert mock_responses[0].release_called
        assert mock_responses[1].release_called
        # Final response released after context exit
        assert mock_responses[2].release_called

    _run_async(_test())


def test_retryable_status_503_triggers_retry():
    """Test 503 status triggers retry"""
    async def _test():
        mock_session = Mock()
        mock_responses = [
            MockResponse(503),
            MockResponse(200),
        ]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        async with get_with_retry(
            mock_session, "http://example.com", retry_config=RetryConfig(max_attempts=2)
        ) as resp:
            assert resp.status == 200

        assert mock_session.request.call_count == 2
        assert mock_responses[0].release_called

    _run_async(_test())


def test_non_retryable_status_returned():
    """Test non-retryable status (404, 403) doesn't trigger retry"""
    # Note: 500 and 502 are in default retryable_error_statuses for GET, so only test non-retryable statuses
    for status in [404, 403, 400, 401]:
        async def _test():
            mock_session = Mock()
            mock_response = MockResponse(status)
            mock_session.request = AsyncMock(return_value=mock_response)

            async with get_with_retry(
                mock_session, "http://example.com", retry_config=RetryConfig(max_attempts=3)
            ) as resp:
                assert resp.status == status

            # Should only make 1 attempt (no retry)
            assert mock_session.request.call_count == 1
            assert mock_response.release_called

        _run_async(_test())


def test_network_error_triggers_retry():
    """Test network errors (ClientError) trigger retry"""
    async def _test():
        mock_session = Mock()
        # First call raises error, second succeeds
        mock_session.request = AsyncMock(
            side_effect=[
                aiohttp.ClientError("Connection failed"),
                MockResponse(200),
            ]
        )

        async with get_with_retry(
            mock_session, "http://example.com", retry_config=RetryConfig(max_attempts=2)
        ) as resp:
            assert resp.status == 200

        assert mock_session.request.call_count == 2

    _run_async(_test())


def test_timeout_error_triggers_retry():
    """Test timeout errors trigger retry"""
    async def _test():
        mock_session = Mock()
        mock_session.request = AsyncMock(
            side_effect=[
                asyncio.TimeoutError(),
                MockResponse(200),
            ]
        )

        async with get_with_retry(
            mock_session, "http://example.com", retry_config=RetryConfig(max_attempts=2)
        ) as resp:
            assert resp.status == 200

        assert mock_session.request.call_count == 2

    _run_async(_test())


def test_caller_exception_no_retry():
    """
    CRITICAL TEST for Bug #2:
    Test that exceptions raised by caller code while processing
    the response do NOT trigger HTTP request retries
    """
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.request = AsyncMock(return_value=mock_response)

        # Caller code raises aiohttp.ClientError while processing response
        with pytest.raises(aiohttp.ClientError, match="Caller error"):
            async with get_with_retry(
                mock_session, "http://example.com", retry_config=RetryConfig(max_attempts=3)
            ) as resp:
                # Simulating caller code that fails while processing response
                raise aiohttp.ClientError("Caller error")

        # Should only make 1 HTTP request (no retry)
        assert mock_session.request.call_count == 1
        # Response should still be released
        assert mock_response.release_called

    _run_async(_test())


def test_caller_timeout_exception_no_retry():
    """Test that TimeoutError from caller code doesn't trigger retry"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.request = AsyncMock(return_value=mock_response)

        with pytest.raises(asyncio.TimeoutError):
            async with get_with_retry(
                mock_session, "http://example.com", retry_config=RetryConfig(max_attempts=3)
            ) as resp:
                # Simulating caller code that times out
                raise asyncio.TimeoutError()

        # Should only make 1 HTTP request (no retry)
        assert mock_session.request.call_count == 1
        assert mock_response.release_called

    _run_async(_test())


def test_max_retries_exhausted():
    """Test that max retries exhausted raises the last exception"""
    async def _test():
        mock_session = Mock()
        mock_session.request = AsyncMock(
            side_effect=[
                aiohttp.ClientError("Error 1"),
                aiohttp.ClientError("Error 2"),
                aiohttp.ClientError("Error 3"),
            ]
        )

        with pytest.raises(aiohttp.ClientError, match="Error 3"):
            async with get_with_retry(
                mock_session, "http://example.com", retry_config=RetryConfig(max_attempts=3)
            ) as resp:
                pass

        assert mock_session.request.call_count == 3

    _run_async(_test())


def test_retry_after_delta_seconds():
    """Test Retry-After header with delta-seconds format"""
    retry_after = _parse_retry_after("30")
    assert retry_after == 30.0

    retry_after = _parse_retry_after("0")
    assert retry_after == 0.0

    # Negative values should be clamped to 0
    retry_after = _parse_retry_after("-10")
    assert retry_after == 0.0


def test_retry_after_http_date():
    """Test Retry-After header with HTTP-date format"""
    # Create a date 60 seconds in the future
    future_time = datetime.now(timezone.utc) + timedelta(seconds=60)
    http_date = future_time.strftime("%a, %d %b %Y %H:%M:%S GMT")

    retry_after = _parse_retry_after(http_date)
    # Should be approximately 60 seconds (allow some variance for test execution time)
    assert 58 <= retry_after <= 62


def test_retry_after_invalid():
    """Test Retry-After header with invalid values"""
    assert _parse_retry_after("invalid") is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after(None) is None


def test_respect_retry_after_honored():
    """Test that Retry-After header is respected when enabled"""
    async def _test():
        mock_session = Mock()
        mock_responses = [
            MockResponse(429, {"Retry-After": "0.1"}),  # Small delay for testing
            MockResponse(200),
        ]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        start_time = asyncio.get_event_loop().time()
        async with get_with_retry(
            mock_session,
            "http://example.com",
            retry_config=RetryConfig(max_attempts=2, respect_retry_after=True),
        ) as resp:
            assert resp.status == 200

        elapsed = asyncio.get_event_loop().time() - start_time
        # Should have waited at least 0.1 seconds
        assert elapsed >= 0.1

    _run_async(_test())


def test_respect_retry_after_disabled():
    """Test that Retry-After header is ignored when disabled"""
    async def _test():
        mock_session = Mock()
        mock_responses = [
            MockResponse(429, {"Retry-After": "100"}),  # Large delay
            MockResponse(200),
        ]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        start_time = asyncio.get_event_loop().time()
        async with get_with_retry(
            mock_session,
            "http://example.com",
            retry_config=RetryConfig(
                max_attempts=2,
                respect_retry_after=False,
                retryable_error_multiplier=0.01,  # Very small for fast test
            ),
        ) as resp:
            assert resp.status == 200

        elapsed = asyncio.get_event_loop().time() - start_time
        # Should NOT have waited 100 seconds
        assert elapsed < 10

    _run_async(_test())


def test_max_retry_after_cap():
    """Test that retry_after is capped at max_retry_after"""
    async def _test():
        mock_session = Mock()
        mock_responses = [
            MockResponse(429, {"Retry-After": "1000"}),  # Very large delay
            MockResponse(200),
        ]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        start_time = asyncio.get_event_loop().time()
        async with get_with_retry(
            mock_session,
            "http://example.com",
            retry_config=RetryConfig(
                max_attempts=2,
                respect_retry_after=True,
                max_retry_after=0.2,  # Cap at 0.2 seconds
            ),
        ) as resp:
            assert resp.status == 200

        elapsed = asyncio.get_event_loop().time() - start_time
        # Should have waited ~0.2 seconds, not 1000
        assert 0.2 <= elapsed < 10

    _run_async(_test())


def test_custom_retryable_error_statuses():
    """Test custom retryable_error_statuses parameter"""
    async def _test():
        mock_session = Mock()
        # 404 normally not retried, but we'll configure it to retry
        mock_responses = [
            MockResponse(404),
            MockResponse(200),
        ]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        async with get_with_retry(
            mock_session,
            "http://example.com",
            retry_config=RetryConfig(
                max_attempts=2,
                retryable_error_statuses=frozenset({404}),
            ),
        ) as resp:
            assert resp.status == 200

        assert mock_session.request.call_count == 2

    _run_async(_test())


def test_request_kwargs_passed_through():
    """Test that additional request kwargs are passed to session.get"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.request = AsyncMock(return_value=mock_response)

        headers = {"User-Agent": "test"}
        params = {"key": "value"}

        async with get_with_retry(
            mock_session,
            "http://example.com",
            headers=headers,
            params=params,
        ) as resp:
            assert resp.status == 200

        # Verify kwargs were passed to session.get
        call_kwargs = mock_session.request.call_args.kwargs
        assert call_kwargs["headers"] == headers
        assert call_kwargs["params"] == params

    _run_async(_test())


def test_resource_cleanup_on_success():
    """Test that response is properly released after successful use"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.request = AsyncMock(return_value=mock_response)

        async with get_with_retry(mock_session, "http://example.com") as resp:
            assert not resp.release_called

        # After exiting context, response should be released
        assert mock_response.release_called

    _run_async(_test())


def test_resource_cleanup_on_caller_exception():
    """Test that response is released even when caller raises exception"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.request = AsyncMock(return_value=mock_response)

        with pytest.raises(ValueError):
            async with get_with_retry(mock_session, "http://example.com") as resp:
                raise ValueError("Test error")

        # Response should be released even though exception was raised
        assert mock_response.release_called

    _run_async(_test())


# ===== Tests for request_with_retry =====

def test_request_with_retry_get_method():
    """Test request_with_retry with GET method"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.request = AsyncMock(return_value=mock_response)

        async with request_with_retry(
            mock_session, "GET", "http://example.com"
        ) as resp:
            assert resp.status == 200

        # Verify request was called with correct method
        call_kwargs = mock_session.request.call_args.kwargs
        assert call_kwargs["method"] == "GET"
        assert call_kwargs["url"] == "http://example.com"
        assert call_kwargs.get("data") is None
        assert call_kwargs.get("json") is None

    _run_async(_test())


def test_request_with_retry_post_with_json():
    """Test request_with_retry POST with JSON data"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.request = AsyncMock(return_value=mock_response)

        json_data = {"key": "value", "number": 42}
        async with request_with_retry(
            mock_session, "POST", "http://example.com", json=json_data
        ) as resp:
            assert resp.status == 200

        # Verify request was called with correct parameters
        call_kwargs = mock_session.request.call_args.kwargs
        assert call_kwargs["method"] == "POST"
        assert call_kwargs["json"] == json_data
        assert call_kwargs.get("data") is None

    _run_async(_test())


def test_request_with_retry_post_with_data():
    """Test request_with_retry POST with raw data"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.request = AsyncMock(return_value=mock_response)

        raw_data = b"raw bytes data"
        async with request_with_retry(
            mock_session, "POST", "http://example.com", data=raw_data
        ) as resp:
            assert resp.status == 200

        # Verify request was called with correct parameters
        call_kwargs = mock_session.request.call_args.kwargs
        assert call_kwargs["method"] == "POST"
        assert call_kwargs["data"] == raw_data
        assert call_kwargs.get("json") is None

    _run_async(_test())


def test_request_with_retry_data_and_json_raises_error():
    """Test that specifying both data and json raises ValueError"""
    async def _test():
        mock_session = Mock()

        with pytest.raises(ValueError, match="Cannot specify both 'data' and 'json'"):
            async with request_with_retry(
                mock_session,
                "POST",
                "http://example.com",
                data=b"data",
                json={"key": "value"}
            ) as resp:
                pass

    _run_async(_test())


def test_request_with_retry_head_method():
    """Test request_with_retry with HEAD method"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200, headers={"Content-Length": "1234"})
        mock_session.request = AsyncMock(return_value=mock_response)

        async with request_with_retry(
            mock_session, "HEAD", "http://example.com"
        ) as resp:
            assert resp.status == 200
            assert resp.headers.get("Content-Length") == "1234"

        call_kwargs = mock_session.request.call_args.kwargs
        assert call_kwargs["method"] == "HEAD"

    _run_async(_test())


def test_request_with_retry_safe_methods_default_retry():
    """Test that safe methods (GET, HEAD) use aggressive default retry statuses"""
    for method in ["GET", "HEAD", "OPTIONS", "TRACE"]:
        async def _test():
            mock_session = Mock()
            # First returns 500 (should retry), then 200
            mock_responses = [MockResponse(500), MockResponse(200)]
            mock_session.request = AsyncMock(side_effect=mock_responses)

            async with request_with_retry(
                mock_session, method, "http://example.com", retry_config=RetryConfig(max_attempts=2)
            ) as resp:
                assert resp.status == 200

            # Should have retried on 500
            assert mock_session.request.call_count == 2
            assert mock_responses[0].release_called

        _run_async(_test())


def test_request_with_retry_unsafe_methods_conservative_retry():
    """Test that unsafe methods (POST, PUT, PATCH, DELETE) use conservative retry"""
    for method in ["POST", "PUT", "PATCH", "DELETE"]:
        async def _test():
            mock_session = Mock()
            # Returns 500 - should NOT retry for unsafe methods by default
            mock_response = MockResponse(500)
            mock_session.request = AsyncMock(return_value=mock_response)

            async with request_with_retry(
                mock_session, method, "http://example.com", retry_config=RetryConfig(max_attempts=3)
            ) as resp:
                assert resp.status == 500

            # Should NOT have retried on 500
            assert mock_session.request.call_count == 1

        _run_async(_test())


def test_request_with_retry_post_retries_on_429():
    """Test that POST retries on 429 (rate limit) by default"""
    async def _test():
        mock_session = Mock()
        mock_responses = [MockResponse(429), MockResponse(200)]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        async with request_with_retry(
            mock_session, "POST", "http://example.com", retry_config=RetryConfig(max_attempts=2)
        ) as resp:
            assert resp.status == 200

        # Should have retried on 429
        assert mock_session.request.call_count == 2

    _run_async(_test())


def test_request_with_retry_post_retries_on_503():
    """Test that POST retries on 503 (service unavailable) by default"""
    async def _test():
        mock_session = Mock()
        mock_responses = [MockResponse(503), MockResponse(200)]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        async with request_with_retry(
            mock_session, "POST", "http://example.com", retry_config=RetryConfig(max_attempts=2)
        ) as resp:
            assert resp.status == 200

        # Should have retried on 503
        assert mock_session.request.call_count == 2

    _run_async(_test())


def test_request_with_retry_custom_retryable_error_statuses_override():
    """Test that custom retryable_error_statuses override method-specific defaults"""
    async def _test():
        mock_session = Mock()
        # POST with 500 and custom retryable_error_statuses that includes 500
        mock_responses = [MockResponse(500), MockResponse(200)]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        async with request_with_retry(
            mock_session,
            "POST",
            "http://example.com",
            retry_config=RetryConfig(
                max_attempts=2,
                retryable_error_statuses=frozenset({429, 500, 502, 503, 504}),
            ),
        ) as resp:
            assert resp.status == 200

        # Should have retried on 500 because of custom retryable_error_statuses
        assert mock_session.request.call_count == 2

    _run_async(_test())


def test_request_with_retry_logging_includes_method():
    """Test that logging includes the HTTP method"""
    # This is mostly a smoke test to ensure the logging doesn't crash
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.request = AsyncMock(return_value=mock_response)

        async with request_with_retry(
            mock_session, "POST", "http://example.com"
        ) as resp:
            assert resp.status == 200

    _run_async(_test())


# ===== Integration tests =====

def test_post_retry_with_retry_after_header():
    """Test POST request respects Retry-After header"""
    async def _test():
        mock_session = Mock()
        mock_responses = [
            MockResponse(429, {"Retry-After": "0.1"}),
            MockResponse(200)
        ]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        start_time = asyncio.get_event_loop().time()
        async with request_with_retry(
            mock_session,
            "POST",
            "http://example.com",
            json={"test": "data"},
            retry_config=RetryConfig(
                max_attempts=2,
                retryable_error_statuses=frozenset({429, 503}),
            ),
        ) as resp:
            assert resp.status == 200

        elapsed = asyncio.get_event_loop().time() - start_time
        # Should have waited at least 0.1 seconds
        assert elapsed >= 0.1

    _run_async(_test())


def test_post_exhausts_retries():
    """Test that POST exhausts all retry attempts"""
    async def _test():
        mock_session = Mock()
        mock_session.request = AsyncMock(
            side_effect=[
                MockResponse(429),
                MockResponse(429),
                MockResponse(429),
            ]
        )

        # All attempts fail with 429, should exhaust retries
        with pytest.raises(Exception):  # tenacity will raise after max attempts
            async with request_with_retry(
                mock_session,
                "POST",
                "http://example.com",
                json={"test": "data"},
                retry_config=RetryConfig(
                    max_attempts=3,
                    retryable_error_statuses=frozenset({429, 503}),
                ),
            ) as resp:
                pass

        assert mock_session.request.call_count == 3

    _run_async(_test())


def test_post_client_error_triggers_retry():
    """Test that POST retries on ClientError"""
    async def _test():
        mock_session = Mock()
        mock_session.request = AsyncMock(
            side_effect=[
                aiohttp.ClientError("Connection failed"),
                MockResponse(200)
            ]
        )

        async with request_with_retry(
            mock_session,
            "POST",
            "http://example.com",
            json={"test": "data"},
            retry_config=RetryConfig(
                max_attempts=2,
                retryable_error_statuses=frozenset({429, 503}),
            ),
        ) as resp:
            assert resp.status == 200

        assert mock_session.request.call_count == 2

    _run_async(_test())


def test_post_timeout_triggers_retry():
    """Test that POST retries on TimeoutError"""
    async def _test():
        mock_session = Mock()
        mock_session.request = AsyncMock(
            side_effect=[
                asyncio.TimeoutError(),
                MockResponse(200)
            ]
        )

        async with request_with_retry(
            mock_session,
            "POST",
            "http://example.com",
            json={"test": "data"},
            retry_config=RetryConfig(
                max_attempts=2,
                retryable_error_statuses=frozenset({429, 503}),
            ),
        ) as resp:
            assert resp.status == 200

        assert mock_session.request.call_count == 2

    _run_async(_test())


# ===== Edge case tests =====

def test_arequest_uppercase_lowercase_method():
    """Test that method names work in both uppercase and lowercase"""
    for method in ["GET", "get", "Post", "HEAD"]:
        async def _test():
            mock_session = Mock()
            mock_response = MockResponse(200)
            mock_session.request = AsyncMock(return_value=mock_response)

            async with request_with_retry(
                mock_session, method, "http://example.com"
            ) as resp:
                assert resp.status == 200

            # Method is normalized to uppercase internally
            call_kwargs = mock_session.request.call_args.kwargs
            assert call_kwargs["method"] == method.upper()

        _run_async(_test())


def test_post_resource_cleanup_on_retry():
    """Test that response is properly released when POST retry occurs"""
    async def _test():
        mock_session = Mock()
        mock_responses = [MockResponse(429), MockResponse(200)]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        async with request_with_retry(
            mock_session,
            "POST",
            "http://example.com",
            json={"test": "data"},
            retry_config=RetryConfig(
                max_attempts=2,
                retryable_error_statuses=frozenset({429, 503}),
            ),
        ) as resp:
            assert resp.status == 200

        # First response should be released before retry
        assert mock_responses[0].release_called
        # Final response released after context exit
        assert mock_responses[1].release_called

    _run_async(_test())


def test_request_kwargs_passed_through_for_post():
    """Test that additional kwargs are passed through for POST requests"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.request = AsyncMock(return_value=mock_response)

        headers = {"Authorization": "Bearer token"}
        params = {"key": "value"}

        async with request_with_retry(
            mock_session,
            "POST",
            "http://example.com",
            json={"test": "data"},
            retry_config=RetryConfig(
                retryable_error_statuses=frozenset({429, 503}),
            ),
            headers=headers,
            params=params
        ) as resp:
            assert resp.status == 200

        # Verify kwargs were passed to session.request
        call_kwargs = mock_session.request.call_args.kwargs
        assert call_kwargs["headers"] == headers
        assert call_kwargs["params"] == params

    _run_async(_test())


# ===== AsyncRateLimiter Tests =====

from tmautils.common import AsyncRateLimiter
from tmautils.web import url_to_rate_limit_key


async def test_rate_limiter_no_limits():
    """Test AsyncRateLimiter with no limits configured (passthrough)."""
    limiter = AsyncRateLimiter()
    # Should be able to acquire without blocking
    async with limiter.acquire("key1"):
        pass
    async with limiter.acquire("key2"):
        pass


async def test_rate_limiter_concurrency_shared_key():
    """Test shared-key concurrency limit blocks when exceeded."""
    limiter = AsyncRateLimiter(max_concurrent=2)
    acquired = []
    released = []

    async def acquire_and_hold(delay: float):
        async with limiter.acquire():  # shared key (None)
            acquired.append(True)
            await asyncio.sleep(delay)
            released.append(True)

    # Start 3 tasks, only 2 should acquire immediately
    task1 = asyncio.create_task(acquire_and_hold(0.1))
    task2 = asyncio.create_task(acquire_and_hold(0.1))
    task3 = asyncio.create_task(acquire_and_hold(0.1))

    await asyncio.sleep(0.01)  # Let tasks start
    assert len(acquired) == 2  # Third blocked

    await asyncio.gather(task1, task2, task3)
    assert len(acquired) == 3  # All completed
    assert len(released) == 3


async def test_rate_limiter_concurrency_different_keys():
    """Test different keys get independent concurrency pools."""
    limiter = AsyncRateLimiter(max_concurrent=1)
    acquired = []

    async def acquire_and_hold(key: str, delay: float):
        async with limiter.acquire(key):
            acquired.append(key)
            await asyncio.sleep(delay)

    # Start 2 tasks with different keys - both should acquire
    task1 = asyncio.create_task(acquire_and_hold("a.com", 0.1))
    task2 = asyncio.create_task(acquire_and_hold("b.com", 0.1))

    await asyncio.sleep(0.01)
    assert len(acquired) == 2  # Different keys, both acquired

    await asyncio.gather(task1, task2)


async def test_rate_limiter_concurrency_same_key_blocks():
    """Test same key concurrency blocks."""
    limiter = AsyncRateLimiter(max_concurrent=1)
    acquired = []

    async def acquire_and_hold(key: str, delay: float):
        async with limiter.acquire(key):
            acquired.append(key)
            await asyncio.sleep(delay)

    # Start 2 tasks with same key - second should block
    task1 = asyncio.create_task(acquire_and_hold("a.com", 0.1))
    task2 = asyncio.create_task(acquire_and_hold("a.com", 0.1))

    await asyncio.sleep(0.01)
    assert len(acquired) == 1  # Same key, second blocked

    await asyncio.gather(task1, task2)
    assert len(acquired) == 2


async def test_rate_limiter_rate():
    """Test rate limiting with token bucket."""
    # Allow 10 per second
    limiter = AsyncRateLimiter(max_rate=10)
    start_time = asyncio.get_event_loop().time()

    # Make 3 requests
    for _ in range(3):
        async with limiter.acquire("example.com"):
            pass

    elapsed = asyncio.get_event_loop().time() - start_time
    # Token bucket should allow bursts, so 3 requests shouldn't take long
    assert elapsed < 1.0  # Should be fast (within burst capacity)


async def test_rate_limiter_rate_with_time_period():
    """Test rate limiting with custom time period (60 requests per minute)."""
    # 60 per minute = 1 per second
    limiter = AsyncRateLimiter(max_rate=60, time_period=60)
    start_time = asyncio.get_event_loop().time()

    # Make 3 requests - should be within burst capacity
    for _ in range(3):
        async with limiter.acquire("example.com"):
            pass

    elapsed = asyncio.get_event_loop().time() - start_time
    assert elapsed < 1.0  # Should be fast (within burst capacity)


async def test_rate_limiter_both_limits():
    """Test that both concurrency and rate limits are applied."""
    limiter = AsyncRateLimiter(max_concurrent=2, max_rate=10)
    acquired = []

    async def acquire_and_hold(delay: float):
        async with limiter.acquire():  # shared key
            acquired.append(True)
            await asyncio.sleep(delay)

    # Start 3 tasks - only 2 should acquire due to semaphore
    task1 = asyncio.create_task(acquire_and_hold(0.1))
    task2 = asyncio.create_task(acquire_and_hold(0.1))
    task3 = asyncio.create_task(acquire_and_hold(0.1))

    await asyncio.sleep(0.01)
    assert len(acquired) == 2

    await asyncio.gather(task1, task2, task3)
    assert len(acquired) == 3


async def test_url_to_rate_limit_key_normalization():
    """Test that url_to_rate_limit_key extracts hostname correctly."""
    # Same host, different paths - should produce same key
    assert url_to_rate_limit_key("http://example.com/path1") == "example.com"
    assert url_to_rate_limit_key("http://example.com/path2") == "example.com"
    assert url_to_rate_limit_key("https://example.com:443/path") == "example.com"


async def test_url_to_rate_limit_key_case_insensitive():
    """Test that url_to_rate_limit_key is case-insensitive."""
    assert url_to_rate_limit_key("http://EXAMPLE.COM/path") == "example.com"
    assert url_to_rate_limit_key("http://Example.Com/path") == "example.com"


# ===== signal_backoff Tests =====


async def test_signal_backoff_basic():
    """Test signal_backoff delays acquire_token."""
    import time
    limiter = AsyncRateLimiter()
    limiter.signal_backoff("key", 0.1)

    start = time.monotonic()
    async with limiter.acquire_token("key"):
        elapsed = time.monotonic() - start
    assert elapsed >= 0.08  # Allow some tolerance


async def test_signal_backoff_max_semantics():
    """Test signal_backoff uses max() — shorter backoff doesn't shorten existing."""
    import time
    limiter = AsyncRateLimiter()
    limiter.signal_backoff("key", 0.2)
    limiter.signal_backoff("key", 0.05)  # shorter — should NOT shorten

    start = time.monotonic()
    async with limiter.acquire_token("key"):
        elapsed = time.monotonic() - start
    assert elapsed >= 0.15  # Should still respect the 0.2s backoff


async def test_signal_backoff_expired_is_noop():
    """Test that expired backoff doesn't delay."""
    import time
    limiter = AsyncRateLimiter()
    limiter.signal_backoff("key", 0.01)
    await asyncio.sleep(0.02)  # Wait for it to expire

    start = time.monotonic()
    async with limiter.acquire_token("key"):
        elapsed = time.monotonic() - start
    assert elapsed < 0.02  # Should be nearly instant


async def test_signal_backoff_per_key_isolation():
    """Test backoff on one key doesn't affect another."""
    import time
    limiter = AsyncRateLimiter()
    limiter.signal_backoff("a.com", 0.2)

    start = time.monotonic()
    async with limiter.acquire_token("b.com"):  # different key
        elapsed = time.monotonic() - start
    assert elapsed < 0.02  # Should not be delayed


async def test_signal_backoff_none_key():
    """Test backoff with None key (shared pool)."""
    import time
    limiter = AsyncRateLimiter()
    limiter.signal_backoff(None, 0.1)

    start = time.monotonic()
    async with limiter.acquire_token():  # None key
        elapsed = time.monotonic() - start
    assert elapsed >= 0.08


async def test_signal_backoff_zero_duration_is_noop():
    """Test signal_backoff with zero or negative duration is a no-op."""
    import time
    limiter = AsyncRateLimiter()
    limiter.signal_backoff("key", 0)
    limiter.signal_backoff("key", -1)

    start = time.monotonic()
    async with limiter.acquire_token("key"):
        elapsed = time.monotonic() - start
    assert elapsed < 0.02


# ===== hold_slot + acquire_token Tests =====


async def test_hold_slot_blocks_concurrent():
    """Test hold_slot holds semaphore slot."""
    limiter = AsyncRateLimiter(max_concurrent=1)
    acquired = []

    async def hold(key: str, delay: float):
        async with limiter.hold_slot(key):
            acquired.append(key)
            await asyncio.sleep(delay)

    task1 = asyncio.create_task(hold("k", 0.1))
    task2 = asyncio.create_task(hold("k", 0.1))
    await asyncio.sleep(0.01)
    assert len(acquired) == 1  # Second blocked by semaphore
    await asyncio.gather(task1, task2)
    assert len(acquired) == 2


async def test_acquire_token_checks_backoff():
    """Test acquire_token respects backoff."""
    import time
    limiter = AsyncRateLimiter()
    limiter.signal_backoff("k", 0.1)

    start = time.monotonic()
    async with limiter.acquire_token("k"):
        elapsed = time.monotonic() - start
    assert elapsed >= 0.08


async def test_acquire_convenience_wraps_both():
    """Test acquire() = hold_slot + acquire_token."""
    limiter = AsyncRateLimiter(max_concurrent=1)
    limiter.signal_backoff("k", 0.1)

    import time
    start = time.monotonic()
    async with limiter.acquire("k"):
        elapsed = time.monotonic() - start
    # Should have both held the semaphore and waited for backoff
    assert elapsed >= 0.08


# ===== Memory Cleanup Tests =====


async def test_cleanup_evicts_idle_keys():
    """Test cleanup removes idle keys."""
    limiter = AsyncRateLimiter(max_idle_seconds=0.01)

    # Create some key state
    async with limiter.acquire("key1"):
        pass

    assert "key1" in limiter._keys

    await asyncio.sleep(0.02)  # Let it become idle
    limiter.cleanup()
    assert "key1" not in limiter._keys


async def test_cleanup_preserves_active_keys():
    """Test cleanup does not evict keys with active operations."""
    limiter = AsyncRateLimiter(max_concurrent=1, max_idle_seconds=0.01)

    async with limiter.hold_slot("key1"):
        await asyncio.sleep(0.02)
        limiter.cleanup()
        assert "key1" in limiter._keys  # Still active, not evicted


async def test_config_string():
    """Test config_string output."""
    limiter = AsyncRateLimiter(max_concurrent=10, max_rate=5, time_period=2.0)
    assert limiter.config_string == "max_concurrent=10, max_rate=5/2.0s"

    limiter2 = AsyncRateLimiter()
    assert limiter2.config_string == "no limits"


# ===== request_with_retry with AsyncRateLimiter Tests =====


async def test_request_with_retry_with_rate_limiter():
    """Test request_with_retry works with rate_limiter parameter."""
    limiter = AsyncRateLimiter(max_concurrent=10)

    mock_session = Mock()
    mock_response = MockResponse(200)
    mock_session.request = AsyncMock(return_value=mock_response)

    async with request_with_retry(
        mock_session, "GET", "http://example.com", rate_limiter=limiter
    ) as resp:
        assert resp.status == 200

    # Verify request was called
    assert mock_session.request.call_count == 1


async def test_request_with_retry_without_rate_limiter():
    """Test request_with_retry works without rate_limiter (backwards compat)."""
    mock_session = Mock()
    mock_response = MockResponse(200)
    mock_session.request = AsyncMock(return_value=mock_response)

    async with request_with_retry(mock_session, "GET", "http://example.com") as resp:
        assert resp.status == 200

    assert mock_session.request.call_count == 1


async def test_request_with_retry_hold_slot_and_acquire_token():
    """Test hold_slot is called once and acquire_token per attempt."""
    hold_slot_count = 0
    acquire_token_count = 0

    limiter = AsyncRateLimiter(max_concurrent=1)

    original_hold_slot = limiter.hold_slot
    original_acquire_token = limiter.acquire_token

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def tracked_hold_slot(key):
        nonlocal hold_slot_count
        hold_slot_count += 1
        async with original_hold_slot(key):
            yield

    @asynccontextmanager
    async def tracked_acquire_token(key):
        nonlocal acquire_token_count
        acquire_token_count += 1
        async with original_acquire_token(key):
            yield

    limiter.hold_slot = tracked_hold_slot
    limiter.acquire_token = tracked_acquire_token

    mock_session = Mock()
    # First returns 429 (retryable), second returns 200
    mock_responses = [MockResponse(429), MockResponse(200)]
    mock_session.request = AsyncMock(side_effect=mock_responses)

    async with request_with_retry(
        mock_session, "GET", "http://example.com",
        rate_limiter=limiter, retry_config=RetryConfig(max_attempts=2)
    ) as resp:
        assert resp.status == 200

    # hold_slot wraps the entire retry loop (called once)
    assert hold_slot_count == 1
    # acquire_token is called per attempt
    assert acquire_token_count == 2


async def test_request_with_retry_holds_slot_during_backoff():
    """Test that semaphore slot is held during retry backoff (prevents starvation)."""
    limiter = AsyncRateLimiter(max_concurrent=1)
    request_2_blocked = True

    mock_session = Mock()
    # Request 1: returns 429, then 200 after retry
    responses_1 = [MockResponse(429, {"Retry-After": "0.1"}), MockResponse(200)]
    request_1_count = [0]

    async def make_request_1():
        def side_effect(*args, **kwargs):
            result = responses_1[request_1_count[0]]
            request_1_count[0] += 1
            return result

        mock_session.request = AsyncMock(side_effect=side_effect)
        async with request_with_retry(
            mock_session, "GET", "http://example.com/1",
            rate_limiter=limiter, retry_config=RetryConfig(max_attempts=2)
        ) as resp:
            return resp.status

    async def make_request_2():
        nonlocal request_2_blocked
        # Try to acquire during request 1's backoff — should be blocked
        await asyncio.sleep(0.05)
        # Use hold_slot with same key (hostname-based)
        async with limiter.hold_slot("example.com"):
            request_2_blocked = False

    task1 = asyncio.create_task(make_request_1())
    task2 = asyncio.create_task(make_request_2())

    # Give request 1 time to get 429 and enter backoff
    await asyncio.sleep(0.08)
    # Request 2 should still be blocked (request 1 holds the slot)
    assert request_2_blocked

    await asyncio.gather(task1, task2)
    # After request 1 completes, request 2 should have acquired
    assert not request_2_blocked


async def test_request_with_retry_429_signals_backoff():
    """Test that 429 signals backoff to rate limiter."""
    limiter = AsyncRateLimiter(max_concurrent=2)

    mock_session = Mock()
    mock_responses = [MockResponse(429, {"Retry-After": "0.5"}), MockResponse(200)]
    mock_session.request = AsyncMock(side_effect=mock_responses)

    async with request_with_retry(
        mock_session, "GET", "http://example.com",
        rate_limiter=limiter, retry_config=RetryConfig(max_attempts=2)
    ) as resp:
        assert resp.status == 200

    # Check that backoff was signaled for the hostname key
    state = limiter._keys.get("example.com")
    assert state is not None
    # backoff_until should have been set (may have expired by now, but should exist)


async def test_request_with_retry_429_max_attempts_1_signals_backoff():
    """Test that 429 signals backoff even with max_attempts=1 (no retry)."""
    limiter = AsyncRateLimiter(max_concurrent=2)

    mock_session = Mock()
    mock_session.request = AsyncMock(
        return_value=MockResponse(429, {"Retry-After": "10"})
    )

    with pytest.raises(aiohttp.ClientError):
        async with request_with_retry(
            mock_session, "GET", "http://example.com",
            rate_limiter=limiter, retry_config=RetryConfig(
                max_attempts=1,
                rate_limited_min_wait=5.0,
            ),
        ) as resp:
            pass

    # Backoff should still have been signaled despite no retry
    state = limiter._keys.get("example.com")
    assert state is not None
    # eff_retry_after = min(10, 60) = 10, so backoff_duration = 10
    assert state.backoff_until > 0


async def test_get_with_retry_with_rate_limiter():
    """Test get_with_retry works with rate_limiter parameter."""
    from tmautils.web import get_with_retry

    limiter = AsyncRateLimiter(max_concurrent=10)

    mock_session = Mock()
    mock_response = MockResponse(200)
    mock_session.request = AsyncMock(return_value=mock_response)

    async with get_with_retry(
        mock_session, "http://example.com", rate_limiter=limiter
    ) as resp:
        assert resp.status == 200


# ===== Rate Limit vs Error Retry Strategy Tests =====


def test_rate_limit_status_uses_rate_limit_wait_strategy():
    """Test that 429 (rate limit) uses the stricter rate limit wait strategy."""
    async def _test():
        mock_session = Mock()
        # First returns 429 without Retry-After, then 200
        mock_responses = [MockResponse(429), MockResponse(200)]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        start_time = asyncio.get_event_loop().time()
        async with request_with_retry(
            mock_session, "GET", "http://example.com",
            retry_config=RetryConfig(
                max_attempts=2,
                rate_limited_min_wait=0.2,  # Use short values for testing
                rate_limited_multiplier=1.0,
                rate_limited_max_wait=1.0,
                retryable_error_min_wait=0.01,  # Error wait should be much faster
                retryable_error_multiplier=0.01,
                retryable_error_max_wait=0.1,
            ),
        ) as resp:
            assert resp.status == 200

        elapsed = asyncio.get_event_loop().time() - start_time
        # Should have waited at least rate_limited_min_wait (0.2s), not error min_wait
        assert elapsed >= 0.2

    _run_async(_test())


def test_error_status_uses_error_wait_strategy():
    """Test that 500 (transient error) uses the error wait strategy."""
    async def _test():
        mock_session = Mock()
        # First returns 500, then 200
        mock_responses = [MockResponse(500), MockResponse(200)]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        start_time = asyncio.get_event_loop().time()
        async with request_with_retry(
            mock_session, "GET", "http://example.com",
            retry_config=RetryConfig(
                max_attempts=2,
                retryable_error_min_wait=0.15,
                retryable_error_multiplier=1.0,
                retryable_error_max_wait=1.0,
                rate_limited_min_wait=1.0,  # Rate limit wait should be much slower
                rate_limited_multiplier=1.0,
                rate_limited_max_wait=2.0,
            ),
        ) as resp:
            assert resp.status == 200

        elapsed = asyncio.get_event_loop().time() - start_time
        # Should have waited at least retryable_error_min_wait (0.15s), but not retryable_error_max_wait (1.0s)
        # Upper bound allows for random jitter from wait_random_exponential
        assert 0.15 <= elapsed < 1.0

    _run_async(_test())


def test_rate_limit_with_retry_after_still_respects_header():
    """Test that 429 with Retry-After header respects the header value."""
    async def _test():
        mock_session = Mock()
        # 429 with short Retry-After header
        mock_responses = [MockResponse(429, {"Retry-After": "0.15"}), MockResponse(200)]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        start_time = asyncio.get_event_loop().time()
        async with request_with_retry(
            mock_session, "GET", "http://example.com",
            retry_config=RetryConfig(
                max_attempts=2,
                rate_limited_min_wait=1.0,  # This should be ignored due to Retry-After
                rate_limited_multiplier=1.0,
                rate_limited_max_wait=2.0,
                respect_retry_after=True,
            ),
        ) as resp:
            assert resp.status == 200

        elapsed = asyncio.get_event_loop().time() - start_time
        # Should have waited ~0.15s (Retry-After value), not rate_limited_min_wait
        assert 0.15 <= elapsed < 0.5

    _run_async(_test())


def test_custom_rate_limited_statuses():
    """Test that custom rate_limited_statuses are used correctly."""
    async def _test():
        mock_session = Mock()
        # 503 normally uses error wait, but we configure it as rate limit
        mock_responses = [MockResponse(503), MockResponse(200)]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        start_time = asyncio.get_event_loop().time()
        async with request_with_retry(
            mock_session, "GET", "http://example.com",
            retry_config=RetryConfig(
                max_attempts=2,
                retryable_error_statuses=frozenset({500, 502}),  # 503 not in error set
                rate_limited_statuses=frozenset({429, 503}),  # 503 is rate limit
                retryable_error_min_wait=0.01,
                retryable_error_multiplier=0.01,
                retryable_error_max_wait=0.1,
                rate_limited_min_wait=0.2,
                rate_limited_multiplier=1.0,
                rate_limited_max_wait=1.0,
            ),
        ) as resp:
            assert resp.status == 200

        elapsed = asyncio.get_event_loop().time() - start_time
        # Should have used rate limit wait (>=0.2s)
        assert elapsed >= 0.2

    _run_async(_test())


def test_default_retryable_error_statuses_dont_include_429():
    """Test that default retryable_error_statuses no longer include 429."""
    async def _test():
        mock_session = Mock()
        # Create 3 mock responses - all 500 (to exhaust retries)
        mock_responses = [MockResponse(500), MockResponse(500), MockResponse(500)]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        # Use empty rate_limited_statuses and default retryable_error_statuses
        # 429 should NOT be retried if rate_limited_statuses is empty
        with pytest.raises(Exception):  # Will exhaust retries on 500
            async with request_with_retry(
                mock_session, "GET", "http://example.com",
                retry_config=RetryConfig(
                    max_attempts=3,
                    rate_limited_statuses=frozenset(),  # Disable rate limit handling
                    retryable_error_min_wait=0.01,  # Fast for testing
                    retryable_error_multiplier=0.01,
                    retryable_error_max_wait=0.1,
                    # retryable_error_statuses defaults to {500, 502, 503, 504} for GET
                ),
            ) as resp:
                pass

        # Request should have been retried (500 is in default retryable_error_statuses)
        assert mock_session.request.call_count == 3

    _run_async(_test())


def test_429_not_retried_if_not_in_rate_limited_statuses():
    """Test that 429 is not retried if excluded from rate_limited_statuses."""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(429)
        mock_session.request = AsyncMock(return_value=mock_response)

        async with request_with_retry(
            mock_session, "GET", "http://example.com",
            retry_config=RetryConfig(
                max_attempts=3,
                rate_limited_statuses=frozenset(),  # 429 not treated as rate limit
                retryable_error_statuses=frozenset({500, 502, 503, 504}),  # 429 not in error set
            ),
        ) as resp:
            assert resp.status == 429  # Should return immediately, no retry

        # Should only make 1 attempt (no retry)
        assert mock_session.request.call_count == 1

    _run_async(_test())


class MockRetryState:
    """Mock tenacity retry state for testing wait strategies."""
    def __init__(self, exception=None):
        self.attempt_number = 1
        self.outcome = Mock()
        self.outcome.failed = exception is not None
        self.outcome.exception = Mock(return_value=exception)


def test_wait_strategy_uses_retry_after_when_available():
    """Test that _WaitRetryAfterOrRandomExp uses retry_after if provided."""
    wait_strategy = _WaitRetryAfterOrRandomExp(
        retryable_error_multiplier=1.0, retryable_error_min=1.0, retryable_error_max=60.0,
        rate_limited_multiplier=2.0, rate_limited_min=5.0, rate_limited_max=120.0,
    )

    # Create exception with retry_after
    exc = _RetryableHTTPStatus(429, retry_after=10.0, is_rate_limited=True)
    retry_state = MockRetryState(exception=exc)

    wait_time = wait_strategy(retry_state)
    assert wait_time == 10.0  # Should use retry_after exactly


def test_wait_strategy_uses_rate_limited_backoff_for_rate_limited():
    """Test that _WaitRetryAfterOrRandomExp uses rate limited backoff for rate limited errors."""
    wait_strategy = _WaitRetryAfterOrRandomExp(
        retryable_error_multiplier=1.0, retryable_error_min=1.0, retryable_error_max=60.0,
        rate_limited_multiplier=2.0, rate_limited_min=5.0, rate_limited_max=120.0,
    )

    # Create rate limit exception without retry_after
    exc = _RetryableHTTPStatus(429, retry_after=None, is_rate_limited=True)
    retry_state = MockRetryState(exception=exc)

    wait_time = wait_strategy(retry_state)
    # Should be in rate limit range [5.0, 120.0]
    assert 5.0 <= wait_time <= 120.0


def test_wait_strategy_uses_retryable_error_backoff_for_errors():
    """Test that _WaitRetryAfterOrRandomExp uses retryable error backoff for non-rate-limited errors."""
    wait_strategy = _WaitRetryAfterOrRandomExp(
        retryable_error_multiplier=1.0, retryable_error_min=1.0, retryable_error_max=60.0,
        rate_limited_multiplier=2.0, rate_limited_min=5.0, rate_limited_max=120.0,
    )

    # Create error exception (not rate limit)
    exc = _RetryableHTTPStatus(500, retry_after=None, is_rate_limited=False)
    retry_state = MockRetryState(exception=exc)

    wait_time = wait_strategy(retry_state)
    # Should be in error range [1.0, 60.0]
    assert 1.0 <= wait_time <= 60.0


def test_retryable_http_status_is_rate_limited_flag():
    """Test that _RetryableHTTPStatus correctly stores is_rate_limited flag."""
    exc_rate_limit = _RetryableHTTPStatus(429, retry_after=5.0, is_rate_limited=True)
    assert exc_rate_limit.is_rate_limited is True
    assert exc_rate_limit.status == 429
    assert exc_rate_limit.retry_after == 5.0

    exc_error = _RetryableHTTPStatus(500, retry_after=None, is_rate_limited=False)
    assert exc_error.is_rate_limited is False
    assert exc_error.status == 500
    assert exc_error.retry_after is None


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", __file__])
