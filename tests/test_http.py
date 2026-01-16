import pytest
import asyncio
from unittest.mock import AsyncMock, Mock
from datetime import datetime, timezone, timedelta
import aiohttp

from tmautils.web import get_with_retry, request_with_retry
from tmautils.web.http import _parse_retry_after


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
            mock_session, "http://example.com", max_attempts=3
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
            mock_session, "http://example.com", max_attempts=2
        ) as resp:
            assert resp.status == 200

        assert mock_session.request.call_count == 2
        assert mock_responses[0].release_called

    _run_async(_test())


def test_non_retryable_status_returned():
    """Test non-retryable status (404, 403) doesn't trigger retry"""
    # Note: 500 and 502 are in default retry_statuses for GET, so only test non-retryable statuses
    for status in [404, 403, 400, 401]:
        async def _test():
            mock_session = Mock()
            mock_response = MockResponse(status)
            mock_session.request = AsyncMock(return_value=mock_response)

            async with get_with_retry(
                mock_session, "http://example.com", max_attempts=3
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
            mock_session, "http://example.com", max_attempts=2
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
            mock_session, "http://example.com", max_attempts=2
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
                mock_session, "http://example.com", max_attempts=3
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
                mock_session, "http://example.com", max_attempts=3
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
                mock_session, "http://example.com", max_attempts=3
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
            max_attempts=2,
            respect_retry_after=True,
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
            max_attempts=2,
            respect_retry_after=False,
            retry_multiplier=0.01,  # Very small for fast test
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
            max_attempts=2,
            respect_retry_after=True,
            max_retry_after=0.2,  # Cap at 0.2 seconds
        ) as resp:
            assert resp.status == 200

        elapsed = asyncio.get_event_loop().time() - start_time
        # Should have waited ~0.2 seconds, not 1000
        assert 0.2 <= elapsed < 10

    _run_async(_test())


def test_custom_retry_statuses():
    """Test custom retry_statuses parameter"""
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
            max_attempts=2,
            retry_statuses=frozenset({404}),
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
                mock_session, method, "http://example.com", max_attempts=2
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
                mock_session, method, "http://example.com", max_attempts=3
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
            mock_session, "POST", "http://example.com", max_attempts=2
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
            mock_session, "POST", "http://example.com", max_attempts=2
        ) as resp:
            assert resp.status == 200

        # Should have retried on 503
        assert mock_session.request.call_count == 2

    _run_async(_test())


def test_request_with_retry_custom_retry_statuses_override():
    """Test that custom retry_statuses override method-specific defaults"""
    async def _test():
        mock_session = Mock()
        # POST with 500 and custom retry_statuses that includes 500
        mock_responses = [MockResponse(500), MockResponse(200)]
        mock_session.request = AsyncMock(side_effect=mock_responses)

        async with request_with_retry(
            mock_session,
            "POST",
            "http://example.com",
            max_attempts=2,
            retry_statuses=frozenset({429, 500, 502, 503, 504})
        ) as resp:
            assert resp.status == 200

        # Should have retried on 500 because of custom retry_statuses
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
            max_attempts=2,
            retry_statuses=frozenset({429, 503})
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
                max_attempts=3,
                retry_statuses=frozenset({429, 503})
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
            max_attempts=2,
            retry_statuses=frozenset({429, 503})
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
            max_attempts=2,
            retry_statuses=frozenset({429, 503})
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
            max_attempts=2,
            retry_statuses=frozenset({429, 503})
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
            retry_statuses=frozenset({429, 503}),
            headers=headers,
            params=params
        ) as resp:
            assert resp.status == 200

        # Verify kwargs were passed to session.request
        call_kwargs = mock_session.request.call_args.kwargs
        assert call_kwargs["headers"] == headers
        assert call_kwargs["params"] == params

    _run_async(_test())


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", __file__])
