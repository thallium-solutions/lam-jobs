# Changelog

All notable changes to `@lam/jobs` are documented in this file.

## [1.0.1] - 2026-10-21

### Changed

- Requires `lamc` 3.0 (`>=3.0, <4`). Classes re-exported by `@lam/jobs` keep their member types (no module imports needed for `json` values); workarounds for lamc 2.1 removed from `cron.lam` and the tests.

## [1.0.0] - 2026-10-19

First release, extracted and generalised from the managed-file-transfer
service (worker leases and job heartbeats, status heartbeats, replication,
webhook and mail retry queues).

### Added

- Library-owned, versioned schema: `Jobs.migrate` / `ensureSchema` /
  `schemaVersion` / `dropSchema` with a configurable table prefix (default
  `lam_jobs_`), a `<prefix>schema_version` table and an advisory lock that
  serialises concurrent migrators.
- `JobLeases`: `acquire`, `renew`, `release`, `holder`, `holds`, `info`,
  `list`, `prune`, fencing tokens and `runExclusive` (renewal while the
  callback runs).
- `JobScheduler`: periodic (`every`) and cron jobs that run once per due
  time across all instances, with a running lease that prevents overlapping
  runs, `tick(nowUnix, force)` for tests, a background loop (`start`, `run`,
  graceful `stop`), `pause`/`resume`/`trigger`, `get`/`list`.
- `CronSchedule`: 5-field expressions with ranges, steps, lists, month and
  weekday names, macros and the Vixie day-of-month/day-of-week rule; next
  run times in UTC.
- `JobHealth`: per-job success/failure, consecutive failures, last error,
  duration and counts; `measure`, `list`, `failing`, `stale`,
  `oldestSuccessAge`, `gauges`.
- `InstanceHeartbeat`: `beat`, background `start`/`run`/`stop` (removes the
  row), `alive`, `aliveByComponent`, `list`, `prune`.
- `JobQueue`: durable JSON jobs with priorities, delays, unique keys and
  per-job attempt limits; `enqueueIn` / `completeIn` inside the caller's
  transaction; claims with `FOR UPDATE SKIP LOCKED` and expiring leases;
  `fail` with capped exponential backoff and jitter (`JobBackoff`), dead
  letters, `bury`, `retry`, `retryDead`, `cancel`, `cancelKey`, `purge`,
  `stats`, `statsAll`, `gauges`.
- `JobWorkers`: a worker pool with lease renewal, per-job timeouts,
  permanent errors, graceful drain, synchronous `runOnce`/`drain`, and
  LISTEN/NOTIFY wake-ups (`listen`) with a polling fallback.

### Testing

- Tests run through the installed-package boundary
  (`tests/run_lamjobs_tests.py`, `lamc >= 2.1.0`). Cron, backoff and offline
  primitive tests always run; PostgreSQL suites (schema, leases, health,
  heartbeats, scheduler, queue, workers and composed races between
  schedulers, worker pools and lease holders) run when
  `LAM_JOBS_TEST_DATABASE_URL` is set (`sh test-services.sh`).
