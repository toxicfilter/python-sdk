![ToxicFilter Python SDK](https://raw.githubusercontent.com/toxicfilter/python-sdk/main/art/banner.png)

# ToxicFilter Python SDK

The official Python client for [ToxicFilter](https://toxicfilter.com).

```bash
pip install toxicfilter-sdk
```

```python
from toxicfilter import Client

tf = Client(os.environ["TOXICFILTER_KEY"])

verdict = tf.text("Check this message", locales=["en"], surface="comment", reference="comment_9931")

if verdict.blocked:
    refuse()
elif verdict.needs_review:
    hold(verdict.id, verdict.reasons)
else:
    publish()
```

Three decisions, not two. `review` is where the uncertainty is allowed to live: forced to
choose between publishing and deleting, a threshold set safely deletes real posts and one
set kindly publishes the abuse. There is no `is_toxic` here for the same reason: fifteen
categories collapsed into one boolean is somebody else's policy in your code.

## What it does for you

**Retries the right failures and never the wrong one.** A 429 or a 5xx is asked again with
a growing wait; `QuotaExhausted` is not, ever. Those two are constantly confused and the
correct behaviour is opposite: retry a rate limit, stop dead on a quota.

**Makes those retries safe.** Every call carries an `Idempotency-Key`, generated per call,
so a request that timed out and is asked again is judged once and billed once.

**Waits as long as the service asked, and no longer.** A 429 carries `retry_after`, and
honouring it beats guessing: the service knows when its own window turns over. It is bounded
by `max_wait` (30 seconds by default) all the same, because a number on the wire should not
decide how long your own call hangs.

```python
from toxicfilter import QuotaExhausted, RateLimited

try:
    verdict = tf.text(comment)
except QuotaExhausted as e:
    # Stop calling. No amount of retrying produces credits.
    alert(f"out of credits: {e.remaining} left, needed {e.required}, renews {e.renews_at}")
except RateLimited:
    # Already retried, and still too many. Back off properly.
    ...
```

## When the model is down

A verdict reached without the model because the provider was failing comes back with
`degraded` set, and is billed as the cheap call. It is a separate field from `used_ai` on
purpose: one says the cheap detectors were enough, the other says nobody read it, and only
the first is reassuring. Hold or queue what matters to you when you see it.

## Rules without a policy

Send the line you care about and nothing else is acted on. No stored policy is looked up,
and no default of ours is laid underneath.

```python
verdict = tf.text(comment, rules={"thresholds": {"sexual": {"block": 0.7}}})
```

A category you did not mention still scores and still appears in `signals`; it just does not
decide anything. `policy` and `rules` in the same call is a `422`, and so is a name that is
not a real category, subject or lead type: a line that acts on nothing looks exactly like a
line that works.

## The rest of the answer

```python
verdict.redacted   # the content with the personal data masked, when you asked
verdict.context    # repeats, near-duplicates, the actor's record and what it moved
verdict.shadow     # what a policy you are trialling would have said. Never what happened
verdict.facts      # noticed, not a finding: a language, an age signal, a fingerprint
verdict.degraded   # part of the pipeline could not run
```

`redacted` is usually worth more than a refusal: throwing a whole comment away because it
carried one phone number throws away everything else the person wrote.

```python
verdict = tf.text(comment, redact=True, actor="user_8812")

publish(verdict.redacted or comment)
```

## Who is writing

Every verdict also carries ``leads``: what kind of lead wrote it, scored per type. Neither a
harm nor a subject, but who is on the other side and what they want.

```python
verdict = tf.conversation(messages, rules={"leads": {"free_work_for_equity": {"block": 0.6}}})

verdict.leads                 # {"free_work_for_equity": 0.9, "no_budget": 0.7}
verdict.lead("sales_pitch")   # 0.0 when nothing of that type showed
verdict.blocked               # True: your rule discarded it
```

Types: `free_work_for_equity`, `no_budget`, `unrealistic_expectations`, `free_consulting`,
`scope_creep`, `no_show`, `sales_pitch`, `partnership_offer`, `job_seeker`,
`student_or_survey`, `support_request`. Send the conversation rather than one message and
everything that person said counts.

## Everything else

```python
tf.email("someone@mailinator.com")
tf.name("asdkjhasd")
tf.signup(name="Ana", email="ana@example.com", bio="...")
tf.image("https://cdn.example.com/photo.jpg")
tf.image(base64_or_data_uri)                    # or the bytes, if you have not published it
tf.image_data(open("a.jpg", "rb").read())       # the same thing, said explicitly
tf.url("https://bit.ly/3xYz")                   # one link, judged as a link
tf.prompt(what_the_user_typed_into_your_chatbot)   # prompt injection, plus everything else

# A message with what came before it. A pile-on is thirty people each writing one
# ordinary rude sentence, and no classifier reading one of them can see it.
tf.conversation([
    {"author": "u1", "content": "..."},
    {"author": "u2", "content": "..."},
    {"author": "u5", "content": "the one being judged"},
], locales=["es"])

# Many things in one call. One bad item is an item, not a batch.
batch = tf.batch([
    {"kind": "text", "content": "...", "reference": "c_1"},
    {"kind": "image", "url": "...", "reference": "p_2"},
], ai=False)

for index, verdict in batch.verdicts.items(): ...
for index, error in batch.failures.items(): ...

# A backfill: queued, answered immediately, polled or webhooked.
queued = tf.batch_async(ten_thousand_comments)
tf.batch_status(queued.id)

# A backfill read back a page at a time. A cursor, not an offset: rows appear as workers
# finish them, so an offset skips whatever was inserted behind it.
page = tf.batch_status(queued.id)

while page.has_more:
    page = tf.batch_status(queued.id, after=page.next_after)

# The review queue.
queue = tf.records(state="open")
tf.resolve(record_id, "approved", moderator="ana@example.com")
tf.feedback(record_id, "false_positive")   # free, and the only honest measure we have

# And what a held verdict has had done to it.
verdict = tf.record(record_id)
verdict.review_state   # open, approved or rejected
verdict.resolved_by    # your own name for whoever decided
verdict.feedback       # what you already told us, or None
verdict.content        # only when your policy keeps it, and only until it expires

tf.keys()                     # prefixes, modes, last use. Never a secret.
tf.revoke_key(key_id)         # including the one you are calling with. There is no create.

tf.usage()   # credits, windows, prices. Works at zero credits.
tf.ping()
```

## Webhooks

Your endpoint URL is public. Verify before you act:

```python
from toxicfilter import webhooks

event = webhooks.event(
    request.get_data(),                                  # the RAW body
    request.headers["X-ToxicFilter-Signature"],
    os.environ["TOXICFILTER_WEBHOOK_SECRET"],
)

if event is None:
    return "", 400
```

## Notes

Python 3.9+. **No dependencies**: the standard library does the HTTP. A client for one
small API is not worth a dependency tree, and pinning `requests` or `httpx` is how a client
gets removed from a project. Bring your own by passing anything with a `send()` as
`transport=`.

## How this is tested

The suite drives the client through a stub transport and covers the parts a caller cannot
see: what is retried, what never is, and that a retry reuses its idempotency key while two
separate calls do not.

The API contract itself is pinned on the other side, by the ToxicFilter application's own
suite, which runs the PHP client against its real routes. Recorded fixtures would agree
with the API on the day they were written and drift silently afterwards.

## Author

Created by [Edu Lazaro](https://edulazaro.com) for [ToxicFilter](https://toxicfilter.com),
the moderation API this client speaks to.

## License

The ToxicFilter Python SDK is open-sourced software licensed under the [MIT license](LICENSE).
