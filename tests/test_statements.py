"""Statements of reasons, appeals and the Transparency Database export."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_client import VERDICT, client

from toxicfilter import ServerError, ToxicFilterError, Verdict

STATEMENT = {
    "restrictions": ["removal"],
    "territories": [],
    "duration": None,
    "facts": {"flagged": ["harassment"], "reasons": ["Insults aimed at the reader"], "source": "own_initiative"},
    "automated": {"detection": True, "decision": True},
    "ground": {"type": "terms", "policy": {"slug": "comments", "version": 3}, "clauses": [], "terms_url": None},
    "redress": {"internal": "appeals@example.com", "out_of_court": True, "judicial": True},
    "locale": "en",
    "text": "We have removed your content.",
}


class StatementAccessorsTest(unittest.TestCase):
    def test_a_verdict_carries_its_statement(self):
        verdict = Verdict({**VERDICT, "decision": "block", "statement": STATEMENT})

        self.assertEqual(verdict.statement["restrictions"], ["removal"])
        self.assertEqual(verdict.statement_text, "We have removed your content.")

    def test_a_verdict_without_a_statement_says_none(self):
        verdict = Verdict(VERDICT)

        self.assertIsNone(verdict.statement)
        self.assertIsNone(verdict.statement_text)
        self.assertIsNone(verdict.appeal)
        self.assertIsNone(verdict.appeal_decision)
        self.assertIsNone(verdict.transparency)

    def test_a_record_carries_its_appeal_and_its_filing(self):
        verdict = Verdict({
            **VERDICT,
            "appeal": {"state": "open", "filed_at": "2026-10-04T10:00:00+00:00", "reason": "A recipe."},
            "transparency": {"uuid": "9f1c", "submitted_at": "2026-10-04T10:01:00+00:00"},
        })

        self.assertEqual(verdict.appeal["state"], "open")
        self.assertEqual(verdict.transparency["uuid"], "9f1c")


class StatementCallsTest(unittest.TestCase):
    def test_it_fetches_a_statement_later_in_a_language(self):
        tf, transport = client([(200, {"id": "mod_01", "statement": STATEMENT})])

        statement = tf.statement("mod_01", locale="es")

        self.assertEqual(statement["restrictions"], ["removal"])
        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertEqual(transport.calls[0]["url"], "https://example.test/api/v1/records/mod_01/statement?locale=es")
        self.assertIsNone(transport.calls[0]["key"])

    def test_no_restriction_is_raised_and_never_retried(self):
        tf, transport = client([
            (409, {"error": {"code": "no_restriction", "message": "This verdict restricts nothing."}}),
        ])

        with self.assertRaises(ToxicFilterError) as caught:
            tf.statement("mod_01")

        self.assertNotIsInstance(caught.exception, ServerError)
        self.assertEqual(caught.exception.code, "no_restriction")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0]["url"], "https://example.test/api/v1/records/mod_01/statement")

    def test_a_call_still_in_flight_is_still_retried(self):
        tf, transport = client([
            (409, {"error": {"code": "idempotency_in_flight", "message": "Still running."}}),
            (200, VERDICT),
        ])

        tf.text("hello")

        self.assertEqual(len(transport.calls), 2)

    def test_it_files_an_appeal(self):
        tf, transport = client([(201, {**VERDICT, "decision": "block", "appeal": {"state": "open"}})])

        verdict = tf.appeal("mod_01", "A recipe.")

        self.assertEqual(transport.calls[0]["method"], "POST")
        self.assertEqual(transport.calls[0]["url"], "https://example.test/api/v1/records/mod_01/appeal")
        self.assertEqual(transport.calls[0]["body"], {"reason": "A recipe."})
        self.assertIsNotNone(transport.calls[0]["key"])
        self.assertEqual(verdict.appeal["state"], "open")

    def test_an_appeal_without_words_sends_an_empty_body(self):
        tf, transport = client([(201, {**VERDICT, "appeal": {"state": "open"}})])

        tf.appeal("mod_01")

        self.assertEqual(transport.calls[0]["method"], "POST")
        self.assertEqual(transport.calls[0]["body"], {})

    def test_it_resolves_an_appeal_and_reads_the_decision(self):
        tf, transport = client([(200, {
            **VERDICT,
            "appeal": {"state": "upheld", "resolved_by": "ana", "explanation": "Because."},
            "appeal_decision": "We have reviewed your appeal and upheld our decision.",
        })])

        verdict = tf.resolve_appeal("mod_01", "upheld", "ana", "Because.", locale="es")

        self.assertEqual(transport.calls[0]["url"], "https://example.test/api/v1/records/mod_01/appeal/resolve")
        self.assertEqual(
            transport.calls[0]["body"],
            {"outcome": "upheld", "moderator": "ana", "explanation": "Because.", "locale": "es"},
        )
        self.assertEqual(verdict.appeal_decision, "We have reviewed your appeal and upheld our decision.")
        self.assertEqual(verdict.appeal["state"], "upheld")

    def test_it_exports_a_period_for_the_transparency_database(self):
        tf, transport = client([(200, {"statements": [{"puid": "mod_01"}], "next": 100})])

        page = tf.transparency("2026-10-01", until="2026-10-31", project="forum", after=50)

        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertEqual(
            transport.calls[0]["url"],
            "https://example.test/api/v1/statements/transparency?since=2026-10-01&until=2026-10-31&project=forum&after=50",
        )
        self.assertEqual(page["statements"][0]["puid"], "mod_01")
        self.assertEqual(page["next"], 100)

    def test_an_export_sends_only_what_was_given(self):
        tf, transport = client([(200, {"statements": [], "next": None})])

        tf.transparency("2026-10-01")

        self.assertEqual(transport.calls[0]["url"], "https://example.test/api/v1/statements/transparency?since=2026-10-01")


if __name__ == "__main__":
    unittest.main()
