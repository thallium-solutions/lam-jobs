# @lam/jobs

`@lam/jobs` is the background-work toolkit for services written in the
[Lammergeier programming language](https://github.com/thallium-solutions/lammergeier-lang)
([lammergeier-lang.dev](https://lammergeier-lang.dev)). Everything is stored in
PostgreSQL, so any number of replicas of a service cooperate through the
database they already have:

- **Leases** (`JobLeases`): named, expiring locks with fencing tokens and a
  distributed-singleton helper.
- **Scheduling** (`JobScheduler`): periodic (`every`) and 5-field cron jobs,
  run **once per due time across all instances**, never overlapping a slow
  run, with a test clock (`tick(nowUnix, force)`) and a background loop with
  graceful `stop()`.
- **Job health** (`JobHealth`): last success/failure, consecutive failures,
  last error, duration and counts per job, plus status-page and metrics
  queries.
- **Instance heartbeats** (`InstanceHeartbeat`): which processes of which
  component are alive.
- **Durable queue** (`JobQueue`, `JobWorkers`): JSON payloads, priorities,
  delays, de-duplication keys, enqueue inside your own transaction,
  `FOR UPDATE SKIP LOCKED` claims with expiring leases, exponential backoff
  with jitter, dead letters, retry/cancel/purge, statistics, a worker pool
  with per-job timeouts and graceful drain, and LISTEN/NOTIFY wake-ups with a
  polling fallback.

The library owns its schema (`Jobs.migrate`), so applications do not copy
SQL migrations around.

## Why

Most services grow the same pieces: "run this every five minutes, but only on
one replica", "retry this webhook with backoff and give up after a day",
"show which jobs are failing on the status page". Written ad hoc they usually
miss a race (two replicas run the job, a crashed worker's job is lost, a
batch claims more rows than intended). `@lam/jobs` packages tested answers
built only on PostgreSQL features: row locks, `ON CONFLICT`,
`FOR UPDATE SKIP LOCKED`, advisory locks and `LISTEN/NOTIFY`.

## Requirements

- `lamc` 3.0 or newer (`>=3.0, <5`).
- PostgreSQL 12 or newer (tested with 16). Other `lamdb` drivers are refused
  with `JobsConfigError`.
- No Go modules beyond those already linked by the standard library: the
  database driver is `lamdb`'s `github.com/lib/pq`, also used for
  LISTEN/NOTIFY.

## Installation

Add the dependency to your `lamlib.toml`:

```toml
[dependencies]
"@lam/jobs" = { git = "https://github.com/thallium-solutions/lam-jobs.git", ref = "1.0.1" }
```

or install it directly:

```bash
lamc install https://github.com/thallium-solutions/lam-jobs.git@1.0.1
```

and import what you need:

```lammergeier
from @lam/jobs import Jobs, JobLeases, JobScheduler, JobHealth, InstanceHeartbeat, JobQueue, JobWorkers
```

## Quick start

```lammergeier
from @lam/jobs import Jobs, JobScheduler, JobQueue, JobWorkers, QueuedJob, InstanceHeartbeat
from lamdb import Db
from lamerrors import Result
from lamenv import Env

func cleanup() -> Result[int] {
    # ... delete expired rows; the count is recorded in JobHealth.
    return Result.Ok(0)
}

func sendEmail(job: QueuedJob) -> Result[bool] {
    to: str = job.payload.to
    print(f"sending to {to}")
    return Result.Ok(true)
}

func main() {
    db: Db = Db.connect("postgres", Env.getOr("DATABASE_URL", "")).unwrap()
    _ = Jobs.migrate(db).unwrap()                # idempotent, safe from every replica

    beat: InstanceHeartbeat = InstanceHeartbeat(db, "worker", "1.4.2")
    beat.start()

    scheduler: JobScheduler = JobScheduler(db)
    scheduler.every("cleanup", 300, cleanup)
    scheduler.start()

    mail: JobQueue = JobQueue(db, "mail")
    workers: JobWorkers = JobWorkers(mail, sendEmail, 4)
    _ = workers.listen()                         # optional LISTEN/NOTIFY wake-ups
    workers.start()

    _ = mail.enqueue({"to": "ada@example.com"}).unwrap()

    # ... serve until shutdown, then:
    _ = workers.stop()                           # stop claiming, drain running jobs
    scheduler.stop()
    _ = beat.stop()
}
```

## Schema and migrations

`Jobs.migrate(db, prefix = "lam_jobs_") -> Result[int]` creates or upgrades
the library tables and returns how many schema versions it applied (0 when
current). Call it at every start from every instance: migrators serialise on
a transaction-scoped advisory lock (`pg_advisory_xact_lock`), every DDL
statement is idempotent, and applied versions are recorded in
`<prefix>schema_version`. `Jobs.ensureSchema` is the same returning
`Ok(true)`; `Jobs.schemaVersion` reports the installed version (0 = none);
`Jobs.dropSchema` removes everything (tests, uninstall).

The prefix (lower-case letters, digits, `_`, at most 40 characters) lets
several applications or test runs share a database; pass the same prefix to
every class. Schema version 1 creates:

| Table | Purpose |
|---|---|
| `<prefix>schema_version` | applied schema versions |
| `<prefix>leases` | `name`, `owner`, `fence`, `acquired_at`, `expires_at` |
| `<prefix>schedules` | one row per scheduled job: `spec`, `next_run_at`, `paused`, running lease (`running_owner`, `running_until`), last run, `runs` |
| `<prefix>health` | one row per job: last success/failure, `last_error`, `consecutive_failures`, last duration/count, totals |
| `<prefix>instances` | heartbeats: `component`, `instance`, `version`, `host`, `started_at`, `seen_at` |
| `<prefix>queue` | queue jobs: `queue`, `state`, `payload` (JSONB), `priority`, `unique_key`, `attempts`/`max_attempts`, `run_at`, claim (`locked_by`, `locked_until`), `last_error`, timestamps |

Applications using `lammigrate` keep their own migrations for their own
tables; there is nothing to copy from this library.

## API reference

All database operations return `Result`; programming errors (an invalid
prefix, a non-positive interval, a duplicate job name) throw `ValueError`.
Times are Unix seconds unless stated otherwise. Error kinds:
`JobsConfigError`, `JobsMigrationError`, `JobsNotFound`, `JobsListenError`,
`JobsTimeout`, `JobsHandlerException`, `JobsPermanentError`, and the
`lamdb` errors (`DbExecError`, `DbQueryError`).

### `Jobs` (schema and helpers)

| Member | Description |
|---|---|
| `Jobs.migrate(db, prefix = JobsDefaultPrefix) -> Result[int]` | create/upgrade the tables; versions applied |
| `Jobs.ensureSchema(db, prefix) -> Result[bool]` | `migrate`, `Ok(true)` |
| `Jobs.schemaVersion(db, prefix) -> Result[int]` | installed version, 0 when absent |
| `Jobs.dropSchema(db, prefix) -> Result[bool]` | drop every library table |
| `Jobs.checkPrefix(prefix) -> bool`, `Jobs.newOwner() -> str` | prefix validation; unique `host:pid:random` owner ids |
| `JobsDefaultPrefix`, `JobsSchemaVersion` | `"lam_jobs_"`, `1` |
| `JobsSql` | interface satisfied by `lamdb.Db` and `lamdb.Tx` (`exec`, `query`) |
| `JobsRow.int/str/bool(row, key)`, `JobsRow.errText(err)` | typed reads of `lamdb` rows, error text |

### `JobLeases(db, owner = "", prefix)`

| Member | Description |
|---|---|
| `acquire(name, ttlSeconds) -> Result[bool]` | take a free or expired lease, or extend our own; `Ok(false)` when held by another owner |
| `renew(name, ttlSeconds) -> Result[bool]` | extend a lease we still hold |
| `release(name) -> Result[bool]` | give it back now (the row stays so `fence` keeps increasing) |
| `holder(name) -> Result[str]` | current owner, `""` when free or expired |
| `holds(name)`, `info(name) -> Result[JobLease]`, `list()` | inspection (`JobLease`: `owner`, `fence`, `acquiredAt`, `expiresAt`, `remainingSeconds`, `held`) |
| `runExclusive(name, ttlSeconds, fn) -> Result[bool]` | run `fn` only while holding the lease, renewing it every `ttl/3`; `Ok(false)` when another instance holds it |
| `prune(olderThanSeconds) -> Result[int]` | delete long-expired leases |

The default owner is unique per `JobLeases` object; pass a stable owner (for
example a pod name) to re-acquire leases after a restart. Expiry uses the
database clock. `fence` increases whenever the lease changes hands: pass it
to downstream systems so they can reject writes from a stale holder.

### `JobScheduler(db, owner = "", prefix)`

| Member | Description |
|---|---|
| `every(name, intervalSeconds, fn, lockSeconds = 60) -> JobScheduler` | periodic job; first run at the first tick, then every `intervalSeconds` |
| `cron(name, expr, fn, lockSeconds = 60) -> JobScheduler` | cron job (UTC); invalid expressions throw `ValueError` |
| `tick(nowUnix, force = false) -> Result[dict[str, int]]` | run due jobs in this call; returns the jobs run here and their counts (-1 = failed) |
| `start()`, `run()`, `stop()` | background loop (each due job in its own task), blocking variant, graceful stop waiting for running jobs |
| `withPollInterval(ms)`, `quiet()` | loop interval (default 1000 ms); no failure logs |
| `pause(name)`, `resume(name)`, `trigger(name)` | for every instance; `trigger` makes a job due now |
| `get(name) -> Result[JobSchedule]`, `list()` | shared state (`spec`, `nextRunAt`, `running`, `runningOwner`, `lastOwner`, `lastStartedAt`, `lastFinishedAt`, `runs`, `paused`) |
| `register(nowUnix)`, `forgetOthers()` | create rows (done by `tick`); delete rows of jobs no longer registered |
| `health` | the `JobHealth` that records every run |

Job functions have the type `func() -> Result[int]`: return `Ok(count)` (any
number worth recording) or an error; exceptions are caught and recorded as
failures. A run happens only after an instance atomically moves the row's
`next_run_at` forward **and** takes the running lease (`lockSeconds`,
renewed every third of it while the job runs), so replicas never run the same
due time twice and never overlap a long run; a crashed instance's lease
expires after `lockSeconds`. Periodic jobs keep their cadence
(`next = previous + interval`) and skip missed runs after downtime instead of
catching up; cron jobs run at the next matching minute. Changing a job's
interval or expression reschedules it.

### `CronSchedule`

`CronSchedule.parse(expr) -> Result[CronSchedule]` (`CronParseError` on
malformed input), `CronSchedule.valid(expr)`, `next(afterUnix)` (first
matching minute strictly after, -1 when none within 8 years), `nextN(after,
count)`, `matches(unix)`. Fields are `minute hour day-of-month month
day-of-week`, each `*`, `?`, a number, a range `a-b`, a step `*/n`, `a-b/n`
or `a/n`, or a comma-separated list of those. Months accept `JAN`-`DEC`,
weekdays `SUN`-`SAT` (case-insensitive; 0 and 7 are Sunday). When both day
fields are restricted, a day matches if either does (Vixie cron). Macros:
`@yearly`/`@annually`, `@monthly`, `@weekly`, `@daily`/`@midnight`,
`@hourly`. All times are UTC.

### `JobHealth(db, prefix)`

| Member | Description |
|---|---|
| `success(job, durationMs = 0, count = 0)`, `failure(job, error, durationMs = 0)` | record an outcome |
| `record(job, failure, durationMs, count)` | `failure == ""` is a success (drop-in for MFT's `heartbeat(job, failure)`) |
| `measure(job, fn) -> Result[int]` | run `fn`, record outcome and duration, return `fn`'s result |
| `get(job) -> Result[JobStatus]`, `list()`, `failing(threshold = 3)`, `stale(maxAgeSeconds)` | queries |
| `oldestSuccessAge() -> Result[int]` | seconds since the least recent last success (-1 = none) |
| `gauges(threshold = 3) -> Result[dict[str, int]]` | `jobs_total`, `jobs_failing`, `jobs_oldest_success_age_seconds`, `job_<name>_success_age_seconds` |
| `forget(job)` | delete a retired job's record |

`JobStatus` fields: `name`, `lastSuccessAt`, `lastFailureAt`, `lastError`,
`consecutiveFailures`, `lastDurationMs`, `lastCount`, `successes`,
`failures`, `lastSuccessAgeSeconds` (-1 = never), `updatedAt`; methods
`ok()` and `healthy(threshold, maxAgeSeconds = 0)`.

### `InstanceHeartbeat(db, component, version = "", instance = "", prefix)`

| Member | Description |
|---|---|
| `beat() -> Result[bool]` | record one beat now |
| `start()`, `run()`, `stop() -> Result[bool]` | beat every 20 s in the background (`withInterval(seconds)`); blocking variant; stop and delete this instance's row |
| `alive(component, withinSeconds = 90) -> Result[int]` | instances of `component` seen within the window |
| `aliveByComponent(withinSeconds = 90) -> Result[dict[str, int]]` | the same for every component |
| `list(component = "", withinSeconds = 90) -> Result[list[InstanceInfo]]` | rows with `alive`, `ageSeconds`, `version`, `host`, `startedAt`, `seenAt` |
| `prune(olderThanSeconds)` | delete rows of crashed instances |

### `JobQueue(db, name = "default", owner = "", prefix)`

| Member | Description |
|---|---|
| `enqueue(payload, runAt = 0, priority = 0, uniqueKey = "", maxAttempts = 0) -> Result[int]` | add a job; returns its id, or 0 when `uniqueKey` matches a pending or running job |
| `enqueueIn(conn, payload, ...)` | the same through a `Db` or **your `Tx`**: the job exists only if your transaction commits |
| `claim(limit = 1, leaseSeconds = 60) -> Result[list[QueuedJob]]` | claim due jobs (priority, then `runAt`) with `FOR UPDATE SKIP LOCKED`; expired claims are recovered first |
| `complete(job)`, `completeIn(conn, job)` | mark done; `Ok(false)` when the claim was lost |
| `fail(job, error, retryAfterSeconds = -1) -> Result[str]` | retry after the backoff delay (or the given delay), `"dead"` after the last attempt; `""` when the claim was lost |
| `bury(job, error)` | dead letter now (permanent error) |
| `extend(job, leaseSeconds)` | renew a claim |
| `retry(id)`, `retryDead(limit = 0)`, `cancel(id)`, `cancelKey(uniqueKey)` | administration |
| `purge(olderThanSeconds, includeDead = true) -> Result[int]` | delete finished jobs |
| `get(id)`, `list(state = "", limit = 100)`, `dead(limit)` | inspection |
| `stats() -> Result[QueueStats]`, `JobQueue.statsAll(db, prefix)`, `gauges()` | counters: `pending`, `scheduled`, `running`, `failed` (waiting for a retry), `dead`, `done`, `cancelled`, `lagSeconds` |
| `withBackoff(JobBackoff)`, `withMaxAttempts(n)`, `withNotify(enabled)` | defaults: 30 s doubling to 6 h with 10 % jitter, 10 attempts, notify on |
| `JobQueue.permanent(message) -> Error` | an error that makes `JobWorkers` bury the job |

`QueuedJob`: `id`, `queue`, `state`, `payload` (`json`), `priority`,
`uniqueKey`, `attempt` (1 during the first run), `maxAttempts`, `runAt`,
`lockedBy`, `lockedUntil`, `lastError`, `createdAt`, `finishedAt`,
`lastAttempt()`.

`JobBackoff(baseSeconds = 30, maxSeconds = 21600, jitter = 0.1)`: delay
`base * 2^(attempt-1)` capped at `maxSeconds`, minus up to `jitter` of it at
random; `ceiling(attempt)`, `delay(attempt)`, `delayWith(attempt, unit)`,
`schedule(attempts)`.

### `JobWorkers(queue, handler, concurrency = 4)`

`handler: func(QueuedJob) -> Result[bool]` returns `Ok(_)` to complete the
job, an error to retry it, or `Result.Err(JobQueue.permanent("..."))` to bury
it; exceptions count as retryable failures.

| Member | Description |
|---|---|
| `start()`, `run()`, `stop() -> bool` | `concurrency` claim loops; blocking variant; stop claiming and wait for running jobs (true when drained) |
| `runOnce() -> Result[int]`, `drain(maxRounds = 0)` | synchronous processing (tests, CLIs) |
| `listen(databaseUrl = "") -> Result[bool]` | LISTEN/NOTIFY wake-ups (default URL: the queue's `Db`) |
| `withLease(seconds)` | claim lease, renewed every third while the handler runs (default 60) |
| `withTimeout(seconds)` | per-job timeout (default none): the job is failed and retried |
| `withPollInterval(ms)`, `withDrainTimeout(seconds)`, `quiet()` | idle polling (1000 ms), stop's drain limit (30 s), no failure logs |
| `completedCount()`, `failedCount()`, `activeCount()`, `notifications()` | local counters |

## Examples

### Periodic jobs across replicas

Every replica registers the same jobs; each due run happens on exactly one
of them, and outcomes land in `JobHealth`:

```lammergeier
from @lam/jobs import JobScheduler
from lamdb import Db
from lamerrors import Result

class Maintenance {
    func init(self, db: Db) {
        self.db: Db = db
    }

    func expireSessions(self) -> Result[int] {
        return self.db.exec("DELETE FROM sessions WHERE expires_at < now()", [])
    }

    func rollUsage(self) -> Result[int] {
        return self.db.exec("INSERT INTO usage_daily SELECT ... ON CONFLICT DO NOTHING", [])
    }
}

func startScheduler(db: Db) -> JobScheduler {
    work: Maintenance = Maintenance(db)
    scheduler: JobScheduler = JobScheduler(db)
    scheduler.every("expire-sessions", 60, work.expireSessions)
    # A long job: hold the running lease for 5 minutes, renewed while it runs.
    scheduler.every("roll-usage", 3600, work.rollUsage, 300)
    scheduler.start()
    return scheduler
}
```

Tests drive the same scheduler on a fake clock: `scheduler.tick(t0)` runs
what is due at `t0`, `scheduler.tick(t0 + 60)` the next round, and
`scheduler.tick(now, true)` forces every job (like MFT's `tick(now, force)`).

### Cron

```lammergeier
scheduler.cron("invoices", "0 6 1 * *", billing.issueInvoices)      # 06:00 UTC on the 1st
scheduler.cron("digest", "30 8 * * MON-FRI", mailer.sendDigest)      # weekdays 08:30 UTC
scheduler.cron("compact", "*/15 0-5 * * *", storage.compact)         # every 15 min at night

next: int = CronSchedule.parse("0 6 1 * *").unwrap().next(Time.nowUnix())
```

### Webhook delivery queue with retries and dead letters

MFT's 12 attempts with backoff doubling from 30 s to a 6 h cap:

```lammergeier
from @lam/jobs import JobQueue, JobWorkers, QueuedJob, JobBackoff
from lamdb import Db
from lamerrors import Result, Error

func deliver(job: QueuedJob) -> Result[bool] {
    url: str = job.payload.url
    if not url.startsWith("https://") {
        return Result.Err(JobQueue.permanent(f"refusing {url}"))   # straight to dead letters
    }
    body: str = job.payload.body
    status: int = postSigned(url, body, f"{job.id}")                 # your HTTP call
    if status < 200 or status >= 300 {
        return Result.Err(Error("WebhookHttp", f"HTTP {status}"))   # retried with backoff
    }
    return Result.Ok(true)
}

func startWebhooks(db: Db) -> JobWorkers {
    hooks: JobQueue = JobQueue(db, "webhooks").withMaxAttempts(12).withBackoff(JobBackoff(30, 21600, 0.1))
    workers: JobWorkers = JobWorkers(hooks, deliver, 8).withTimeout(30)
    _ = workers.listen()
    workers.start()
    return workers
}
```

Operators list and replay dead letters with `hooks.dead()`,
`hooks.retry(id)` or `hooks.retryDead()`, and keep the table small with a
scheduled `hooks.purge(30 * 86400)`.

### Enqueue in the same transaction as a business write

```lammergeier
from @lam/jobs import JobQueue, JobsRow
from lamdb import Db, Tx
from lamerrors import Result

func createOrder(tx: Tx, mail: JobQueue, email: str) -> Result[int] {
    rows: list[dict[str, any]] = tx.query("INSERT INTO orders (email) VALUES (?) RETURNING id", [email])?
    id: int = JobsRow.int(rows[0], "id")
    # Committed together: no order without its email, no email for a rolled-back order.
    _ = mail.enqueueIn(tx, {"template": "order-confirmation", "order": id, "to": email}, 0, 0, f"order-mail-{id}")?
    return Result.Ok(id)
}

func placeOrder(db: Db, mail: JobQueue, email: str) -> Result[int] {
    tx: Tx = db.begin()?
    created: Result[int] = createOrder(tx, mail, email)
    if not created.ok() {
        _ = tx.rollback()
        return created
    }
    _ = tx.commit()?
    return created
}
```

The `pg_notify` sent by `enqueueIn` is delivered only at commit, so
listening workers wake exactly when the job becomes visible. A handler can
likewise call `queue.completeIn(tx, job)` to commit its own writes and the
job's completion atomically.

### Health, status page and metrics

```lammergeier
from @lam/jobs import JobHealth, InstanceHeartbeat, JobQueue, QueueStats
from @lam/jobs/health import JobStatus
from lamdb import Db
from lamerrors import Result

func componentState(db: Db, beats: InstanceHeartbeat, component: str) -> Result[str] {
    if beats.alive(component, 90)? == 0 {
        return Result.Ok("outage")
    }
    failing: list[JobStatus] = JobHealth(db).failing(3)?
    if component == "worker" and len(failing) > 0 {
        return Result.Ok("degraded")
    }
    return Result.Ok("operational")
}

func metrics(db: Db) -> Result[dict[str, int]] {
    out: dict[str, int] = JobHealth(db).gauges(3)?        # jobs_failing, jobs_oldest_success_age_seconds, job_<name>_...
    for name, s in JobQueue.statsAll(db)? {
        out[f"queue_{name}_pending"] = s.pending
        out[f"queue_{name}_dead"] = s.dead
        out[f"queue_{name}_lag_seconds"] = s.lagSeconds
    }
    return Result.Ok(out)
}
```

`JobStatus`, `JobSchedule`, `InstanceInfo`, `QueuedJob`, `QueueStats` and
`JobLease` implement `__json__`, so they can be used directly in `json`
values.
typed locals (`to: str = job.payload.to`) before passing them on.

## Operations notes

- **Locking.** Schedules use a single conditional `UPDATE` per job (row lock,
  `next_run_at` check, running lease); leases use `INSERT ... ON CONFLICT DO
  UPDATE ... WHERE`; queue claims use
  `UPDATE ... WHERE id = ANY(ARRAY(SELECT ... FOR UPDATE SKIP LOCKED))`. The
  `ARRAY(...)` form matters: with `id IN (SELECT ... LIMIT n FOR UPDATE SKIP
  LOCKED)` PostgreSQL may re-run the locking sub-select for every outer row
  and claim far more than `n` rows. Data-modifying CTEs that also lock the
  rows they change are avoided (they can silently skip work). Migrations
  serialise on an advisory lock.
- **Clocks.** Lease and claim expiry use the database clock. Schedule due
  times compare against the caller's clock (`Time.nowUnix()` in `run()`), so
  keep instance clocks NTP-synchronised; skew only shifts when a job runs,
  never makes it run twice.
- **At-least-once.** A job whose worker crashes, hangs past its timeout, or
  loses its database connection before `complete` runs again after its
  lease expires. A timed-out handler cannot be killed (Go has no goroutine
  cancellation) and may still finish later. Scheduled runs are at-most-once
  per due time while the running lease is held, and a crashed run is not
  retried until the next due time.
- **Idempotency.** Make handlers idempotent: use `uniqueKey` to avoid
  duplicate enqueues, pass `job.id` as an idempotency key to external APIs
  (MFT sends `X-MFT-Delivery`), write results with `ON CONFLICT`, or commit
  the job's writes together with `completeIn(tx, job)`.
- **Leases and fencing.** Choose `lockSeconds`/`withLease` larger than a
  database hiccup (renewal happens every third of it). Use `JobLease.fence`
  when a stale holder could still write somewhere else.
- **Housekeeping.** Schedule `queue.purge(...)`, `beats.prune(...)` and, for
  dynamic lease names, `leases.prune(...)`. The partial indexes keep pending
  and running lookups fast regardless of finished rows.
- **LISTEN/NOTIFY.** `listen()` opens one extra connection per worker pool;
  notifications only shorten the wait, polling (`withPollInterval`) remains
  the safety net. Connection poolers in transaction mode (PgBouncer) do not
  support LISTEN: point `listen(url)` at a direct connection or rely on
  polling.

## Testing

```bash
sh test-services.sh --detach          # disposable postgres:16-alpine on a free port
eval "$(sh test-services.sh --url)"   # exports LAM_JOBS_TEST_DATABASE_URL
python3 tests/run_lamjobs_tests.py --verbose
sh test-services.sh --stop
```

The runner needs `lamc >= 3.0.0` on `PATH`, stages this checkout as
`extlibs/@lam/jobs` in a temporary tree (the installed-package boundary),
compiles every `tests/test_*.lam` without caches and compares its output
with the `# expect:` lines. Tests marked `# requires: database` are reported
as skipped when `LAM_JOBS_TEST_DATABASE_URL` is unset; the cron, backoff and
offline primitive tests always run. The PostgreSQL suites cover schema
migrations under concurrency, leases, health, heartbeats, the scheduler, the
queue, the worker pool and composed races (competing schedulers and worker
pools, lease contention, crash recovery, transactional enqueues).

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE). Release history is recorded in [CHANGELOG.md](CHANGELOG.md).
