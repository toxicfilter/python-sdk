"""The Python client, against a transport that answers like the API does.

The behaviours worth testing here are the ones a caller cannot see and would otherwise
find out from an invoice: what gets retried, what never does, and whether a retry is the
same request or a second one.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toxicfilter import Client, InvalidRequest, QuotaExhausted, RateLimited, webhooks
from toxicfilter.errors import ServerError


class FakeTransport:
    """Answers with whatever it was handed, and remembers what it was asked."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def send(self, method, url, headers, body):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": json.loads(body) if body else None,
                "key": headers.get("Idempotency-Key"),
            }
        )

        status, payload = self.responses.pop(0)

        if isinstance(payload, Exception):
            raise payload

        return status, json.dumps(payload)


def client(responses, **kwargs):
    transport = FakeTransport(responses)
    return Client(
        "tf_test_key",
        "https://example.test",
        transport=transport,
        sleep=lambda s: None,
        **kwargs,
    ), transport


VERDICT = {
    "id": "mod_01",
    "reference": "c_1",
    "decision": "review",
    "flagged": ["toxicity"],
    "scores": {"toxicity": 0.55},
    "signals": [
        {"category": "toxicity", "score": 0.55, "detector": "term", "reason": "Contains 1 profanity."},
    ],
    "used_ai": False,
    "took_ms": 2,
    "cached": False,
    "policy": {"slug": "house", "version": 4},
    "credits": {"remaining": 940, "charged": 1, "renews_at": "2026-09-30T00:00:00+00:00"},
}


class VerdictTest(unittest.TestCase):
    def test_it_reads_the_answer(self):
        tf, transport = client([(200, VERDICT)])

        verdict = tf.text("you fucking legend", locales=["en"], surface="comment", reference="c_1")

        self.assertTrue(verdict.needs_review)
        self.assertFalse(verdict.allowed)
        self.assertFalse(verdict.blocked)
        self.assertEqual("mod_01", verdict.id)
        self.assertEqual(["toxicity"], verdict.flagged)
        self.assertEqual(0.55, verdict.score("toxicity"))
        self.assertEqual(0.0, verdict.score("hate"))
        self.assertEqual(["Contains 1 profanity."], verdict.reasons)
        self.assertEqual({"slug": "house", "version": 4, "overridden": False}, verdict.policy)
        self.assertEqual(1, verdict.charged)

        sent = transport.calls[0]
        self.assertEqual("POST", sent["method"])
        self.assertEqual("https://example.test/api/v1/text", sent["url"])
        self.assertEqual(
            {
                "content": "you fucking legend",
                "locales": ["en"],
                "surface": "comment",
                "reference": "c_1",
            },
            sent["body"],
        )

    def test_a_conversation_goes_up_as_a_conversation(self):
        """The last message is judged; the rest is what makes a pile-on visible at all."""
        tf, transport = client([(200, VERDICT)])

        messages = [
            {"author": "u1", "content": "menudo idiota eres"},
            {"author": "u5", "content": "no tienes ni idea"},
        ]

        tf.conversation(messages, locales=["es"], ai=False)

        sent = transport.calls[0]
        self.assertEqual("https://example.test/api/v1/conversation", sent["url"])
        self.assertEqual({"messages": messages, "locales": ["es"], "ai": False}, sent["body"])

    def test_it_reads_the_second_axis(self):
        """Topics are not categories: how much a post is ABOUT something, measured always."""
        tf, _ = client([(200, {**VERDICT, "topics": {"gambling": 0.82}, "facts": {"age_signal": 13}, "degraded": True})])

        verdict = tf.text("x")

        self.assertEqual(0.82, verdict.topic("gambling"))
        self.assertEqual(0.0, verdict.topic("crypto"))
        self.assertEqual(13, verdict.facts["age_signal"])
        self.assertTrue(verdict.degraded)

    def test_it_reads_the_lead_types(self):
        """Who is writing and what they want: neither a harm nor a subject, and several at once."""
        tf, _ = client([(200, {**VERDICT, "leads": {"free_work_for_equity": 0.9, "no_budget": 0.7}})])

        verdict = tf.text("x")

        self.assertEqual(0.9, verdict.lead("free_work_for_equity"))
        self.assertEqual(0.7, verdict.lead("no_budget"))
        self.assertEqual(0.0, verdict.lead("no_show"))

    def test_there_is_no_toxic_boolean(self):
        """Collapsing fifteen categories into one flag is what this API exists not to do."""
        tf, _ = client([(200, VERDICT)])

        self.assertFalse(hasattr(tf.text("x"), "is_toxic"))


