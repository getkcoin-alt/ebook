# Automation service

The ingestion pipeline. A file goes in; a validated, described, tagged, indexed
product comes out.

- **Port** 8007 · **Schema** `automation` · **17 endpoints** · **130 tests**

---

## Thirteen stages, each checkpointed

```
collect → validate → extract_metadata
       → generate_description → seo → tags
       → thumbnail → convert → compress → watermark
       → upload → index → publish
```

Every stage writes its own row in `job_stages` with its output, its duration and its
outcome. That is the whole reliability story: **a job that dies at stage 11 resumes at
stage 11**, rather than redoing ten minutes of CPU and three dollars of tokens.

It is also what makes `acks_late=True` safe. A worker killed mid-deploy has its task
redelivered; without the checkpoint that would mean re-running the whole pipeline
including the side effects that already landed.

Stage rows are created **up front, all thirteen, as `pending`**. A history that
materialises rows as it goes cannot distinguish "stage 9 has not run yet" from "stage
9 was never part of this job" — and that is exactly the distinction someone staring at
a stuck job needs.

### One Celery task per job, not one per stage

The obvious design is a thirteen-link chain. This runs the whole job in one task,
because:

- **The resume story is identical** — it comes from the checkpoint either way. A chain
  does not resume from a checkpoint; it resumes from wherever it broke.
- **A chain is thirteen chances to lose the thread.** Every hop can be redelivered,
  dropped past a visibility timeout, or orphaned when a worker dies between finishing
  one link and queueing the next. The failure mode is a job that is neither running
  nor failed, with nothing looking for it.
- **The source bytes stay in memory across stages.** A chain would either re-download
  a 300MB file at every hop or pass it through the broker. The first turns ten stages
  into ten downloads; the second puts a book in Redis.

The cost is one worker slot held for the job's duration, paid for with a dedicated
queue so a twelve-minute conversion never sits in front of a 200ms email.

---

## Three ways a stage can not-succeed

The distinction between them is the entire failure policy.

| Outcome | Meaning | What happens |
|---|---|---|
| **Skipped** | Nothing to do. No cover image in the source; no AI provider configured; watermarking off. | Recorded with its reason. Pipeline continues. |
| **Terminal** | The input is wrong and will be wrong next time. A corrupt PDF; an unsupported format; an encrypted file. | Job fails immediately, no retry. |
| **Transient** | Anything else — a timeout, a 503, a bucket blip. | Checkpointed where it stands, requeued with jittered backoff, resumes at this stage. |

Retrying a corrupt PDF five times with exponential backoff spends twenty minutes
reaching the same answer and buries the real reason under four duplicate log lines.
That is why terminal is a category and not just "failed".

**Skipped is not failed.** They lead to different operator actions — "no AI provider
configured" is a deployment decision, "the model returned garbage" is a bug — and a
dashboard that renders both red trains everyone to ignore red.

### Required versus enrichment

`REQUIRED_STAGES` is `collect`, `validate`, `extract_metadata`, `upload`, `index`,
`publish`. Everything else is enrichment: an **optional stage that fails does not fail
the job**. A book with no AI tags is publishable; blocking on a model provider's bad
afternoon turns one outage into a backlog nobody can clear.

`index` is in the required set because it is the stage that **writes the pipeline's
output back to the catalogue**. The search push inside it is already best-effort and
swallows its own failures, so the only way that stage fails is the catalogue write
failing — and a job reporting success having written nothing back has produced
nothing. (This one was caught by a test, not by design.)

### Backoff lives in the row

`next_attempt_at` is a timestamp the claim query filters on, not a `sleep` inside a
worker. A worker holding a task through a ten-minute backoff is a worker doing nothing
while the queue grows behind it. The delay is **jittered** — without it, five hundred
jobs that failed together because a provider went down all become eligible at the same
instant and knock it over again the moment it recovers.

`requeue_stale` covers the case `acks_late` cannot: a worker that acked, started, and
was then SIGKILLed. That job sits at `RUNNING` forever with nobody looking at it, and
the heartbeat is what distinguishes it from a job legitimately spending twelve minutes
on a large conversion.

---

## Reading untrusted files

Everything this service parses was uploaded by somebody. Two rules run through
`services/documents.py`:

**The extension is a claim; the magic bytes are evidence.** A file named `book.pdf` is
a file someone *named* `book.pdf`. Format is decided from the header, and a mismatch
is recorded as a warning rather than a failure — plenty of real files are misnamed —
but never ignored, because a pipeline that trusts the name will eventually hand a zip
to a PDF parser and report a stack trace instead of an answer.

