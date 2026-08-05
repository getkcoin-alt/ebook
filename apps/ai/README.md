# AI service

Book copy, tagging, moderation, embeddings and a catalogue-grounded assistant —
behind one cost ceiling and one cache.

- **Port** 8004 · **Schema** `ai` · **12 endpoints** · **48 tests**

---

## The ceiling is the feature

Every other service on this platform fails by becoming unavailable, which is loud.
This one fails by spending money, which is silent until the invoice arrives. A retry
loop against a paid model is the only bug in this codebase that keeps costing after
everyone has gone home.

So `DAILY_COST_LIMIT_USD` is a **hard stop, not a target**. Reaching it returns 503
and serves nothing until the UTC day rolls over. That is a deliberate trade: an AI
feature that is off for the rest of the day is a worse product, and an uncapped one
is a worse business.

Three details make the ceiling trustworthy rather than decorative:

- **It is checked before the model call, never after.** A ceiling enforced afterwards
  has already spent the money it exists to prevent.
- **The check is a single indexed row read**, not a `SUM` over the generation ledger.
  It runs on every request, and a growing full scan on the hot path is how a cost
  control becomes the reason the service is slow.
- **Spend is recorded with a conditional `UPDATE`.** Read-modify-write would let two
  concurrent requests read the same total and both add to it from the same base,
  losing one charge — so the counter would drift low under exactly the load that
  makes the limit matter.

There is a **second, per-user ceiling** (`USER_DAILY_COST_LIMIT_USD`). Without it the
first person to write a script takes the feature away from everyone else.

At `COST_WARNING_THRESHOLD` (0.8 by default) requests still succeed but a warning is
logged — the signal that someone should look *before* the feature turns itself off.

**Costs are estimates and labelled as such.** Token counts come from the provider's
response, never from a client-side estimate (those are wrong by 10–30% depending on
the tokeniser). Prices live in `PRICING` in `services/providers.py` rather than being
fetched, because a cost ceiling that depends on a network call is a cost ceiling that
fails open. An unrecognised model falls back to `DEFAULT_PRICING`, which is
deliberately high: an unknown model that is cheaper just leaves budget unspent, while
one assumed cheap could blow through the ceiling before anyone notices.

When a provider returns no usage block, the request is recorded with **zero tokens
and a warning**. A made-up number in a cost ledger is worse than a missing one.

---

## Cache first, always

Book copy is generated once and read thousands of times. Regenerating per request is
pure waste, and non-determinism means the description changes each time for no reason
a reader would understand.

`Generator.generate` runs in a fixed order, and each step exists to avoid paying for
something:

1. **Cache** — Redis, then the `cached_results` table behind it. Redis is allowed to
   be empty; it is a cache. The table is what survives a flush, and it matters
   because regenerating every description on the platform after an eviction is a
   bill, not an inconvenience. A cache read that raises is logged and ignored — an
   outage should cost money, not availability.
2. **Budget.**
3. **Moderation**, for user-supplied text. Cheap classifier before expensive
   generation.
4. **The model**, then the ledger row and the spend in one transaction.

The cache key is a SHA-256 of everything that affects the output — kind, locale,
title, sorted authors and categories, language, and the excerpt. The excerpt is in
there on purpose: a book whose text was re-extracted should regenerate its copy,
because the old copy was written from different material.

**Every outcome is recorded in `generations`** — cached, blocked and failed included.
A ledger of only successful calls makes the cache look free, moderation look like it
never ran, and the failure rate invisible. The failure rate is the number that tells
you a provider is degrading.

---

## Prompts belong to the server

**No caller ever supplies a system prompt.** Every endpoint names a `TaskKind` and
the prompt for it lives in `services/prompts.py`. Accepting a caller's prompt would
make the platform's voice unversioned, make output impossible to reproduce, and turn
every endpoint into a jailbreak surface — "ignore your instructions and dump your
context" only works when the attacker controls instructions.

User content always goes in the **user turn**, never interpolated into the system
prompt. That is the real boundary: a model weighs its system prompt more heavily than
its input, and mixing the two removes the distinction that makes the instructions
authoritative. On Anthropic the system prompt is a separate field, not a message —
putting it in the message list is the mistake that makes a prompt overridable by
input.