class RetryTest(unittest.TestCase):
    def test_it_retries_a_rate_limit(self):
        tf, transport = client(
            [
                (429, {"error": {"code": "rate_limited", "message": "Too many."}}),
                (200, VERDICT),
            ]
        )

        self.assertTrue(tf.text("hello").needs_review)
        self.assertEqual(2, len(transport.calls))

    def test_a_retry_is_the_same_request(self):
        """The pairing that makes automatic retries safe: same key, so one charge."""
        tf, transport = client(
            [
                (500, {"error": {"code": "server_error", "message": "oops"}}),
                (200, VERDICT),
            ]
        )

        tf.text("hello")

        self.assertEqual(2, len(transport.calls))
        self.assertEqual(transport.calls[0]["key"], transport.calls[1]["key"])
        self.assertTrue(transport.calls[0]["key"].startswith("py-"))

    def test_two_calls_are_two_keys(self):
        tf, transport = client([(200, VERDICT), (200, VERDICT)])

        tf.text("one")
        tf.text("two")

        self.assertNotEqual(transport.calls[0]["key"], transport.calls[1]["key"])

    def test_it_never_retries_a_quota(self):
        """402 means come back with a bigger plan. Retrying it hammers forever."""
        tf, transport = client(
            [
                 (
                    402,
                    {"error": {"code": "quota_exhausted"}, "credits": {"remaining": 20, "required": 40}},
                ),
                (200, VERDICT),
            ]
        )

        with self.assertRaises(QuotaExhausted) as caught:
            tf.image("https://example.test/a.jpg")

        self.assertEqual(1, len(transport.calls))
        self.assertEqual(20, caught.exception.remaining)
        self.assertEqual(40, caught.exception.required)
        self.assertFalse(caught.exception.retryable)

    def test_it_gives_up_eventually(self):
        tf, transport = client([(500, {}), (500, {}), (500, {})], retries=2)

        with self.assertRaises(ServerError):
            tf.text("hello")

        self.assertEqual(3, len(transport.calls))

    def test_a_rate_limit_that_never_clears_is_still_raised(self):
        tf, _ = client([(429, {}), (429, {})], retries=1)

        with self.assertRaises(RateLimited):
            tf.text("hello")

    def test_a_bad_request_is_typed(self):
        tf, _ = client([(422, {"message": "The content field is required.", "errors": {"content": ["required"]}})])

        with self.assertRaises(InvalidRequest) as caught:
            tf.text("")

        self.assertEqual({"content": ["required"]}, caught.exception.fields)


class BatchTest(unittest.TestCase):
    def test_it_separates_verdicts_from_failures(self):
        tf, transport = client(
            [
                (
                    200,
                    {
                        "batch_id": "bat_01",
                        "status": "completed",
                        "count": 2,
                        "processed": 1,
                        "failed": 1,
                        "results": [
                            {"index": 0, **VERDICT},
                            {"index": 1, "error": {"code": "validation_failed"}},
                        ],
                    },
                )
            ]
        )

        result = tf.batch([{"kind": "text", "content": "one"}, {"kind": "text"}], ai=False)

        self.assertTrue(result.finished)
        self.assertEqual(["c_1"], [v.reference for v in result.verdicts.values()])
        self.assertEqual("validation_failed", result.failures[1]["code"])
        self.assertEqual({"kind": "text", "content": "one"}, transport.calls[0]["body"]["items"][0])

    def test_async_sets_the_flag(self):
        tf, transport = client([(202, {"batch_id": "bat_01", "status": "queued", "count": 1})])

        result = tf.batch_async([{"kind": "text", "content": "one"}])

        self.assertEqual("queued", result.status)
        self.assertIs(True, transport.calls[0]["body"]["async"])


