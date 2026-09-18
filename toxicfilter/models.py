"""The answers, with the awkward parts already handled."""

from __future__ import annotations

from typing import Any


class Verdict:
    """One answer.

    There is deliberately no ``is_toxic``. The API returns fifteen categories and three
    decisions precisely because collapsing them into one boolean bakes somebody else's
    policy into every caller, and an SDK that offers the boolean anyway undoes the whole
    argument on the way out of the door.
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        """Wrap the decoded JSON of one answer."""
        self.raw = raw

    @property
    def decision(self) -> str:
        """``allow``, ``review`` or ``block``."""
        return str(self.raw.get("decision", "allow"))

    @property
    def allowed(self) -> bool:
        """Nothing crossed a line. Publish it."""
        return self.decision == "allow"

    @property
    def needs_review(self) -> bool:
        """A person should look. Hold it; do not delete it."""
        return self.decision == "review"

    @property
    def blocked(self) -> bool:
        """Refuse it."""
        return self.decision == "block"

    @property
    def id(self) -> str | None:
        """The verdict's own name: what a support ticket or a webhook refers to."""
        return self.raw.get("id")

    @property
    def reference(self) -> str | None:
        """Your own id for the thing, handed back."""
        return self.raw.get("reference")

    @property
    def flagged(self) -> list[str]:
        """Everything that crossed a line, worst first.

        Categories come back by their own name and have a score in ``scores``. Whatever
        crossed on the other two axes is prefixed ``topic:`` or ``lead:``, because its score
        lives in ``topics`` or ``leads`` and a bare ``gambling`` here would send you looking
        for one that does not exist.
        """
        return list(self.raw.get("flagged") or [])

    @property
    def scores(self) -> dict[str, float]:
        """The highest score per category, 0 to 1."""
        return dict(self.raw.get("scores") or {})

    def score(self, category: str) -> float:
        """One category's score, 0.0 when it did not score at all."""
        return float(self.scores.get(category, 0.0))

    @property
    def signals(self) -> list[dict[str, Any]]:
        """Every finding, with its evidence."""
        return list(self.raw.get("signals") or [])

    @property
    def reasons(self) -> list[str]:
        """Why, in words you can show the person whose content it was.

        A rejection with no reason is what makes people think moderation is arbitrary.
        """
        return [signal.get("reason", "") for signal in self.signals]

    @property
    def topics(self) -> dict[str, float]:
        """How much this is ABOUT a subject, 0 to 1, for every topic your account measures.

        A separate axis from the categories on purpose: gambling is not harm, it is a
        subject, and whether a casino advert belongs on your site is your decision rather
        than ours. A post centred on one scores high; one mentioning it in passing does not.
        """
        return dict(self.raw.get("topics") or {})

    def topic(self, topic: str) -> float:
        """One subject's score, 0.0 when it is not there at all."""
        return float(self.topics.get(topic, 0.0))

    @property
    def leads(self) -> dict[str, float]:
        """Who is writing, 0 to 1 per lead type (``free_work_for_equity``, ``no_budget``,
        ``no_show``...).

        One person is often several types at once, so each keeps its own score, and none
        acts until your rules give a type a line.
        """
        return dict(self.raw.get("leads") or {})

    def lead(self, name: str) -> float:
        """One lead type's score, 0.0 when nothing suggested it."""
        return float(self.leads.get(name, 0.0))

    @property
    def facts(self) -> dict[str, Any]:
        """What was noticed but is not a finding: ``age_signal``, a detected language, a
        near-duplicate's fingerprint. Deliberately not signals: a thirteen-year-old saying
        so is a child using a website, not a thing they did wrong.
        """
        return dict(self.raw.get("facts") or {})

    @property
    def degraded(self) -> bool:
        """Part of the pipeline could not run, usually the model.

        The verdict is still real; it was reached with less. Its own field rather than a
        quiet ``used_ai: False`` so a caller who asked for a model can tell "it read this
        and found nothing" from "it never ran".
        """
        return bool(self.raw.get("degraded"))

    @property
    def used_ai(self) -> bool:
        """Whether a model read it, or the cheap detectors settled it."""
        return bool(self.raw.get("used_ai"))

    @property
    def cached(self) -> bool:
        """Whether this content had been judged before."""
        return bool(self.raw.get("cached"))

    @property
    def charged(self) -> int:
        """What this call cost, in credits."""
        credit_block = self.raw.get("credits") or {}
        return int(credit_block.get("charged", self.raw.get("charged", 0)))

    @property
    def credits_remaining(self) -> int:
        """Credits left after this call."""
        return int((self.raw.get("credits") or {}).get("remaining", 0))

    @property
    def policy(self) -> dict[str, Any]:
        """Which rules produced this, by name and version. Worth logging."""
        policy = self.raw.get("policy") or {}
        return {"slug": policy.get("slug", "default"), "version": int(policy.get("version", 0))}

    @property
    def redacted(self) -> str | None:
        """The content with the personal data masked out, when you asked with
        ``redact=True`` and there was any.

        Usually worth more than refusing the message: throwing a whole comment away because
        it carried one phone number throws away everything else the person wrote. ``None``
        when you did not ask, or when there was nothing to mask.
        """
        redacted = self.raw.get("redacted")
        return redacted if isinstance(redacted, str) else None

    @property
    def context(self) -> dict[str, Any]:
        """What was known beyond the content itself: ``repeats``, ``similar``, and the
        actor's ``history`` with the ``adjustment`` it earned.

        Present only when it was not empty, and present whenever it was, including when it
        moved the line by nothing: a decision changed by somebody's record with no way to
        see that is the kind of moderation this API exists not to be.
        """
        return dict(self.raw.get("context") or {})

    @property
    def shadow(self) -> dict[str, Any] | None:
        """What the policy being TRIALLED would have said, with its own slug and version.

        Reported and never acted on: seeing the disagreement over your own traffic is the
        entire point of running one. ``None`` when no shadow policy is set.
        """
        shadow = self.raw.get("shadow")

        if isinstance(shadow, dict):
            return {
                "slug": shadow.get("slug"),
                "version": int(shadow.get("version", 0)),
                "decision": shadow.get("decision"),
            }

        # A stored verdict spells the same two facts flat, the way its columns are named.
        if self.raw.get("shadow_slug") or self.raw.get("shadow_decision"):
            return {
                "slug": self.raw.get("shadow_slug"),
                "version": int(self.raw.get("shadow_version", 0)),
                "decision": self.raw.get("shadow_decision"),
            }

        return None

    @property
    def review(self) -> dict[str, Any]:
        """Where this verdict stands in the queue: the state, who decided and when.

        Only ``review`` opens an entry, so a verdict that was allowed outright has nothing
        here. Filled by ``records()`` and ``record()`` rather than by the call that produced
        the verdict.
        """
        return dict(self.raw.get("review") or {})

    @property
    def review_state(self) -> str | None:
        """``open``, ``approved`` or ``rejected``."""
        state = self.review.get("state")
        return state if isinstance(state, str) else None

    @property
    def resolved(self) -> bool:
        """Whether a person has already dealt with it."""
        return self.review_state in ("approved", "rejected")

    @property
    def resolved_by(self) -> str | None:
        """Your own name for whoever decided. We never invent one."""
        by = self.review.get("resolved_by")
        return by if isinstance(by, str) else None

    @property
    def resolved_at(self) -> str | None:
        """When a person decided, ISO 8601, or None."""
        by = self.review.get("resolved_at")
        return by if isinstance(by, str) else None

    @property
    def feedback(self) -> dict[str, Any] | None:
        """What you have already told us about this verdict: ``correct``,
        ``false_positive`` or ``false_negative``, with its note and the time it was sent.

        One answer per verdict, so sending a second replaces the first. ``None`` when nobody
        has said.
        """
        feedback = self.raw.get("feedback")
        return dict(feedback) if isinstance(feedback, dict) else None

    @property
    def content(self) -> str | None:
        """The content, when your policy asked us to keep it and it has not expired yet.

        Empty almost everywhere on purpose: ``retain_hours`` is 0 by default and a verdict
        stores a hash of the content and never the content. Only ``record()`` ever fills
        this; a listing deliberately does not, because a queue screen wants fifty headlines
        rather than fifty comments.
        """
        content = self.raw.get("content")
        return content if isinstance(content, str) else None

    @property
    def content_expires_at(self) -> str | None:
        """When the retained content is dropped, or None."""
        at = self.raw.get("content_expires_at")
        return at if isinstance(at, str) else None

    @property
    def kind(self) -> str | None:
        """What was judged: ``text``, ``email``, ``name``, ``image``, ..."""
        kind = self.raw.get("kind")
        return kind if isinstance(kind, str) else None

    @property
    def created_at(self) -> str | None:
        """When the verdict was reached, ISO 8601."""
        at = self.raw.get("created_at")
        return at if isinstance(at, str) else None

    @property
    def batch_id(self) -> str | None:
        """The batch it arrived in, when it arrived in one."""
        batch = self.raw.get("batch_id")
        return batch if isinstance(batch, str) else None

    @property
    def took_ms(self) -> int:
        """How long it took us, in milliseconds."""
        return int(self.raw.get("took_ms", 0))

    def __repr__(self) -> str:  # pragma: no cover - debugging sugar
        """Summarise the verdict for a log line or a debugger."""
        return f"<Verdict {self.decision} flagged={self.flagged} id={self.id}>"