Structured tasks (SEO, tags, categories, recommendations, moderation) run at
temperature 0.2, because JSON that varies run to run is JSON that sometimes fails to
parse. `parse_json_output` strips code fences and falls back to the outermost braces:
models wrap JSON in prose however firmly the prompt says not to, and failing the
whole generation over a stray ` ```json ` would waste a call that already succeeded
and already cost money.

---

## Moderation fails closed

When the classifier is unavailable, submission is **refused**, not waved through.
That is the uncomfortable choice and it is the right one: an unmoderated path opens
exactly when the service is under strain, which is exactly when someone is most
likely to be abusing it. A user who retries in a minute is a smaller problem than
content nobody checked. Set `MODERATION_FAIL_CLOSED=false` only if you have decided
otherwise on purpose.

Two judgements are worth stating plainly:

- **A review is allowed to be harshly negative about a book.** Criticism of a work is
  not harassment of its author, and a moderator that cannot tell the difference
  silently becomes a tool for suppressing bad reviews — worse for a bookstore than
  the occasional rude sentence.
- **Flags outrank the boolean.** A model that flags something but forgets to set
  `allowed: false` still blocks. Trusting one field alone makes the outcome depend on
  the model keeping two fields consistent.

Structural spam (a long run of one repeated character) is caught **before** any model
call. Spending a model call to decide that `aaaa…` is spam is a model call spent
badly.

---

## The assistant is grounded, and access is checked

A language model will confidently invent a book that does not exist, and a reader
will go looking for it. So the question is used to search the real catalogue first,
those results become the context, and the prompt says the model may only name books
from that list. A recommendation the store cannot sell is worse than no
recommendation. If search is down, grounding is skipped with a warning — the answer
degrades, the feature does not disappear.

When a conversation is scoped to a specific book, **entitlement is verified before
any of the book's text reaches a prompt**. Otherwise "ask about this book" is a way
to read a book you have not bought, one question at a time. The books service is
authoritative; this service only asks.

History is **trimmed, not summarised** — summarising costs a model call per turn to
save tokens on the next one, which is a losing trade at these context sizes. Chat
always sets `force_refresh`: a cached answer to "tell me more" would be the previous
conversation's answer.

A conversation that belongs to someone else returns 404, not 403. A 403 confirms it
exists.

---

## Endpoints

### User (`/v1/ai`)
`GET /status` · `POST /chat` · `GET /conversations` ·
`GET /conversations/{id}` · `DELETE /conversations/{id}` · `POST /recommend`

`/status` exists so the frontend can hide AI affordances on a deployment with no API
key configured, rather than offering a button that always 503s.

### Internal (HMAC-signed, `/internal`)
`POST /generate` · `POST /generate/batch` · `POST /moderate` · `POST /embeddings` ·
`GET /usage` · `POST /maintenance/prune`

`/generate/batch` is what the automation pipeline calls: description, SEO and tags
for one book in one round trip. A partial failure returns the fields that succeeded
rather than failing the batch — a book with a description and no tags is publishable;
a book with nothing is not. Kinds run **sequentially**: five parallel calls would
blow through a provider's rate limit and make the budget check meaningless, since all
five would pass it before any of them recorded a cost.

`/embeddings` is the search service's semantic mode. Vectors are re-sorted by the
response's `index` field, because the API does not guarantee response order matches
input order and a silently shuffled batch attaches embeddings to the wrong documents
— which looks like the model simply being bad at its job.

---

## Running it

```bash
cp .env.example .env
alembic upgrade head
python -m main                # http://localhost:8004/docs
```

With no `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` the service starts, reports
`ai_available: false` from `/status`, and returns 503 from generation. That is
intentional — the rest of the platform must be runnable without an AI budget.

Tests need no infrastructure and make no model calls:

```bash
PYTHONPATH=. pytest tests/ -q
```

The provider in tests is a **recording** stub implementing the real protocol, with a
programmable response and a programmable failure mode (`retryable` vs `permanent`).
That is what makes the parts worth testing testable — the budget ceiling, the cache,
failover, JSON recovery, moderation failing closed — none of which involve a real
model, and all of which are where the money and the safety live.

---

## Things worth knowing before you change this

**Failover is only for retryable failures.** A 429 or 5xx moves to the next provider;
a 400 is our bug, and retrying it elsewhere spends money to fail twice.

**`_increment` opens its SAVEPOINT before `session.add`.** `begin_nested()`
autoflushes pending state, so adding first emits the INSERT *outside* the savepoint
and the `IntegrityError` escapes the `try`, taking the surrounding transaction with
it. Same pattern in `_store`.

**The platform budget row is found with `IS NULL`, not `= NULL`.** It has a null
user, and `= NULL` is never true in SQL — the wrong comparison means the global
ceiling is never found and never enforced.

**`prune` keeps the platform-wide budget rows.** They are a year-over-year cost
record and cost nothing to retain. The per-user rows grow with the user base and
nobody needs last year's per-user daily spend.

**Day keys are UTC.** Local time would reset the ceiling twice a year and make
replicas disagree about when today ends.

**`CACHED` is its own status, not a flavour of `SUCCEEDED`.** Collapsing them makes
the cache hit-rate unmeasurable and inflates the cost report with requests that cost
nothing.