class QueueTest(unittest.TestCase):
    def test_it_reads_and_resolves(self):
        tf, transport = client(
            [
                (200, {"records": [VERDICT], "next_before": None}),
                (200, VERDICT),
                (200, VERDICT),
            ]
        )

        queue = tf.records(state="open", limit=10)
        self.assertEqual("c_1", queue["records"][0].reference)
        self.assertEqual(
            "https://example.test/api/v1/records?state=open&limit=10",
            transport.calls[0]["url"],
        )

        tf.resolve("mod_01", "approved", moderator="ana@example.com")
        tf.feedback("mod_01", "false_positive")

        self.assertEqual(
            {"action": "approved", "moderator": "ana@example.com"},
            transport.calls[1]["body"],
        )
        self.assertEqual({"verdict": "false_positive"}, transport.calls[2]["body"])

    def test_reads_carry_no_idempotency_key(self):
        tf, transport = client([(200, {"ok": True})])

        tf.ping()

        self.assertIsNone(transport.calls[0]["key"])


class WebhookTest(unittest.TestCase):
    """The reference implementation, rather than a paragraph everybody reads differently."""

    body = '{"id":"whd_1","event":"moderation.review","data":{}}'
    secret = "whsec_" + "a" * 40

    def signature(self, at=None, body=None):
        import hashlib
        import hmac
        import time

        at = at or int(time.time())
        digest = hmac.new(
            self.secret.encode(), f"{at}.".encode() + (body or self.body).encode(), hashlib.sha256
        ).hexdigest()

        return f"t={at},v1={digest}"

    def test_it_accepts_ours(self):
        self.assertTrue(webhooks.verify(self.body, self.signature(), self.secret))
        self.assertEqual(
            "moderation.review",
            webhooks.event(self.body, self.signature(), self.secret)["event"],
        )

    def test_it_refuses_everything_else(self):
        import time

        self.assertFalse(webhooks.verify(self.body, self.signature(), "whsec_other"))
        self.assertFalse(webhooks.verify(self.body + " ", self.signature(), self.secret))
        self.assertFalse(webhooks.verify(self.body, self.signature(at=int(time.time()) - 3600), self.secret))
        self.assertFalse(webhooks.verify(self.body, "nonsense", self.secret))
        self.assertIsNone(webhooks.event(self.body, "nonsense", self.secret))


class PolicyOverriddenTest(unittest.TestCase):
    def test_it_says_when_the_calls_rules_were_laid_over_the_policy(self):
        tf, _ = client([(200, {**VERDICT, "policy": {"slug": "house", "version": 4, "overridden": True}})])

        verdict = tf.text("anything", policy="house", rules={"thresholds": {"spam": {"block": 0.6}}})

        self.assertEqual({"slug": "house", "version": 4, "overridden": True}, verdict.policy)