**Every bound is checked before the work it bounds.** Checking the decompressed size
of an EPUB after unzipping it is not a check; the memory is already gone.

Specifically:

- **Zip bombs.** An EPUB is a zip, and a 2MB archive that expands to 40GB passes every
  upload size check — the upload really was 2MB. The archive directory's declared
  sizes are summed **inside the loop**, so it stops before reading the entry that
  would exhaust memory rather than after totalling every entry.
- **Path traversal.** An entry named `../../etc/passwd` is refused at the boundary.
  Nothing here writes archive entries to disk, but an entry name becomes part of a
  derived storage key.
- **Billion laughs.** `ElementTree` has not resolved external entities since Python
  3.7, so XXE is not reachable — but it still expands *internal* ones, and a 1KB
  document can become gigabytes. A declared entity in an OPF or a container has no
  legitimate purpose, so it is refused outright.
- **Image decompression bombs.** A 60000×60000 PNG is a few KB on the wire and 14GB
  decoded. Pillow reads the header on `open`, so the dimensions are checked before the
  pixels are allocated.
- **Encrypted PDFs.** An empty-password decrypt is attempted first, because a PDF with
  only an *owner* password (print/copy restrictions) opens fine and there is nothing to
  circumvent. A real user password means the file cannot be read, and the job says so.

The container image runs non-root. This is the one service on the platform where a
memory-safety bug in a native library is reachable from user input at all — which is
also why there is **no PDF renderer**: no poppler, no mupdf. Covers are extracted from
the first page's embedded images rather than rasterised. That is a real limitation
(a typeset title page yields no cover) in exchange for a much smaller image and one
fewer class of bug.

---

## What the transform stages will and will not do

**Compression is lossless and conditional.** It deduplicates identical objects and
recompresses content streams. It does **not** downsample embedded images, which is
where the large wins usually are — that is lossy, and silently degrading the file a
customer paid for is not a decision a compression stage gets to make. And a rewrite
gaining less than `MIN_COMPRESSION_GAIN` is skipped entirely: every byte that differs
from the master is a byte that can be wrong, and a 1% saving does not buy that.

**The watermark is metadata, not an overlay.** A visible watermark degrades the thing
the customer bought and is removed in seconds by anyone who wants it gone — so it
annoys honest readers and stops nobody. Per-recipient tracing is genuinely useful, but
it has to happen at *download* time with the buyer's identity in it, which is the
books service's job. What this stage sets is provenance on the master: producer,
creator, and a marker that the file came out of this pipeline.

**"Convert" produces a sample, not a format conversion.** PDF→EPUB needs a full layout
engine and produces a reflowed file no publisher has approved; a bad automatic EPUB of
a design-heavy book is worse for a customer than no EPUB. What is safe and useful is
the first pages — the same file, just less of it — capped at a quarter of the book, and
skipped entirely for short ones. A ten-page preview of a twelve-page pamphlet is a
giveaway, not a preview.

**The source object is never overwritten.** Derived artefacts are written beside it.
The uploader's original is the only thing here that cannot be regenerated, and the
moment a compression bug is discovered it is what makes everything recoverable.

**Derived keys are job-addressed, not random.** A stage re-run after a redelivery
overwrites the object it wrote last time; a random key would leave the first orphaned
in the bucket with nothing referencing it and nothing to clean it up.

---

## Nothing publishes itself

`AUTO_PUBLISH` defaults to **off**. A finished job leaves the book ready for a human
to approve.

A pipeline that publishes whatever it is handed puts machine-written copy in front of
customers with nobody having read it, and the first time a model hallucinates an award
into a description it is on the storefront. Approving is one click. That click is the
difference between an assistant and an unsupervised publisher.

The catalogue patch sends **only fields the pipeline actually produced**. Sending
nulls for the rest would clear an editor's hand-written description the moment an AI
stage was skipped — precisely the case where the human text is all there is.

---

## Bulk import

`dry_run` defaults to **true**. An import is typically a spreadsheet an editor exported
and edited by hand, and its errors are systematic — a shifted column, a decimal point
in a price — so they are in *every* row. Finding that out from a validation report
costs nothing. Finding it out from four hundred wrong books costs an afternoon and a
lot of trust.

**Rows are independent.** One bad row is reported with its line number from the
uploaded file and the rest proceed. All-or-nothing means a single typo on line 287
sends an editor back to the start.

