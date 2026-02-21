"""Lightweight circuit breaker tracking Claude API availability.

Three states:
- ``"closed"``   — API healthy, all calls proceed
- ``"open"``     — API down, calls rejected immediately (fast path)
- ``"half_open"``— backoff expired, next call is a recovery probe

Only *connectivity* errors trip the breaker (``APIConnectionError``,
``APITimeoutError``, ``InternalServerError``, ``RateLimitError``).
Auth / bad-request errors do **not** trip it — the API is reachable,
the problem is on our side.
"""

import time

import anthropic
import groq as groq_sdk

FAILURE_THRESHOLD: int = 3
INITIAL_BACKOFF: float = 30.0
MAX_BACKOFF: float = 300.0

# Errors that indicate the API is unreachable or overloaded
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
    anthropic.RateLimitError,
    groq_sdk.APIConnectionError,
    groq_sdk.APITimeoutError,
    groq_sdk.InternalServerError,
    groq_sdk.RateLimitError,
)


def is_transient(exc: Exception) -> bool:
    """Return ``True`` if *exc* should be treated as a connectivity failure."""
    return isinstance(exc, TRANSIENT_ERRORS)


class ClaudeHealth:
    """Circuit breaker for the Claude API."""

    def __init__(self) -> None:
        self._state: str = "closed"
        self._failures: int = 0
        self._backoff: float = INITIAL_BACKOFF
        self._opened_at: float = 0.0

    # -- public properties ---------------------------------------------------

    @property
    def state(self) -> str:
        # Auto-transition open → half_open when backoff expires
        if self._state == "open":
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._backoff:
                self._state = "half_open"
        return self._state

    @property
    def available(self) -> bool:
        """Should a call be attempted right now?"""
        return self.state != "open"

    @property
    def status_message(self) -> str:
        """Human-readable status string for Discord."""
        s = self.state
        if s == "closed":
            return "Claude API is healthy."
        if s == "open":
            remaining = self._backoff - (time.monotonic() - self._opened_at)
            return (
                f"Claude API is unreachable. "
                f"Next retry in {max(0, int(remaining))}s."
            )
        # half_open
        return "Claude API is recovering — testing with next request."

    # -- recording outcomes --------------------------------------------------

    def record_success(self) -> None:
        """Reset the circuit to *closed* on a successful API call."""
        self._state = "closed"
        self._failures = 0
        self._backoff = INITIAL_BACKOFF

    def record_failure(self) -> bool:
        """Record a connectivity failure.

        Returns ``True`` if this failure tripped the circuit **open**
        (i.e. we just crossed the threshold), ``False`` otherwise.
        """
        if self._state == "half_open":
            # Probe failed — reopen with doubled backoff
            self._backoff = min(self._backoff * 2, MAX_BACKOFF)
            self._state = "open"
            self._opened_at = time.monotonic()
            return True

        if self._state == "open":
            # Already open — nothing to do
            return False

        # closed state
        self._failures += 1
        if self._failures >= FAILURE_THRESHOLD:
            self._state = "open"
            self._opened_at = time.monotonic()
            return True
        return False


# Module-level singletons
claude_health = ClaudeHealth()
groq_health = ClaudeHealth()
