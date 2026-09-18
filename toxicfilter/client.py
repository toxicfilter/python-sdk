"""The ToxicFilter API, from Python."""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Protocol

from .errors import RateLimited, ServerError, ToxicFilterError, error_for
from .models import BatchResult, Verdict

VERSION = "1.0.0"


class Transport(Protocol):
    """How a request actually leaves the machine.

    One method, so the client depends on no HTTP library at all, with nothing to conflict
    with whatever your application already pins, and so it can be driven against a server
    in tests rather than against a recording.
    """

    def send(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, str]:  # pragma: no cover - interface
        """Send the request and return ``(status, body)``."""
        ...


class UrllibTransport:
    """The standard library, and nothing else.

    A client for one small API is not worth a dependency tree, and a library that forces a
    major version of ``requests`` or ``httpx`` on its host is a library that gets removed
    the first time the host disagrees.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        """Build a transport whose requests give up after ``timeout`` seconds."""
        self.timeout = timeout

    def send(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, str]:
        """Send the request and return ``(status, body)``, raising only for the unreachable."""
        request = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            # Reported as a server error because the caller does the same thing about
            # either: the request did not arrive, so ask again.
            raise ServerError(f"Could not reach ToxicFilter: {e.reason}") from e
        except OSError as e:
            # A READ timeout does not arrive as a URLError. `urlopen` wraps what happens
            # while connecting and lets `socket.timeout` (which is `TimeoutError`, an
            # OSError) through untouched once the connection is open, so without this the
            # one failure most worth retrying was the one that escaped as something the
            # caller had never heard of.
            raise ServerError(f"Could not reach ToxicFilter: {e}") from e



def _encode_image(data: bytes | str) -> str:
    """Bytes as the API wants them: base64, or a ``data:`` URI left alone.

    Encoding something already encoded would send the alphabet of the alphabet, and the
    service would decode one layer and find text where a picture should be.
    """
    if isinstance(data, str):
        if data.startswith("data:"):
            return data

        try:
            base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            return base64.b64encode(data.encode()).decode()

        return data

    return base64.b64encode(data).decode()


class Client:
    """The ToxicFilter API, from Python.

    Retries a 429 and a 5xx with a growing wait, and a 402 never. Every call carries an
    ``Idempotency-Key``, so a retried timeout is judged once and billed once.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://toxicfilter.com",
        retries: int = 2,
        timeout: float = 10.0,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_wait: float = 30.0,
    ) -> None:
        """Build a client.

        Args:
            api_key: ``tf_live_...`` or ``tf_test_...``.
            base_url: the service, for a self-hosted or a staging one.
            retries: how many times to ask again when it is worth asking again. A 429 or a
                5xx is retried with a growing wait; a 402 never is.
            timeout: seconds for the whole request. Ignored when ``transport`` is given,
                since a transport owns its own.
            transport: anything with a ``send()``, to put the request through your own HTTP
                stack.
            sleep: how to wait between attempts. Replaced in tests so they do not.
            max_wait: ceiling on that wait. A 429 says how long to wait, and a number on
                the wire must not decide how long your own request hangs.
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.transport = transport or UrllibTransport(timeout)
        self._sleep = sleep
        #: The longest this will ever sleep between attempts, whatever the service asks for.
        #: A 429 says exactly how long to wait and honouring it is the point; honouring it
        #: without a ceiling lets a number on the wire decide how long your call hangs.
        self.max_wait = max_wait

    # -- moderation ---------------------------------------------------------------

    def text(self, content: str, **options: Any) -> Verdict:
        """Judge a piece of text: a comment, a review, a message, a description."""
        return Verdict(self._post("/api/v1/text", {"content": content, **options}))

    def email(self, address: str, **options: Any) -> Verdict:
        """Judge an address, including a malformed one, which is the point."""
        return Verdict(self._post("/api/v1/email", {"address": address, **options}))

    def name(self, name: str, **options: Any) -> Verdict:
        """Judge a display name or a username."""
        return Verdict(self._post("/api/v1/name", {"name": name, **options}))

    def signup(self, **fields: Any) -> Verdict:
        """Name, email and bio judged together, because the combination is the signal."""
        return Verdict(self._post("/api/v1/signup", fields))

    def image(self, url: str, **options: Any) -> Verdict:
        """Judge a picture, by address or by value.

        An http or https address is fetched by us. Anything else is treated as the file
        itself and goes out as bytes: not a guess, since ``url`` accepts those two schemes
        and nothing else, so everything else is bytes by elimination.
        """
        if not url.startswith(("http://", "https://")):
            return self.image_data(url, **options)

        return Verdict(self._post("/api/v1/image", {"url": url, **options}))

    def image_data(self, data: bytes | str, **options: Any) -> Verdict:
        """Judge a picture you hold, rather than one you have published.

        Takes the raw file, a base64 string or a ``data:`` URI. Never retained by the
        service, whatever the policy says: you already hold the file.
        """
        return Verdict(self._post("/api/v1/image", {"data": _encode_image(data), **options}))

    def prompt(self, content: str, **options: Any) -> Verdict:
        """Text on its way into YOUR model, rather than to a reader.

        Prompt injection, plus everything a comment is checked for: somebody pasting a card
        number into your chatbot is a problem you have either way.
        """
        return Verdict(self._post("/api/v1/prompt", {"content": content, **options}))

    def url(self, url: str, **options: Any) -> Verdict:
        """One link, judged as a link. Never fetches it, so a clean answer means "looks like
        what it says", not "safe".
        """
        return Verdict(self._post("/api/v1/url", {"url": url, **options}))

    def conversation(self, messages: list[dict[str, Any]], **options: Any) -> Verdict:
        """Judge a message with what came before it.

        The LAST message is judged and the rest is context, which is what catches a pile-on
        or an approach that no single message shows. Up to fifty, oldest first, each
        ``{"author": ..., "content": ...}``.
        """
        return Verdict(self._post("/api/v1/conversation", {"messages": list(messages), **options}))

    # -- batches ------------------------------------------------------------------

    def batch(self, items: list[dict[str, Any]], **options: Any) -> BatchResult:
        """Many things in one call, answered now."""
        return BatchResult(self._post("/api/v1/batch", {"items": list(items), **options}))

    def batch_async(self, items: list[dict[str, Any]], **options: Any) -> BatchResult:
        """Queue the same batch. Answer immediately; the work happens on our side."""
        return self.batch(items, **{**options, "async": True})

    def batch_status(self, batch_id: str, **query: Any) -> BatchResult:
        """Read an async batch back, with its results paged by cursor."""
        return BatchResult(self._get(f"/api/v1/batches/{urllib.parse.quote(batch_id)}", query))

    # -- the review queue ---------------------------------------------------------

    def records(self, **query: Any) -> dict[str, Any]:
        """List what is waiting for a person."""
        body = self._get("/api/v1/records", query)

        return {
            "records": [Verdict(row) for row in body.get("records", [])],
            "next_before": body.get("next_before"),
        }

    def record(self, record_id: str) -> Verdict:
        """One stored verdict, in full, with the content if any was kept."""
        return Verdict(self._get(f"/api/v1/records/{urllib.parse.quote(record_id)}"))

    def resolve(
        self,
        record_id: str,
        action: str,
        moderator: str | None = None,
        note: str | None = None,
    ) -> Verdict:
        """Record that a person decided. ``action`` is ``approved`` or ``rejected``."""
        payload = {"action": action}

        if moderator is not None:
            payload["moderator"] = moderator
        if note is not None:
            payload["note"] = note

        path = f"/api/v1/records/{urllib.parse.quote(record_id)}/resolve"

        return Verdict(self._post(path, payload))

    def feedback(self, record_id: str, verdict: str, note: str | None = None) -> Verdict:
        """Tell us the verdict was wrong. It costs nothing, and it is the only honest
        measure of whether the thresholds are set well.
        """
        payload: dict[str, Any] = {"verdict": verdict}

        if note is not None:
            payload["note"] = note

        path = f"/api/v1/records/{urllib.parse.quote(record_id)}/feedback"

        return Verdict(self._post(path, payload))

    # -- the account --------------------------------------------------------------

    def usage(self) -> dict[str, Any]:
        """Credits, windows, prices. Free, and it answers even when the allowance is gone."""
        return self._get("/api/v1/usage")

    def keys(self) -> dict[str, Any]:
        """Your keys: prefixes, modes, last use. Never a secret."""
        return self._get("/api/v1/keys")

    def revoke_key(self, key_id: int | str) -> dict[str, Any]:
        """Revokes one, including the one you are calling with. There is no create."""
        return self._post(f"/api/v1/keys/{urllib.parse.quote(str(key_id))}/revoke", {})

    def ping(self) -> dict[str, Any]:
        """Is the key good, is the service up. Costs nothing."""
        return self._get("/api/v1/ping")

    # -- plumbing -----------------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)

        # One key per call, reused by every retry of that call. That is what makes the
        # retrying safe: the same key means the same request, and the API answers it once
        # however many times the network makes us ask.
        key = payload.pop("idempotency_key", None) or f"py-{secrets.token_hex(16)}"

        return self._send("POST", path, {}, payload, key)

    def _get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._send("GET", path, query or {}, None, None)

    def _send(
        self,
        method: str,
        path: str,
        query: dict[str, Any],
        payload: dict[str, Any] | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        url = self.base_url + path
        query = {k: v for k, v in query.items() if v is not None}

        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": f"toxicfilter-python/{VERSION}",
        }

        body = None

        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()

        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        attempt = 0

        while True:
            try:
                status, raw = self.transport.send(method, url, headers, body)
                decoded = self._decode(raw)

                if status < 400:
                    return decoded

                raise error_for(status, decoded)
            except ToxicFilterError as e:
                if not e.retryable or attempt >= self.retries:
                    raise

                attempt += 1

                # Growing, because a service that just said "too many" is not helped by
                # being asked again immediately.
                #
                # A rate limit says how long, and that beats guessing: the service knows
                # when its own window turns over. Bounded all the same, so a wrong or
                # hostile number cannot pin this thread for as long as it likes.
                self._sleep(min(
                    self.max_wait,
                    e.retry_after if isinstance(e, RateLimited) else 2**attempt,
                ))

    @staticmethod
    def _decode(raw: str) -> dict[str, Any]:
        try:
            decoded = json.loads(raw or "{}")
        except ValueError:
            return {}

        return decoded if isinstance(decoded, dict) else {}
