import pytest
import asyncio
from unittest.mock import AsyncMock, Mock
from datetime import datetime, timezone, timedelta
import aiohttp

from tmautils.web import aget_with_retry
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
        mock_session.get = AsyncMock(return_value=mock_response)

        async with aget_with_retry(mock_session, "http://example.com") as resp:
            assert resp.status == 200
            assert not resp.release_called  # Not released yet during processing

        # After context exits, response should be released
        assert mock_response.release_called
        assert mock_session.get.call_count == 1

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
        mock_session.get = AsyncMock(side_effect=mock_responses)

        async with aget_with_retry(
            mock_session, "http://example.com", max_attempts=3
        ) as resp:
            assert resp.status == 200

        # Should have made 3 attempts
        assert mock_session.get.call_count == 3
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
        mock_session.get = AsyncMock(side_effect=mock_responses)

        async with aget_with_retry(
            mock_session, "http://example.com", max_attempts=2
        ) as resp:
            assert resp.status == 200

        assert mock_session.get.call_count == 2
        assert mock_responses[0].release_called

    _run_async(_test())


def test_non_retryable_status_returned():
    """Test non-retryable status (404, 500) doesn't trigger retry"""
    for status in [404, 500, 403, 502]:
        async def _test():
            mock_session = Mock()
            mock_response = MockResponse(status)
            mock_session.get = AsyncMock(return_value=mock_response)

            async with aget_with_retry(
                mock_session, "http://example.com", max_attempts=3
            ) as resp:
                assert resp.status == status

            # Should only make 1 attempt (no retry)
            assert mock_session.get.call_count == 1
            assert mock_response.release_called

        _run_async(_test())


def test_network_error_triggers_retry():
    """Test network errors (ClientError) trigger retry"""
    async def _test():
        mock_session = Mock()
        # First call raises error, second succeeds
        mock_session.get = AsyncMock(
            side_effect=[
                aiohttp.ClientError("Connection failed"),
                MockResponse(200),
            ]
        )

        async with aget_with_retry(
            mock_session, "http://example.com", max_attempts=2
        ) as resp:
            assert resp.status == 200

        assert mock_session.get.call_count == 2

    _run_async(_test())


def test_timeout_error_triggers_retry():
    """Test timeout errors trigger retry"""
    async def _test():
        mock_session = Mock()
        mock_session.get = AsyncMock(
            side_effect=[
                asyncio.TimeoutError(),
                MockResponse(200),
            ]
        )

        async with aget_with_retry(
            mock_session, "http://example.com", max_attempts=2
        ) as resp:
            assert resp.status == 200

        assert mock_session.get.call_count == 2

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
        mock_session.get = AsyncMock(return_value=mock_response)

        # Caller code raises aiohttp.ClientError while processing response
        with pytest.raises(aiohttp.ClientError, match="Caller error"):
            async with aget_with_retry(
                mock_session, "http://example.com", max_attempts=3
            ) as resp:
                # Simulating caller code that fails while processing response
                raise aiohttp.ClientError("Caller error")

        # Should only make 1 HTTP request (no retry)
        assert mock_session.get.call_count == 1
        # Response should still be released
        assert mock_response.release_called

    _run_async(_test())


def test_caller_timeout_exception_no_retry():
    """Test that TimeoutError from caller code doesn't trigger retry"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.get = AsyncMock(return_value=mock_response)

        with pytest.raises(asyncio.TimeoutError):
            async with aget_with_retry(
                mock_session, "http://example.com", max_attempts=3
            ) as resp:
                # Simulating caller code that times out
                raise asyncio.TimeoutError()

        # Should only make 1 HTTP request (no retry)
        assert mock_session.get.call_count == 1
        assert mock_response.release_called

    _run_async(_test())


def test_max_retries_exhausted():
    """Test that max retries exhausted raises the last exception"""
    async def _test():
        mock_session = Mock()
        mock_session.get = AsyncMock(
            side_effect=[
                aiohttp.ClientError("Error 1"),
                aiohttp.ClientError("Error 2"),
                aiohttp.ClientError("Error 3"),
            ]
        )

        with pytest.raises(aiohttp.ClientError, match="Error 3"):
            async with aget_with_retry(
                mock_session, "http://example.com", max_attempts=3
            ) as resp:
                pass

        assert mock_session.get.call_count == 3

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
        mock_session.get = AsyncMock(side_effect=mock_responses)

        start_time = asyncio.get_event_loop().time()
        async with aget_with_retry(
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
        mock_session.get = AsyncMock(side_effect=mock_responses)

        start_time = asyncio.get_event_loop().time()
        async with aget_with_retry(
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
        mock_session.get = AsyncMock(side_effect=mock_responses)

        start_time = asyncio.get_event_loop().time()
        async with aget_with_retry(
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
        mock_session.get = AsyncMock(side_effect=mock_responses)

        async with aget_with_retry(
            mock_session,
            "http://example.com",
            max_attempts=2,
            retry_statuses=frozenset({404}),
        ) as resp:
            assert resp.status == 200

        assert mock_session.get.call_count == 2

    _run_async(_test())


def test_request_kwargs_passed_through():
    """Test that additional request kwargs are passed to session.get"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.get = AsyncMock(return_value=mock_response)

        headers = {"User-Agent": "test"}
        params = {"key": "value"}

        async with aget_with_retry(
            mock_session,
            "http://example.com",
            headers=headers,
            params=params,
        ) as resp:
            assert resp.status == 200

        # Verify kwargs were passed to session.get
        call_kwargs = mock_session.get.call_args.kwargs
        assert call_kwargs["headers"] == headers
        assert call_kwargs["params"] == params

    _run_async(_test())


def test_resource_cleanup_on_success():
    """Test that response is properly released after successful use"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.get = AsyncMock(return_value=mock_response)

        async with aget_with_retry(mock_session, "http://example.com") as resp:
            assert not resp.release_called

        # After exiting context, response should be released
        assert mock_response.release_called

    _run_async(_test())


def test_resource_cleanup_on_caller_exception():
    """Test that response is released even when caller raises exception"""
    async def _test():
        mock_session = Mock()
        mock_response = MockResponse(200)
        mock_session.get = AsyncMock(return_value=mock_response)

        with pytest.raises(ValueError):
            async with aget_with_retry(mock_session, "http://example.com") as resp:
                raise ValueError("Test error")

        # Response should be released even though exception was raised
        assert mock_response.release_called

    _run_async(_test())


if __name__ == "__main__":
    pytest.main(["-vv", "-rA", __file__])
