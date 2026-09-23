"""What the API can refuse, as types you can branch on.

The distinction this module exists for is 402 against 429. They get confused constantly and
the correct client behaviour is opposite: retry a rate limit, and stop dead on a quota. A
client that retries both hammers the second one forever and never succeeds.
"""

from __future__ import annotations

from typing import Any


class ToxicFilterError(Exception):
    """Anything the API refused."""

    #: Whether asking again could plausibly work.
    retryable = False

    # The status, the payload and the code travel with the error because branching on a
    # message is what a client does when the exception does not carry them.
    def __init__(
        self,
        message: str,
        status: int = 0,
        code: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Build the error with what the response said about it."""
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.payload = payload or {}


class AuthenticationError(ToxicFilterError):
    """The key was missing, unrecognised or revoked. A wrong key stays wrong."""


class QuotaExhausted(ToxicFilterError):
    """The account cannot pay for this call.

    **Never retried by anything in this library.** Catch it, stop calling and tell
    somebody: no amount of waiting produces credits.
    """

    @property
    def remaining(self) -> int:
        """Credits left on the account."""
        return int(self.payload.get("credits", {}).get("remaining", 0))

    @property
    def required(self) -> int:
        """What this call would have cost.

        An estimate, and a 402 can arrive with credits still in the account. A check costs
        one credit and a model reading adds the tokens it used, rounded up, which is only
        known afterwards; so the call is priced beforehand at a typical reading (about 8
        credits for a comment and 10 for an image) and refused when that does not fit.
        """
        return int(self.payload.get("credits", {}).get("required", 0))

    @property
    def renews_at(self) -> str | None:
        """When the monthly allowance comes back, ISO 8601, or None."""
        renews = self.payload.get("credits", {}).get("renews_at")

        return str(renews) if renews is not None else None


class RateLimited(ToxicFilterError):
    """Too many requests. Retried automatically; if you see it, the retries ran out."""

    retryable = True

    @property
    def retry_after(self) -> int:
        """Seconds the service asked us to wait, or 1."""
        return max(1, int(self.payload.get("retry_after", 1)))


class InvalidRequest(ToxicFilterError):
    """The request was malformed and nothing was judged."""

    @property
    def fields(self) -> dict[str, list[str]]:
        """The fields it refused, each with its messages."""
        return self.payload.get("errors") or self.payload.get("error", {}).get("fields") or {}


class NotFound(ToxicFilterError):
    """No such verdict, batch or endpoint on this account."""


class ServerError(ToxicFilterError):
    """Our fault, or the network's. Retried automatically."""

    retryable = True


def error_for(status: int, payload: dict[str, Any]) -> ToxicFilterError:
    """Return the right exception for a status."""
    error = payload.get("error") or {}
    message = error.get("message") or payload.get("message") or "The request failed."
    code = error.get("code")

    if status == 401:
        return AuthenticationError(message, status, code, payload)
    if status == 402:
        return QuotaExhausted(message, status, code, payload)
    if status == 404:
        return NotFound(message, status, code, payload)
    if status == 422:
        return InvalidRequest(message, status, code, payload)
    if status == 429:
        return RateLimited(message, status, code, payload)
    # 409 is `idempotency_in_flight`: the earlier attempt at this very call is still
    # running, so waiting and asking again is exactly right.
    if status == 409 or status >= 500:
        return ServerError(message, status, code, payload)

    return ToxicFilterError(message, status, code, payload)