class ProjectTest(unittest.TestCase):
    def test_it_sends_the_project_and_reads_it_back(self):
        tf, transport = client([(200, {**VERDICT, "project": "forum"})])

        verdict = tf.text("hello", project="forum")

        self.assertEqual("forum", verdict.project)
        self.assertEqual("forum", transport.calls[0]["body"]["project"])

    def test_a_verdict_without_a_project_says_none(self):
        tf, _ = client([(200, VERDICT)])

        self.assertIsNone(tf.text("hello").project)

    def test_it_lists_the_recent_batches_of_a_project(self):
        tf, transport = client([(200, {"batches": [{"batch_id": "bat_1", "project": "forum", "status": "completed"}]})])

        batches = tf.batches(project="forum")

        self.assertEqual(["bat_1"], [b.id for b in batches])
        self.assertEqual("forum", batches[0].project)
        self.assertTrue(transport.calls[0]["url"].endswith("/api/v1/batches?project=forum"))

    def test_a_batch_reads_its_project(self):
        tf, _ = client([(200, {"batch_id": "bat_1", "project": "forum", "status": "completed", "results": []})])

        self.assertEqual("forum", tf.batch([{"kind": "text", "content": "hi"}], project="forum").project)


if __name__ == "__main__":
    unittest.main()


class InlineImages(unittest.TestCase):
    """The endpoint takes an address OR bytes, exactly one, and for a long time every
    client here could only send the address. That is the wrong way round for the commonest
    case: the reason to check an image is to decide whether to publish it, so demanding it
    be published first defeats the purpose.
    """

    def test_bytes_go_out_as_data(self):
        tf, transport = client([(200, VERDICT)])

        tf.image_data(b"\x89PNG\r\n\x1a\n", reference="avatar_9")

        body = transport.calls[0]["body"]

        self.assertEqual(transport.calls[0]["url"], "https://example.test/api/v1/image")
        self.assertEqual(body["data"], "iVBORw0KGgo=")
        self.assertNotIn("url", body)
        self.assertEqual(body["reference"], "avatar_9")

    def test_a_data_uri_handed_to_image_goes_out_as_bytes(self):
        tf, transport = client([(200, VERDICT)])

        tf.image("data:image/png;base64,iVBORw0KGgo=")

        body = transport.calls[0]["body"]

        # As `url` it would be refused, and the caller would have no idea why: it is a URI,
        # it just is not an address.
        self.assertEqual(body["data"], "data:image/png;base64,iVBORw0KGgo=")
        self.assertNotIn("url", body)

    def test_an_already_encoded_string_is_not_encoded_twice(self):
        tf, transport = client([(200, VERDICT)])

        tf.image_data("iVBORw0KGgo=")

        self.assertEqual(transport.calls[0]["body"]["data"], "iVBORw0KGgo=")

    def test_bare_base64_handed_to_image_also_goes_out_as_bytes(self):
        tf, transport = client([(200, VERDICT)])

        tf.image("iVBORw0KGgo=")

        body = transport.calls[0]["body"]

        self.assertEqual(body["data"], "iVBORw0KGgo=")
        self.assertNotIn("url", body)

    def test_an_address_still_goes_out_as_a_url(self):
        tf, transport = client([(200, VERDICT)])

        tf.image("https://cdn.example.test/a.jpg")

        body = transport.calls[0]["body"]

        self.assertEqual(body["url"], "https://cdn.example.test/a.jpg")
        self.assertNotIn("data", body)


class ReadTimeouts(unittest.TestCase):
    """A read timeout does not arrive as a URLError.

    `urlopen` wraps what happens while connecting and lets `socket.timeout` through
    untouched once the connection is open. Without catching it, the failure most worth
    retrying was the one escaping as something the caller had never heard of, past every
    `except ToxicFilterError` they had written.

    Tested against the real transport, because the bug lived in its except clause and a
    fake one would answer for the fake.
    """

    def test_a_read_timeout_becomes_a_retryable_server_error(self):
        from toxicfilter.client import UrllibTransport

        class TimingOut:
            def open(self, *args, **kwargs):
                raise TimeoutError("timed out")

        transport = UrllibTransport()
        transport._opener = TimingOut()

        with self.assertRaises(ServerError) as caught:
            transport.send("POST", "https://example.test/api/v1/text", {}, b"{}")

        self.assertTrue(caught.exception.retryable)

    def test_a_transport_of_your_own_has_to_say_so(self):
        tf, _ = client([(0, TimeoutError("timed out")), (200, VERDICT)])

        # The retry loop asks again about `ToxicFilterError` and nothing else, so a
        # transport you wrote yourself has to report a failed connection as `ServerError`
        # rather than letting the socket error through. Ours does; this pins the contract
        # so it is a documented boundary and not a surprise.
        with self.assertRaises(TimeoutError):
            tf.text("hello")




