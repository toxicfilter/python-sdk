"""The ToxicFilter API, from Python."""

from __future__ import annotations

import base64
import binascii
import http.client
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Protocol

from .errors import RateLimited, ServerError, ToxicFilterError, error_for
from .models import BatchResult, Verdict

#: The one place the version is written, besides ``pyproject.toml``: the publish workflow
#: checks the tag against the manifest, and ``test_the_constant_and_the_manifest_agree``
#: checks the manifest against this, so the User-Agent cannot claim a release it is not.
VERSION = "1.2.4"


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


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Hand a 3xx back instead of following it.

    urllib follows a POST answered with 302 as a GET to wherever ``Location`` says,
    carrying the ``Authorization`` header with it. The API never redirects, so a redirect
    is a proxy, a captive portal or a misconfigured base URL: following it sends the key
    somewhere it was not meant for and reads that page as the answer.
    """

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        """Refuse every redirect, which makes urllib raise it as an ``HTTPError``."""
        return None


class UrllibTransport:
    """The standard library, and nothing else.

    A client for one small API is not worth a dependency tree, and a library that forces a
    major version of ``requests`` or ``httpx`` on its host is a library that gets removed
    the first time the host disagrees.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        """Build a transport whose socket operations give up after ``timeout`` seconds."""
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirects)

    def send(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, str]:
        """Send the request and return ``(status, body)``, raising only for the unreachable.

        Every failure of the connection itself becomes a ``ServerError``, which the client
        retries. That includes the ones urllib does not wrap: a read timeout arrives as a
        bare ``TimeoutError``, a truncated or garbled response as ``http.client``'s own
        ``IncompleteRead`` or ``BadStatusLine``, and reading the body of an error response
        can time out as well. Any of those escaping untyped would pass every
        ``except ToxicFilterError`` the caller wrote, on the failure most worth retrying.
        """
        request = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return response.status, response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                # Read here, inside the outer try: the body of an error can stall too.
                return e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            # Reported as a server error because the caller does the same thing about
            # either: the request did not arrive, so ask again.
            raise ServerError(f"Could not reach ToxicFilter: {e.reason}") from e
        except (http.client.HTTPException, OSError) as e:
            raise ServerError(f"Could not reach ToxicFilter: {e!r}") from e


def _encode_image(data: bytes | str) -> str:
    """Bytes as the API wants them: standard base64, or a ``data:`` URI left alone.

    Encoding something already encoded would send the alphabet of the alphabet, and the
    service would decode one layer and find text where a picture should be. Base64 is
    therefore recognised as the rest of the world writes it, not only as ``b64encode``
    does: wrapped at 76 columns (MIME, ``openssl``, ``base64.encodebytes``), in the
    URL-safe alphabet, or without its padding. Each of those failed a strict check here and
    went out encoded a second time. What is sent is the cleaned, standard form, because
    the service decodes strictly and does not accept ``-`` or ``_``.
    """
    if isinstance(data, str):
        if data.startswith("data:"):
            return data

        cleaned = "".join(data.split()).replace("-", "+").replace("_", "/")

        if cleaned and len(cleaned) % 4:
            cleaned += "=" * (-len(cleaned) % 4)

        try:
            base64.b64decode(cleaned, validate=True)
        except (ValueError, binascii.Error):
            return base64.b64encode(data.encode()).decode()

        return cleaned

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
            timeout: seconds for each socket operation (connecting, then each read), not
                for the whole request: that is what urllib's timeout means, so a server
                trickling bytes can take longer in total. Ignored when ``transport`` is
                given, since a transport owns its own.
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

    def batches(self, **query: Any) -> list[BatchResult]:
        """List the most recent batches, newest first, each summarised without its rows.

        For the caller who lost a batch id: a crashed worker, a restarted deploy. Takes
        ``limit`` (1 to 100) and ``project``. Read one in full with ``batch_status()``.
        """
        body = self._get("/api/v1/batches", query)

        return [BatchResult(row) for row in body.get("batches", []) if isinstance(row, dict)]

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

                if 200 <= status < 300:
                    return self._answer(status, raw)

                if status < 400:
                    # The API never redirects. A 3xx is a proxy, a portal or a wrong base
                    # URL, and whatever page it points at is not a verdict.
                    raise ServerError(
                        f"ToxicFilter answered {status}, a redirect, which the API never "
                        "sends. Check base_url and any proxy in between.",
                        status,
                    )

                raise error_for(status, self._decode(raw))
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
    def _answer(status: int, raw: str) -> dict[str, Any]:
        """Return the body of a success, which has to be a JSON object or is no answer at all.

        In 1.0.0 an empty or non-JSON body decoded to ``{}`` and a verdict with nothing in
        it read as ``allow``: a proxy's HTML page published whatever was being checked.
        Raised as a retryable ``ServerError``, because a garbled answer is the network's
        fault and asking again is right.
        """
        try:
            decoded = json.loads(raw)
        except ValueError:
            decoded = None

        if not isinstance(decoded, dict):
            raise ServerError(
                f"ToxicFilter answered {status} with a body that is not a JSON object, "
                "so there is no verdict in it.",
                status,
            )

        return decoded

    @staticmethod
    def _decode(raw: str) -> dict[str, Any]:
        """Return the body of a refusal, as far as it can be read.

        Lenient on purpose, unlike ``_answer``: a 502 from a load balancer is an HTML page,
        and it must still become a ``ServerError`` rather than a decoding failure.
        """
        try:
            decoded = json.loads(raw or "{}")
        except ValueError:
            return {}

        return decoded if isinstance(decoded, dict) else {}
