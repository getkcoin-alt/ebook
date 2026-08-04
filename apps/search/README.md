# Search service

Full-text search, autocomplete, facets, related books, trending queries and search
analytics — backed by Meilisearch.

- **Port** 8003 · **Schema** `search` · **14 endpoints** · **96 tests**

---

## Two ideas hold this service together

**The index is a cache, not a source of truth.** Every document in Meilisearch can
be rebuilt from the books service. Nothing is stored here that cannot be regenerated
— which is what makes it safe to empty an index during an incident, and why the
`search` schema contains only the index ledger and analytics.

**A search outage is not a platform outage.** Meilisearch being unreachable produces
clean 503s on `/v1/search` and nothing else. The service still starts, still passes
liveness, and still answers `/v1/admin/search/health` so an operator can see what is
wrong. Index setup at boot is best-effort for the same reason: a search engine that
is down must not stop the service from starting.

That second idea is why the timeouts are short and explicit. A hung request to
Meilisearch holds a worker slot open until it times out, and enough of those turn
"search is slow" into "the gateway's connection pool is exhausted."

---

## The query path

A search box is the most hostile input surface on the platform, so everything a
caller can influence is validated against a closed set *before* it reaches the
engine. A crafted query is a 400 from us, not a 400 from Meilisearch surfacing as a
500.

**Filters** come from `FILTERABLE` — a fixed map of API names to index attributes.
Values are quoted and escaped, backslashes before quotes (reversing that order
double-escapes the backslashes the quote replacement introduces and lets a crafted
value break out). They are built as Meilisearch's **array form**, where outer
elements AND and a nested array ORs — structurally, not by string concatenation,
which is what removes the operator-precedence bugs.

`status = "published"` is appended unconditionally, after the caller's filters. No
combination of query parameters can surface a draft.

**Sorts** come from a closed enum mapped to server-declared expressions. A free-form
sort string could ask the engine to order by an attribute nobody made sortable —
which is a 400 from Meilisearch on a live search.

`assert_filters_are_indexable()` runs at startup and fails fast if the API accepts a
filter the index never declared filterable. Without it, that mismatch ships and
surfaces as a broken facet in production.

**Pagination** is cursor-based over an offset. The body asks for `limit + 1`
documents, so "is there a next page?" is answered without a second query or a
reliance on the estimated total. Paging past Meilisearch's scan cap is a 400 with an
explanation, because a silently empty page is much worse to debug.

`estimated_total` is exactly that — computed from a capped scan. Present it as "about
N results", never as an exact count.

---

## Indexing

Catalogue changes arrive as **events**, not as a call from the books service. An
index that is briefly stale is a far smaller problem than a publish that fails
because search was mid-deploy.

Delivery is at-least-once, so the handler is idempotent by construction:

- **A ledger row per document** (`indexed_documents`) holds a checksum. An unchanged
  document is skipped without a request to the engine — which is what makes an
  hourly reconcile cheap enough to actually run.
- **The document id is the book id**, so even a duplicate write converges on one
  document rather than creating a second.

A book that was deleted between the event being published and the event being
handled is *removed from the index*, not dead-lettered. That race is exactly what
the reconciler exists to resolve.

### Rebuilds

| Mode | What it does | When |
|---|---|---|
| `reconcile` | Walks the source, repairs only what diverged | Scheduled, hourly |
| `full` | Empties the index first, then refills | Operator action, after a mapping or settings change |

Both take a **distributed lock**. Two replicas rebuilding one index at once would
each delete the other's freshly written documents, and the symptom is a search index
that empties itself for no apparent reason.

Batching is backpressured: `INDEX_BATCH_SIZE` documents per request, at most
`INDEX_MAX_CONCURRENT_BATCHES` in flight, with a short pause between waves so
Meilisearch's task queue can drain. A reindex that saturates that queue starves the
live updates arriving from book events.

---

## Analytics

`search_queries` and `search_clicks` exist for one report: **what people search for
and do not find**. That list is books to acquire, synonyms to add and spellings to
handle, written by the people who wanted to buy them.