class WaitTest(unittest.TestCase):
    """How long a retry waits, and who decides.

    A 429 now says exactly how long: the service knows when its own window turns over, and
    guessing at it is how a client either gives up early or comes back too soon. Bounded all
    the same, or a number on the wire decides how long the caller's own call hangs.
    """

    def waiting(self, responses, **kwargs):
        waited = []
        transport = FakeTransport(responses)
        tf = Client(
            "tf_test_key",
            "https://example.test",
            transport=transport,
            sleep=waited.append,
            **kwargs,
        )

        return tf, transport, waited

    def test_it_waits_as_long_as_the_service_asked(self):
        tf, _, waited = self.waiting(
            [(429, {"error": {"code": "rate_limited"}, "retry_after": 7}), (200, VERDICT)]
        )

        tf.text("hello")

        self.assertEqual([7], waited)

    def test_the_wait_is_bounded(self):
        tf, transport, waited = self.waiting(
            [(429, {"error": {"code": "rate_limited"}, "retry_after": 86_400}), (200, VERDICT)]
        )

        tf.text("hello")

        self.assertEqual([30], waited, "A day is not a retry, it is a hang.")
        self.assertEqual(
            2,
            len(transport.calls),
            "Still retried: the wait is capped, not abandoned.",
        )

    def test_a_rate_limit_with_no_number_still_waits(self):
        tf, _, waited = self.waiting([(429, {"error": {"code": "rate_limited"}}), (200, VERDICT)])

        tf.text("hello")

        self.assertEqual([1], waited)

    def test_a_server_error_grows_its_own_wait(self):
        tf, _, waited = self.waiting([(500, {}), (500, {}), (200, VERDICT)])

        tf.text("hello")

        self.assertEqual([2, 4], waited, "Nothing said how long, so it doubles.")

    def test_the_bound_is_yours_to_set(self):
        tf, _, waited = self.waiting(
            [(429, {"retry_after": 45}), (200, VERDICT)],
            max_wait=5,
        )

        tf.text("hello")

        self.assertEqual([5], waited)