The status is one of three, not two: `partial` exists because reporting an import
where a fifth of the rows failed as "completed" hides the work somebody still has to
do.

A row with no `source_key` creates a catalogue entry and queues no job — a
metadata-only backfill for books whose files arrive later.

---

## Endpoints

### Operator (`/v1/automation`, needs `automation:run` / `automation:read`)
`POST /jobs` · `GET /jobs` · `GET /jobs/{id}` · `POST /jobs/{id}/retry` ·
`POST /jobs/{id}/cancel` · `POST /jobs/{id}/run` · `GET /stats` ·
`POST /imports` · `GET /imports` · `GET /imports/{id}`

There is **no public surface at all**. A customer has no reason to know a pipeline
exists, and a job's stage history names storage keys and sibling services.

`POST /jobs` takes a **storage key, never a file body**. A 400MB upload through the
gateway is a request that cannot be retried and a proxy buffer nobody sized for it —
and the file is already in object storage by the time anyone asks for this. A second
live job for the same key is a 409.

`POST /jobs/{id}/retry` resumes from the last completed stage. Pass `from_stage` when
a stage *succeeded* but produced something wrong — a description written from a bad
excerpt, a cover taken from the wrong page. Without it the resume starts after the last
success and reproduces exactly the same output.

`GET /stats` reports `failures_by_stage`, which is the number that says what to fix
first; the job status only says that something failed. The duration is a **median** —
one forty-minute job on a 900-page scan drags a mean away from what a typical run
costs, and the typical run is what capacity planning needs.

### Internal (HMAC-signed)
`POST /internal/jobs` · `GET /internal/jobs` · `GET /internal/jobs/{id}` ·
`POST /internal/jobs/{id}/cancel` · `POST /internal/maintenance/drain` ·
`POST /internal/maintenance/requeue-stale` · `POST /internal/maintenance/prune`

### Events consumed
`book.created` — when it carries a source key. Deliberately a short list: an event that
starts expensive work is a way for any producer on the platform to spend this service's
CPU.

Two idempotency guards, and both are needed. The `processed_events` row catches a
redelivery of the *same* event; the partial unique index on `automation_jobs` catches
two *different* events asking for the same file.

---

## Running it

```bash
cp .env.example .env
alembic upgrade head
python -m main                    # http://localhost:8007/docs
```

Jobs run on a worker, not in the API process:

```bash
celery -A tasks.celery_app worker -Q automation --concurrency=2
```

For local work, `INLINE_EXECUTION=true` runs them in the API process instead. That is
a development convenience and the service logs a warning about it at startup — it puts
a CPU-bound job on the event loop serving requests.

Tests need no broker, no bucket, no model provider:

```bash
PYTHONPATH=. pytest tests/ -q
```

The **documents in the tests are real**: `make_pdf` builds an actual PDF with content
streams pypdf has to parse, `make_epub` builds a spec-shaped EPUB zip, and
`make_zip_bomb` builds a small archive that declares a huge expansion. A fixture that
handed the pipeline a dict called "pdf" would pass while proving nothing about the one
part of this service that touches untrusted input.

---

## Things worth knowing before you change this

**`job.result` is reassigned, never mutated.** SQLAlchemy does not track in-place
changes to a plain JSON column, so `job.result.update(...)` is silently discarded at
flush — and the next resume rebuilds from a document missing everything that run
produced.

**Underscore-prefixed stage outputs are transient.** `_pending_uploads` carries
artefact *bytes* from the stage that made them to the stage that writes them. The
runner strips those keys before checkpointing; persisting them would push megabytes of
binary through a JSON column, and would fail before it got that far.

**`AutomationJob.completed_stages` never triggers a lazy load.** It is read during
response serialisation, and a lazy load there happens outside SQLAlchemy's greenlet
context under asyncio — which does not degrade to a slow query, it raises
`MissingGreenlet` and turns every job response into a 500.

**`COLLECT` re-runs on every resume**, even though it succeeded. Its output is the
source bytes, which live in process memory and not in the checkpoint, and every later
stage needs them.

**Pruning keeps dead-lettered jobs.** They are the ones somebody still has to look at.
Pruning the failures while retaining the successes is exactly backwards for a table
whose reason to exist is explaining what went wrong.

**`SEARCH_INDEXING_ENABLED=false` does not skip the `index` stage.** That stage also
writes the catalogue patch, and skipping it would silently discard everything the
pipeline produced. The search call inside it is the part that no-ops.
