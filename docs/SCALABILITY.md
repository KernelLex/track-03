# Scalability

This is a plan, not a migration. The explicit instruction behind this
document was "scaling should be in the picture, not that we need to scale
it now — make the infra and code scalable, that's all." Nothing below has
been built; it exists so the path forward is concrete instead of a vague
"and then it scales" wave of the hand, and so a decision to actually
migrate can be made deliberately, not discovered under production load.

## What's true today, honestly

The current build runs as a **single FastAPI process** (`uv run trucommit
serve`) against **per-file SQLite databases** — `events.db` (webhook
dedup), `ledger.db` (the hash-chained action ledger), and a matching
`*_outbound.db` per ledger (outbound idempotency claims). Every connection
is opened with `PRAGMA journal_mode=WAL`, which already buys real
concurrent-reader support and crash-safe writes — this is not a toy
setup, and SQLite in WAL mode genuinely handles a meaningful write rate
(low thousands of writes/second is realistic for this codebase's
write shape). The real ceiling isn't SQLite's write throughput; it's
**one file, one machine**: every process that wants to read or write the
ledger has to run on the box that file lives on, and only one writer
transaction commits at a time.

For a hackathon demo and an early pilot with a handful of merchants, this
ceiling is nowhere close — the honest answer to "does this scale to the
demo" is yes, already. The plan below is for what changes if this becomes
many merchants, many concurrent webhook sources, and a real uptime
requirement.

## The three things that actually need to change, in the order they'd bite

### 1. The database: SQLite files -> Postgres, schema-compatible

Every table in this codebase (`events`, ledger entries, `outbound_actions`,
the recovery ledger's `UNIQUE(payment_id)` table) is plain SQL with no
SQLite-specific syntax beyond `AUTOINCREMENT` and `strftime()` defaults —
both have direct Postgres equivalents (`SERIAL`/`GENERATED ALWAYS AS
IDENTITY`, `now()`). None of the business logic (`Ledger.verify_chain()`,
`OutboundActionStore.claim()`, `EventStore.record()`) depends on
SQLite-only behavior; the `UNIQUE` constraints that make claim-then-act
and redelivery-dedup safe are standard SQL and work identically under
Postgres's MVCC. This is a connection-string change and a migration
script, not a rewrite — the honest reason it isn't done now is that it
adds a real external dependency (a Postgres instance to run, back up, and
pay for) that a single-demo-machine setup doesn't need yet.

**What this unlocks**: multiple app processes (see #2) sharing one
database, real concurrent writers, point-in-time recovery, and a
managed-service option (RDS, Cloud SQL, Neon, Supabase) instead of a file
that lives and dies with one machine's disk.

**What has to be added, not just swapped**: every `debtor_id`/`invoice_id`
in this build is a bare string with no `merchant_id` scoping — fine for
one demo merchant, wrong the moment a second merchant's data lives in the
same tables. A real migration adds a `merchant_id` column to every table,
foreign-keys `debtor_id` to a real per-merchant debtor table, and adds it
to every `UNIQUE` constraint (`UNIQUE(merchant_id, source, event_id)`,
not just `UNIQUE(source, event_id)`) — otherwise two merchants' webhooks
with coincidentally identical `event_id`s would collide. This is the
actual multi-tenancy line, and it's a schema decision, not an
infrastructure one — worth getting right before the first second merchant
onboards, not after.

### 2. The orchestrator: synchronous in-request -> a real queue

`_maybe_orchestrate()` currently runs DIAGNOSE -> DECIDE -> BOUNDS -> ACT
**inline, inside the webhook HTTP handler**, before the response is
returned. That's exactly right for a demo (the response literally shows
the orchestration result) and exactly wrong under real load: a slow rail
call (Razorpay latency, a network hiccup) blocks that request thread, and
a burst of webhooks — a real merchant's whole day of failed payments
landing at once, say — serializes behind whatever the rail's response
time is. The fix is standard and doesn't touch the pipeline logic itself:
the webhook handler's job becomes "verify, dedupe, enqueue, return 200
immediately," and a separate worker pool (Celery, RQ, or even a
lightweight `asyncio` task queue for a first step) calls the exact same
`run_pipeline()` this build already has, off the request path. Nothing in
`agent/orchestrate.py` assumes it's being called synchronously from a
request handler — it's already a plain function taking a ledger, a rail,
and a diagnosis, which is exactly the shape a queue worker calls.

**Why this matters for correctness, not just speed**: Razorpay (and Meta,
for WhatsApp) retry a webhook that doesn't get a fast 2xx. An orchestrator
running inline means a slow rail call risks the *webhook* timing out and
being redelivered — `EventStore`'s dedup already makes a redelivery safe,
but it's cleaner not to rely on that safety net for something a queue
avoids by construction.

### 3. The API layer: one process -> stateless, horizontally scaled

`app.state.orchestrator_ledger`/`orchestrator_store`/`orchestrator_rail`/
`orchestrator_channel` are all constructed once in `lifespan()` and held
as long-lived connections on a single process. Once the database is
Postgres (#1) and orchestration is queued (#2), the FastAPI process itself
becomes closer to stateless — it verifies a signature, writes a dedup
record, enqueues a job, returns. That's the shape that scales
horizontally behind a load balancer (multiple `uv run trucommit serve`
processes, or containers, all pointing at the same Postgres) without any
per-process state to worry about losing. The in-process APScheduler
(`agent/auditor/scheduler.py`) would need to move to a single designated
worker (or a real scheduler like Postgres-backed `pg_cron` /
a dedicated Celery-beat process) once there's more than one API process —
running the Auditor's sampling job N times on N processes is wasteful,
not wrong, but worth fixing before N gets large.

## What does *not* need to change

- **The ledger's hash-chain design** (`agent/ledger/store.py`) is
  already correct under concurrent writers — each entry's hash covers
  the previous entry's hash, and Postgres's transactional guarantees are
  strictly stronger than SQLite's for this purpose.
- **The bounds engine, EV arithmetic, diagnosis taxonomy** — all pure,
  stateless functions already. Scaling the infrastructure around them
  changes nothing about how they're called.
- **The channel abstraction** (`agent/notify/protocol.py`) — already
  decoupled from any particular scale assumption; adding a fourth or
  fifth `MessageChannel` implementation (SMS, email) is unrelated to any
  of the above.
- **The rail abstraction** — same reasoning; `RazorpayRail` and a future
  second payment processor's rail both already fit the same `Rail`
  protocol regardless of how many processes are calling it.

## A rough order of operations, if this is ever actually done

1. Add `merchant_id` to the schema and every relevant `UNIQUE` constraint
   (a real design decision, worth its own short doc when it happens —
   the correctness of multi-tenancy depends on getting this scoping
   right, not on the database engine underneath it).
2. Stand up Postgres, port the schema (the SQL is already
   near-portable), point `TRUECOMMIT_LEDGER_DB`-equivalent config at a
   connection string instead of a file path.
3. Move `_maybe_orchestrate()`'s body behind a queue; the webhook handler
   enqueues instead of calling `run_pipeline()` directly.
4. Make the API layer stateless and run more than one instance behind a
   load balancer; move the Auditor's scheduled jobs to a single worker.

Each step is independently useful and independently deferrable — there's
no "big bang" migration implied here, and steps 1-2 alone (multi-tenant
schema on Postgres, still one process, still synchronous orchestration)
would already comfortably serve many merchants at real-world small-business
volume before step 3 or 4 became necessary.