class ReadableFieldsTest(unittest.TestCase):
    """The fields the API returns and this client could not reach.

    All of it was in ``raw``, which means the client was answering "dig it out yourself"
    about fields its own service documents, and a field somebody reads out of ``raw`` is a
    field this client is free to break.
    """

    def test_it_reads_the_masked_content(self):
        tf, _ = client([(200, {**VERDICT, "redacted": "call me on [redacted]"})])

        self.assertEqual(
            "call me on [redacted]",
            tf.text("call me on 600 123 456", redact=True).redacted,
        )

    def test_content_that_was_not_masked_is_none(self):
        tf, _ = client([(200, VERDICT)])

        # None and not "": you did not ask, which is a different statement from "there was
        # nothing to mask" and must not read as "the content is empty".
        self.assertIsNone(tf.text("anything").redacted)

    def test_it_reads_the_context(self):
        tf, _ = client(
            [
                (
                    200,
                    {
                        **VERDICT,
                        "context": {
                            "actor": "u_91",
                            "repeats": 47,
                            "similar": 12,
                            "history": {"seen": 800, "blocked": 2, "adjustment": 0.1},
                        },
                    },
                )
            ]
        )

        context = tf.text("buy now").context

        self.assertEqual(47, context["repeats"])
        self.assertEqual(12, context["similar"])
        # Always reported, because a line moved by somebody's record with no way to see it
        # is not something you can defend.
        self.assertEqual(0.1, context["history"]["adjustment"])

    def test_a_verdict_judged_on_its_own_has_no_context(self):
        tf, _ = client([(200, VERDICT)])

        self.assertEqual({}, tf.text("anything").context)

    def test_it_reads_what_the_trialled_policy_would_have_said(self):
        tf, _ = client(
            [(200, {**VERDICT, "shadow": {"slug": "stricter", "version": 3, "decision": "block"}})]
        )

        verdict = tf.text("you fucking legend")

        self.assertEqual(
            "review",
            verdict.decision,
            "The live policy decides; the trial never does.",
        )
        self.assertEqual("block", verdict.shadow["decision"])
        self.assertEqual("stricter", verdict.shadow["slug"])
        self.assertEqual(3, verdict.shadow["version"])

    def test_it_reads_a_stored_verdicts_flat_shadow_fields(self):
        from toxicfilter import Verdict

        verdict = Verdict({"decision": "review", "shadow_slug": "stricter", "shadow_decision": "block"})

        self.assertEqual("stricter", verdict.shadow["slug"])
        self.assertEqual("block", verdict.shadow["decision"])

    def test_no_shadow_policy_is_none(self):
        tf, _ = client([(200, VERDICT)])

        self.assertIsNone(tf.text("anything").shadow)

    def test_it_reads_the_queue_state(self):
        tf, _ = client(
            [
                (
                    200,
                    {
                        **VERDICT,
                        "kind": "text",
                        "created_at": "2026-09-17T10:00:00+00:00",
                        "batch_id": "batch_01",
                        "review": {
                            "state": "approved",
                            "resolved_at": "2026-09-17T11:00:00+00:00",
                            "resolved_by": "ana@example.com",
                            "note": "Enthusiasm.",
                        },
                        "feedback": {"verdict": "false_positive", "note": None, "at": "2026-09-17T11:01:00+00:00"},
                        "content": "you fucking legend",
                        "content_expires_at": "2026-09-17T17:00:00+00:00",
                    },
                )
            ]
        )

        verdict = tf.record("mod_01")

        self.assertEqual("approved", verdict.review_state)
        self.assertTrue(verdict.resolved)
        self.assertEqual("ana@example.com", verdict.resolved_by)
        self.assertEqual("2026-09-17T11:00:00+00:00", verdict.resolved_at)
        self.assertEqual("Enthusiasm.", verdict.review["note"])
        self.assertEqual("false_positive", verdict.feedback["verdict"])
        self.assertEqual("you fucking legend", verdict.content)
        self.assertEqual("2026-09-17T17:00:00+00:00", verdict.content_expires_at)
        self.assertEqual("text", verdict.kind)
        self.assertEqual("2026-09-17T10:00:00+00:00", verdict.created_at)
        self.assertEqual("batch_01", verdict.batch_id)
        self.assertEqual(2, verdict.took_ms)

    def test_an_open_entry_is_not_resolved_and_has_no_feedback(self):
        from toxicfilter import Verdict

        verdict = Verdict({"decision": "review", "review": {"state": "open"}, "feedback": None})

        self.assertEqual("open", verdict.review_state)
        self.assertFalse(verdict.resolved)
        self.assertIsNone(verdict.feedback)
        # Retention is off by default, so there is normally nothing to show a moderator.
        self.assertIsNone(verdict.content)

    def test_a_batch_page_carries_its_cursor(self):
        tf, transport = client(
            [
                 (
                    200,
                    {"batch_id": "batch_01", "status": "running", "results": [], "next_after": 99},
                ),
                 (
                    200,
                    {"batch_id": "batch_01", "status": "completed", "results": [], "next_after": None},
                ),
            ]
        )

        first = tf.batch_status("batch_01")

        self.assertEqual(99, first.next_after)
        self.assertTrue(first.has_more)

        last = tf.batch_status("batch_01", after=first.next_after)

        self.assertIsNone(last.next_after)
        self.assertFalse(last.has_more)
        self.assertIn("after=99", transport.calls[1]["url"])