class BatchResult:
    """A verdict per item, or an error in its place.

    One bad item is an item, not a batch: the API answers 200 with the failure filed where
    that item was, so nothing here throws for a single bad element.
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        """Wrap the decoded JSON of one answer."""
        self.raw = raw

    @property
    def id(self) -> str:
        """The batch's own id, `bat_...`."""
        return str(self.raw.get("batch_id", ""))

    @property
    def status(self) -> str:
        """`queued`, `running`, `completed` or `failed`."""
        return str(self.raw.get("status", "queued"))

    @property
    def finished(self) -> bool:
        """Whether there is nothing left to wait for."""
        return self.status == "completed"

    @property
    def verdicts(self) -> dict[int, Verdict]:
        """Keyed by the position each item was sent in."""
        return {
            int(row.get("index", i)): Verdict(row)
            for i, row in enumerate(self.raw.get("results") or [])
            if "error" not in row
        }

    @property
    def failures(self) -> dict[int, dict[str, Any]]:
        """The items that could not be judged, keyed by their index."""
        rows = self.raw.get("results") or self.raw.get("errors") or []
        return {
            int(row.get("index", i)): row["error"]
            for i, row in enumerate(rows)
            if "error" in row
        }

    @property
    def count(self) -> int:
        """How many items were submitted."""
        return int(self.raw.get("count", 0))

    @property
    def processed(self) -> int:
        """How many have a verdict."""
        return int(self.raw.get("processed", 0))

    @property
    def failed(self) -> int:
        """How many could not be judged."""
        return int(self.raw.get("failed", 0))

    @property
    def credits_charged(self) -> int:
        """What the batch has cost so far, in credits."""
        return int(self.raw.get("credits_charged", 0))

    @property
    def next_after(self) -> int | None:
        """The cursor for the next page of results, or ``None`` when that was the last one.

        A cursor rather than a page number, and this is the field that makes paging work at
        all: rows appear as workers finish them, so an offset into a growing list skips
        whatever was inserted behind it. Pass it back as ``after``.
        """
        after = self.raw.get("next_after")
        return None if after is None else int(after)

    @property
    def has_more(self) -> bool:
        """Whether there is another page to ask for."""
        return self.next_after is not None

    def __repr__(self) -> str:  # pragma: no cover
        """Summarise the batch for a log line or a debugger."""
        return f"<BatchResult {self.id} {self.status} {self.processed}/{self.count}>"