Recording **never fails a search** — every write is best-effort and swallows its
exceptions, on its own session so it cannot roll back or lock anything the request is
doing. A search that returned good results must not 500 because a counter was
unavailable.

A search returns a `query_id`, and a click posts it back. That pairing is the only
way click-through can be attributed; there is no way to reconstruct it afterwards.

Queries are normalised (lower-cased, whitespace-collapsed) before grouping, or
`Machine Learning` and `machine  learning` report as different searches.

Rows are pruned by the worker via `/internal/maintenance/prune-analytics`. They
accumulate faster than anything else here — one per completed search — and a
year-old query tells you nothing a month-old one does not.

---

## Trending

Hourly Redis sorted sets, merged at read time with a decay weight per bucket age.

The obvious implementation is one ZSET with an exponentially growing score — the
Hacker News trick. It works, and then six months later the exponent overflows a float
and the entire ranking becomes `inf`. Rescaling to avoid that is a background job
nobody writes.

Buckets have none of that: each is a plain count with a TTL, decay is applied on
read, and the data expires itself. `0.9 ** 24` is about 8%, so a search from
yesterday counts for roughly a twelfth of one from this hour — which is what makes
this *trending* rather than *all-time popular*.

The score is a ranking weight. Never display it as a volume figure.

---

## Semantic search

Off by default, and the service is fully functional without a single embedding.

`SearchBackend` is the seam. The query API, the filter builder, the facet mapping and
the response schema are all backend-independent, so enabling hybrid search is a
config flip — `SEMANTIC_ENABLED=true` — and disabling it during an incident is the
same flip in reverse. The default `KeywordBackend` adds a `hybrid` block to the same
request body; nothing else changes.

Enabling it in production requires an embedder registered with Meilisearch under the
name in `SEMANTIC_EMBEDDER`. Until then the flag is honoured but inert: a request
that opts in gets keyword results and an honest `semantic: false`, rather than a
silent claim it cannot back up.

---

## Endpoints

### Public
| Method | Path | Notes |
|---|---|---|
| `GET` | `/v1/search` | Facets, filters, cursor pagination |
| `GET` | `/v1/search/suggest` | Books + authors + categories, one round trip |
| `GET` | `/v1/search/trending` | Time-decayed ranking |
| `GET` | `/v1/search/related/{book_id}` | Works without embeddings |
| `POST` | `/v1/search/click` | Attributes a click to its search |

### Admin (`analytics:read` / `settings:write`)
Index health, reindex, run history, the analytics report, and clearing trending.

### Internal (HMAC-signed)
Direct document push and delete for the automation pipeline, scheduled reindex, and
analytics pruning for the worker.

---

## Running it

```bash
cp .env.example .env
alembic upgrade head
python -m main                # http://localhost:8003/docs
```

Tests need no infrastructure — in-memory SQLite, `fakeredis`, and a fake engine:

```bash
PYTHONPATH=. pytest tests/ -q
```

Meilisearch is replaced at the **HTTP transport**, not at the client. `MeiliClient`
builds every request and interprets every response exactly as it does in production,
and a small in-memory engine answers them. That is deliberate: the interesting bugs
here live in the request bodies — filter syntax, sort expressions, hybrid blocks —
and in the response mapping, and stubbing the client would test neither.

---

## Things worth knowing before you change this

**Adding a filter is two edits, not one.** `FILTERABLE` in `services/query.py` *and*
the filterable-attributes list in `services/indexes.py`. Meilisearch rejects a filter
on an attribute it was never told about, and the rejection lands on a live search.
`assert_filters_are_indexable()` catches the mismatch at startup — leave it wired up.

**The document mapper reads the books service's real field names.** `available_formats`
not `formats`, `ai_tags` not `tags`, `effective_price_minor` not `price_minor`, and a
publisher arrives as an object. Indexing `price_minor` makes every sale invisible to
the price filter and the price facet, which is a silent failure.

**Documents store storage keys, not URLs.** This service does not know the CDN base,
and baking one into every document makes them all wrong the day it changes.

**Ask the backend whether it used semantic search; don't infer it.** The backend
rewrites the body it was handed, so the copy the query service holds never carries
the hybrid block. `uses_semantic()` exists for exactly this.
