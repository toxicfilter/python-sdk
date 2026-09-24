"""The answers, with the awkward parts already handled."""

from __future__ import annotations

from typing import Any

from .errors import ServerError


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
        """``allow``, ``review`` or ``block``.

        Raises ``ServerError`` when the answer carries none. It used to default to
        ``allow``, which made anything that was not a verdict (an empty body, a proxy's
        page) publish the content it was supposed to be checking. Every answer that is a
        verdict carries one, stored records and batch rows included.
        """
        decision = self.raw.get("decision")

        if decision not in ("allow", "review", "block"):
            raise ServerError(
                f"The answer carried no decision ({decision!r}), so it is not a verdict."
            )

        return str(decision)

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
    def project(self) -> str | None:
        """The project the verdict was filed under: the one you named, or your default."""
        project = self.raw.get("project")
        return project if isinstance(project, str) else None

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
    def reason(self) -> str | None:
        """The first reason, or None when there is none."""
        return next((reason for reason in self.reasons if reason), None)

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
    def renews_at(self) -> str | None:
        """When the monthly allowance comes back, ISO 8601, or None.

        The end of the current month whatever the balance, so it is there long before you
        run out. ``None`` for an account that has never spent anything (the month starts
        with the first call) and on a stored verdict, which carries no credits block.
        """
        renews = (self.raw.get("credits") or {}).get("renews_at")
        return renews if isinstance(renews, str) else None

    @property
    def model(self) -> dict[str, Any] | None:
        """``{"asked": True, "read": False, "why": ...}`` when you asked for the model and
        it was deliberately not run on this call, or ``None``.

        Deliberately, as opposed to ``degraded``, which says it could not run. In a
        conversation the model reads only when it adds something (``why`` is then
        ``conversation_sampling``), and without this block a message it skipped looked
        exactly like one the cheap detectors settled.
        """
        model = self.raw.get("model")
        return dict(model) if isinstance(model, dict) else None

    @property
    def policy(self) -> dict[str, Any]:
        """Which rules produced this, by name and version. Worth logging.

        ``overridden`` is true when the call's own ``rules`` were laid over the policy.
        """
        policy = self.raw.get("policy") or {}
        return {
            "slug": policy.get("slug", "default"),
            "version": int(policy.get("version", 0)),
            "overridden": policy.get("overridden") is True,
        }

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
        here. Filled by ``record()``, ``resolve()`` and ``feedback()``, which answer with the
        whole stored verdict. Not by ``records()``: the listing returns the verdict alone,
        and not by the call that produced it.
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
    def project(self) -> str | None:
        """The project the batch was filed under."""
        project = self.raw.get("project")
        return project if isinstance(project, str) else None

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

    def _error_rows(self) -> list[dict[str, Any]]:
        """Every error row, wherever the answer put it.

        A sync batch puts item errors among its ``results``; ``batch_status()`` puts
        verdicts in ``results`` and errors in ``errors``. Reading ``results or errors`` never
        reached the second as soon as one verdict existed, so a page of a finished backfill
        reported no failures at all. Both are read; the same row in both (a sync batch's
        malformed items are) is one failure, because they are keyed by index.
        """
        rows = [*(self.raw.get("results") or []), *(self.raw.get("errors") or [])]
        return [row for row in rows if isinstance(row, dict) and "error" in row]

    @property
    def failures(self) -> dict[int, dict[str, Any]]:
        """The items that could not be judged, keyed by the position they were sent in.

        Only rows that name their item. A failure that belongs to no single item (a chunk
        of an async batch that died outright) is in ``unplaced_failures`` instead: numbered
        by its position in the list it would have taken a real item's key.
        """
        return {
            int(row["index"]): row["error"]
            for row in self._error_rows()
            if row.get("index") is not None
        }

    @property
    def unplaced_failures(self) -> list[dict[str, Any]]:
        """Failures that name no item, such as ``chunk_failed``.

        A worker that dies with a chunk reports how many items were lost, not which, so
        there is no index to key it by. ``failed`` counts those items; this says why.
        """
        return [row["error"] for row in self._error_rows() if row.get("index") is None]

    @property
    def is_async(self) -> bool:
        """Whether it was queued rather than answered in the call."""
        return bool(self.raw.get("async"))

    @property
    def summary(self) -> dict[str, int]:
        """How many ended ``allow``, ``review`` and ``block``, and how many ``failed``.

        Counted over the whole batch, not over the page you are holding.
        """
        return {k: int(v) for k, v in (self.raw.get("summary") or {}).items()}

    @property
    def status_url(self) -> str | None:
        """Where to poll an async batch. Only on the answer that queued it."""
        url = self.raw.get("status_url")
        return url if isinstance(url, str) else None

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
