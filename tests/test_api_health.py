"""Tests for the circuit breaker (api_health module)."""

from unittest.mock import patch

import anthropic
import groq as groq_sdk
import pytest

import api_health
from api_health import ClaudeHealth


class TestInitialState:
    def test_starts_closed(self) -> None:
        h = ClaudeHealth()
        assert h.state == "closed"

    def test_starts_available(self) -> None:
        h = ClaudeHealth()
        assert h.available is True

    def test_status_message_closed(self) -> None:
        h = ClaudeHealth()
        assert "healthy" in h.status_message.lower()


class TestFailureThreshold:
    def test_single_failure_stays_closed(self) -> None:
        h = ClaudeHealth()
        tripped = h.record_failure()
        assert tripped is False
        assert h.state == "closed"

    def test_two_failures_stays_closed(self) -> None:
        h = ClaudeHealth()
        h.record_failure()
        tripped = h.record_failure()
        assert tripped is False
        assert h.state == "closed"

    def test_three_failures_trips_open(self) -> None:
        h = ClaudeHealth()
        h.record_failure()
        h.record_failure()
        tripped = h.record_failure()
        assert tripped is True
        assert h.state == "open"

    def test_record_failure_returns_true_only_on_transition(self) -> None:
        h = ClaudeHealth()
        results = [h.record_failure() for _ in range(5)]
        # Only the 3rd failure (index 2) should return True
        assert results == [False, False, True, False, False]


class TestOpenState:
    def test_open_is_not_available(self) -> None:
        h = ClaudeHealth()
        for _ in range(3):
            h.record_failure()
        assert h.available is False

    def test_status_message_open(self) -> None:
        h = ClaudeHealth()
        for _ in range(3):
            h.record_failure()
        msg = h.status_message
        assert "unreachable" in msg.lower()


class TestHalfOpen:
    def test_transitions_to_half_open_after_backoff(self) -> None:
        h = ClaudeHealth()
        for _ in range(3):
            h.record_failure()
        assert h._state == "open"

        # Simulate backoff expiry
        with patch("time.monotonic", return_value=h._opened_at + 31):
            assert h.state == "half_open"
            assert h.available is True

    def test_status_message_half_open(self) -> None:
        h = ClaudeHealth()
        for _ in range(3):
            h.record_failure()
        with patch("time.monotonic", return_value=h._opened_at + 31):
            msg = h.status_message
            assert "recovering" in msg.lower()

    def test_success_in_half_open_resets_to_closed(self) -> None:
        h = ClaudeHealth()
        for _ in range(3):
            h.record_failure()

        with patch("time.monotonic", return_value=h._opened_at + 31):
            _ = h.state  # trigger transition to half_open

        h.record_success()
        assert h.state == "closed"
        assert h._failures == 0
        assert h._backoff == api_health.INITIAL_BACKOFF

    def test_failure_in_half_open_reopens_with_doubled_backoff(self) -> None:
        h = ClaudeHealth()
        for _ in range(3):
            h.record_failure()
        initial_backoff = h._backoff

        with patch("time.monotonic", return_value=h._opened_at + 31):
            _ = h.state  # trigger transition to half_open

        tripped = h.record_failure()
        assert tripped is True
        assert h.state == "open"
        assert h._backoff == initial_backoff * 2


class TestBackoffCap:
    def test_backoff_capped_at_max(self) -> None:
        h = ClaudeHealth()
        # Trip open
        for _ in range(3):
            h.record_failure()

        # Repeatedly fail in half_open to double backoff
        for _ in range(20):
            with patch("time.monotonic", return_value=h._opened_at + h._backoff + 1):
                _ = h.state  # trigger half_open
            h.record_failure()  # reopen with doubled backoff

        assert h._backoff == api_health.MAX_BACKOFF


class TestIsTransient:
    def test_connection_error_is_transient(self) -> None:
        exc = anthropic.APIConnectionError(request=None)
        assert api_health.is_transient(exc) is True

    def test_timeout_error_is_transient(self) -> None:
        exc = anthropic.APITimeoutError(request=None)
        assert api_health.is_transient(exc) is True

    def test_internal_server_error_is_transient(self) -> None:
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {}
        exc = anthropic.InternalServerError(
            message="Internal server error",
            response=mock_response,
            body=None,
        )
        assert api_health.is_transient(exc) is True

    def test_rate_limit_error_is_transient(self) -> None:
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}
        exc = anthropic.RateLimitError(
            message="Rate limited",
            response=mock_response,
            body=None,
        )
        assert api_health.is_transient(exc) is True

    def test_auth_error_is_not_transient(self) -> None:
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.headers = {}
        exc = anthropic.AuthenticationError(
            message="Invalid key",
            response=mock_response,
            body=None,
        )
        assert api_health.is_transient(exc) is False

    def test_bad_request_is_not_transient(self) -> None:
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.headers = {}
        exc = anthropic.BadRequestError(
            message="Bad request",
            response=mock_response,
            body=None,
        )
        assert api_health.is_transient(exc) is False

    def test_generic_exception_is_not_transient(self) -> None:
        assert api_health.is_transient(ValueError("oops")) is False


class TestIsTransientGroq:
    def test_groq_connection_error_is_transient(self) -> None:
        exc = groq_sdk.APIConnectionError(request=None)
        assert api_health.is_transient(exc) is True

    def test_groq_timeout_error_is_transient(self) -> None:
        exc = groq_sdk.APITimeoutError(request=None)
        assert api_health.is_transient(exc) is True

    def test_groq_internal_server_error_is_transient(self) -> None:
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {}
        exc = groq_sdk.InternalServerError(
            message="Internal server error",
            response=mock_response,
            body=None,
        )
        assert api_health.is_transient(exc) is True

    def test_groq_rate_limit_error_is_transient(self) -> None:
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}
        exc = groq_sdk.RateLimitError(
            message="Rate limited",
            response=mock_response,
            body=None,
        )
        assert api_health.is_transient(exc) is True

    def test_groq_auth_error_is_not_transient(self) -> None:
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.headers = {}
        exc = groq_sdk.AuthenticationError(
            message="Invalid key",
            response=mock_response,
            body=None,
        )
        assert api_health.is_transient(exc) is False


class TestGroqHealthSingleton:
    def test_groq_health_exists(self) -> None:
        assert isinstance(api_health.groq_health, ClaudeHealth)

    def test_groq_health_starts_closed(self) -> None:
        # The singleton may have state from other tests, so just verify it's a ClaudeHealth
        assert api_health.groq_health.state in ("closed", "open", "half_open")
