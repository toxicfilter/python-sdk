"""What a review of 1.0.0 found, each pinned by a test that failed on that release.

Most of these share one shape: the client agreeing with itself rather than with the API or
with the network. A body that was not an answer read as an empty one, an empty one read as
``allow``, and a failure the transport did not expect escaped as a class nobody catches.
"""

import base64
import http.client
import http.server
import json
import re
import sys
import textwrap
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_client import VERDICT, client

from toxicfilter import VERSION, BatchResult, Client, ServerError, ToxicFilterError, Verdict
from toxicfilter.client import UrllibTransport, _encode_image

ROOT = Path(__file__).resolve().parent.parent


class RawTransport:
    """Answers with raw strings, because the bugs here live in what is NOT JSON."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def send(self, method, url, headers, body):
        self.calls += 1
        return self.responses.pop(0)


def raw_client(responses, **kwargs):
    transport = RawTransport(responses)
    return Client("tf_test_key", "https://example.test", transport=transport, sleep=lambda s: None, **kwargs), transport


class FailOpenTest(unittest.TestCase):
    """A moderation client must never turn "I could not read the answer" into ``allow``.

    In 1.0.0 any status under 400 was success, an empty or non-JSON body decoded to ``{}``,
    and a verdict with no ``decision`` defaulted to ``allow``. A proxy's login page, a
    captive portal or a redirect to the marketing site therefore published whatever was
    being checked.
    """

    def test_a_redirect_is_not_an_answer(self):
        tf, _ = raw_client([(302, "")], retries=0)

        with self.assertRaises(ServerError) as caught:
            tf.text("hello")

        self.assertEqual(302, caught.exception.status)
        self.assertTrue(caught.exception.retryable)

    def test_an_empty_success_is_not_an_answer(self):
        tf, _ = raw_client([(200, "")], retries=0)

        with self.assertRaises(ServerError) as caught:
            tf.text("hello")

        self.assertEqual(200, caught.exception.status)

    def test_html_is_not_an_answer(self):
        tf, _ = raw_client([(200, "<html>Sign in to the proxy</html>")], retries=0)

        with self.assertRaises(ServerError):
            tf.text("hello")

    def test_json_that_is_not_an_object_is_not_an_answer(self):
        tf, _ = raw_client([(200, "[]")], retries=0)

        with self.assertRaises(ServerError):
            tf.text("hello")

    def test_an_unreadable_answer_is_retried(self):
        tf, transport = raw_client([(200, ""), (200, json.dumps(VERDICT))])

        self.assertTrue(tf.text("hello").needs_review)
        self.assertEqual(2, transport.calls)

    def test_an_error_page_that_is_not_json_is_still_typed(self):
        tf, _ = raw_client([(502, "<html>Bad gateway</html>")], retries=0)

        with self.assertRaises(ServerError) as caught:
            tf.text("hello")

        self.assertEqual(502, caught.exception.status)

    def test_a_verdict_without_a_decision_is_not_allow(self):
        verdict = Verdict({"id": "mod_01"})

        with self.assertRaises(ToxicFilterError):
            verdict.decision  # noqa: B018

        with self.assertRaises(ToxicFilterError):
            verdict.allowed  # noqa: B018

    def test_the_real_transport_does_not_follow_a_redirect(self):
        """Urllib follows a POST 302 as a GET, carrying the Authorization header, and the
        page at the far end decoded to ``{}``. The key went somewhere it was not sent and
        the answer was ``allow``.
        """
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                hits.append(self.path)
                body = b'{"decision": "allow"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            url = f"http://127.0.0.1:{server.server_port}/api/v1/text"
            status, _ = UrllibTransport(timeout=5).send("POST", url, {"Authorization": "Bearer k"}, b"{}")
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(302, status)
        self.assertEqual(["/api/v1/text"], hits)


class EncodedImagesTest(unittest.TestCase):
    """Base64 as the rest of the world writes it, not only as ``b64encode`` does."""

    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3

    def test_base64_with_line_breaks_is_not_encoded_twice(self):
        wrapped = base64.encodebytes(self.png).decode()  # 76 columns, as MIME and openssl write it

        self.assertIn("\n", wrapped)
        self.assertEqual(base64.b64encode(self.png).decode(), _encode_image(wrapped))

    def test_url_safe_base64_is_not_encoded_twice(self):
        url_safe = base64.urlsafe_b64encode(self.png).decode()

        self.assertRegex(url_safe, r"[-_]")
        # Normalised to the standard alphabet, which is what the service decodes strictly.
        self.assertEqual(base64.b64encode(self.png).decode(), _encode_image(url_safe))

    def test_unpadded_url_safe_base64_is_not_encoded_twice(self):
        url_safe = base64.urlsafe_b64encode(self.png[:-1]).decode().rstrip("=")

        self.assertEqual(base64.b64encode(self.png[:-1]).decode(), _encode_image(url_safe))

    def test_a_data_uri_still_passes_through_untouched(self):
        uri = "data:image/png;base64,iVBORw0KGgo="

        self.assertEqual(uri, _encode_image(uri))

    def test_it_goes_out_cleaned(self):
        tf, transport = client([(200, VERDICT)])

        tf.image_data("iVBO\r\nRw0K\nGgo=\n")

        self.assertEqual("iVBORw0KGgo=", transport.calls[0]["body"]["data"])


class BatchFailuresTest(unittest.TestCase):
    """``GET /v1/batches/{id}`` sends verdicts in ``results`` and item errors in ``errors``."""

    def test_a_page_with_verdicts_still_reports_its_errors(self):
        result = BatchResult(
            {
                "batch_id": "bat_01",
                "status": "completed",
                "results": [{"index": 0, **VERDICT}],
                "errors": [{"index": 1, "error": {"code": "unknown_policy"}}],
            }
        )

        self.assertEqual([0], list(result.verdicts))
        self.assertEqual("unknown_policy", result.failures[1]["code"])

    def test_a_sync_error_listed_twice_is_one_failure(self):
        row = {"index": 1, "error": {"code": "validation_failed"}}
        result = BatchResult({"results": [{"index": 0, **VERDICT}, row], "errors": [row]})

        self.assertEqual({1: {"code": "validation_failed"}}, result.failures)

    def test_a_lost_chunk_never_takes_a_real_items_place(self):
        """A chunk that died has no index. Numbered by its position it became item 0 and
        either hid item 0's own error or claimed item 0 failed when it had a verdict.
        """
        result = BatchResult(
            {
                "results": [{"index": 0, **VERDICT}],
                "errors": [
                    {"index": 3, "error": {"code": "internal_error"}},
                    {"error": {"code": "chunk_failed", "message": "25 items ..."}},
                ],
            }
        )

        self.assertEqual({3}, set(result.failures))
        self.assertEqual(["chunk_failed"], [e["code"] for e in result.unplaced_failures])
        self.assertIn(0, result.verdicts)


class TransportFailuresTest(unittest.TestCase):
    """Failures the real transport let through untyped, past every ``except ToxicFilterError``."""

    def failing(self, error):
        transport = UrllibTransport()

        class Failing:
            def open(self, *args, **kwargs):
                raise error

        transport._opener = Failing()

        with self.assertRaises(ServerError) as caught:
            transport.send("POST", "https://example.test/api/v1/text", {}, b"{}")

        self.assertTrue(caught.exception.retryable)

    def test_a_truncated_response_is_a_server_error(self):
        self.failing(http.client.IncompleteRead(b"{", 10))

    def test_a_garbled_status_line_is_a_server_error(self):
        self.failing(http.client.BadStatusLine("HTTP/9 banana"))

    def test_a_timeout_reading_an_error_body_is_a_server_error(self):
        import urllib.error

        class Stalling:
            def read(self, *args):
                raise TimeoutError("timed out")

            def close(self):
                pass

        self.failing(urllib.error.HTTPError("https://example.test", 500, "oops", {}, Stalling()))


class NewAccessorsTest(unittest.TestCase):
    def test_it_says_when_the_model_was_deliberately_not_asked(self):
        tf, _ = client([(200, {**VERDICT, "model": {"asked": True, "read": False, "why": "conversation_sampling"}})])

        self.assertEqual("conversation_sampling", tf.text("x").model["why"])

    def test_a_verdict_the_model_read_has_no_model_block(self):
        tf, _ = client([(200, VERDICT)])

        self.assertIsNone(tf.text("x").model)

    def test_it_says_when_the_credits_come_back(self):
        tf, _ = client([(200, VERDICT)])

        self.assertEqual("2026-09-30T00:00:00+00:00", tf.text("x").renews_at)

    def test_a_batch_carries_its_summary_and_where_to_poll(self):
        result = BatchResult(
            {
                "batch_id": "bat_01",
                "status": "queued",
                "async": True,
                "summary": {"allow": 0, "review": 0, "block": 0, "failed": 1},
                "status_url": "https://toxicfilter.com/api/v1/batches/bat_01",
            }
        )

        self.assertTrue(result.is_async)
        self.assertEqual(1, result.summary["failed"])
        self.assertEqual("https://toxicfilter.com/api/v1/batches/bat_01", result.status_url)

    def test_a_sync_batch_has_no_status_url(self):
        result = BatchResult({"batch_id": "bat_01", "async": False})

        self.assertFalse(result.is_async)
        self.assertIsNone(result.status_url)
        self.assertEqual({}, result.summary)


class VersionTest(unittest.TestCase):
    """The tag is checked against ``pyproject.toml`` and the User-Agent read a constant
    typed separately, so the two could disagree on PyPI with nothing failing.
    """

    def test_the_constant_and_the_manifest_agree(self):
        manifest = (ROOT / "pyproject.toml").read_text()
        declared = re.search(r'^version = "([^"]+)"', manifest, re.M).group(1)

        self.assertEqual(declared, VERSION)
        self.assertEqual("1.2.4", VERSION)

    def test_the_user_agent_carries_it(self):
        tf, transport = client([(200, VERDICT)])

        tf.text("x")

        self.assertEqual(f"toxicfilter-python/{VERSION}", transport.calls[0]["headers"]["User-Agent"])


class DocumentationTest(unittest.TestCase):
    def test_the_licence_link_survives_pypi(self):
        readme = (ROOT / "README.md").read_text()

        # PyPI does not rewrite a relative link, so `(LICENSE)` is a 404 on the package page.
        self.assertFalse("](LICENSE)" in readme)
        self.assertTrue("https://github.com/toxicfilter/python-sdk/blob/main/LICENSE" in readme)

    def test_the_readme_backfill_fits_an_async_batch(self):
        readme = (ROOT / "README.md").read_text()

        # `max_async` is 1,000 items: a bigger list is a 422 before anything is queued.
        self.assertFalse("ten_thousand" in readme)

    def test_the_package_docstring_is_valid_python(self):
        import toxicfilter

        example = toxicfilter.__doc__.split("\n", 2)[2]
        compile(textwrap.dedent(example), "<docstring>", "exec")

    def test_no_stale_prices(self):
        for path in [*(ROOT / "toxicfilter").glob("*.py"), ROOT / "README.md"]:
            text = path.read_text().lower()

            for stale in ("forty", "12 credits", "moderate_ai", "moderate_image"):
                self.assertNotIn(stale, text, f"{path.name} still says {stale!r}")


class ReasonTest(unittest.TestCase):
    def test_reason_is_the_first_reason_or_none(self) -> None:
        verdict = Verdict({"decision": "block", "signals": [
            {"category": "spam", "reason": ""},
            {"category": "spam", "reason": "Contains a referral link"},
            {"category": "personal_data", "reason": "Contains a phone number"},
        ]})

        self.assertEqual("Contains a referral link", verdict.reason)
        self.assertEqual(3, len(verdict.reasons))
        self.assertIsNone(Verdict({"decision": "allow", "signals": []}).reason)


if __name__ == "__main__":
    unittest.main()
