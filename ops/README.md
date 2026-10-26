# AI-Filter host operations

This directory is the versioned source of truth for AI-Filter host scheduling,
backup, drift verification, and read-only operational alerts. Application image
rollback remains separate from SQLite data recovery.


## JD policy: NO_BACKUP (2026-09-10)

The operator has retired automatic backups and restore drills. The daily 03:00
Miniflux job, daily 03:30 system backup, and Sunday 04:30 restore drill are disabled
in root cron. The managed cron template retains its two former jobs as comments,
so installing the current source does not reinstate them. The Miniflux line is
outside the managed block and is retained as a disabled comment as well.

The ops checker explicitly reports backup, restore_drill, and offsite_upload as
SKIPPED_BY_POLICY, not as a successful backup or verified recovery. Other business,
market, enrichment and container checks are unchanged. The first natural checker
cycle stamps a policy epoch and suppresses a policy-only recovery notification;
current business failures and subsequent real recovery retain normal deduplication.
This migration does not run the checker or mutate its notification state. Historic
archives, logs, state attestations, failed units and the isolated 2.1 GiB rehearsal
are preserved. The three original executable scripts remain unchanged; their
continued existence is not permission to run them manually.

The historical backup/restore examples below are retained as reference, not
current instructions or authorization. Do not run them, create a new backup,
configure/upload offsite storage, or enable a backup schedule without separate
explicit authorization. The general installer is NOT the scoped policy migration:
it also enables market timers and is not to be run as part of this change.

## Dashboard host-check snapshot

The existing hourly checker also writes `data/ops/host_status.json` next to the
configured database. This small, atomic snapshot contains only fixed check names,
PASS/WARN/FAIL/SKIPPED_BY_POLICY statuses, start/end times and the release reference.
It does not export raw diagnostic details, secrets or host paths. `--dry-run`
does not write it; export failure leaves the previous file intact and does not
change the checker's existing alert or exit decisions.

The authenticated dashboard reads this file without running checks. Missing,
invalid, older-than-90-minute or different-release snapshots display as unknown.
The service headline reflects only `release_containers`; other checks are shown
separately. Policy skips are not successful backups. This is a last-check view,
not a live-health claim, and requires both the updated application image and
the updated host checker. No Docker socket or new schedule is needed.

## Assets

- `systemd/`: the verified weekday 15:10 market-close collector plus the 15:25
  exact-date daily-K repair one-shots and timers. The repair is a secondary
  completeness check; it does not replace or manually replay the primary job.
- `cron/ai-filter.crontab`: a marked block for system backup, conservative
  recovery health checks, read-only alerts, and refresh triggers.
- `backup/backup_system.sh`: atomic archive creation, SQLite online backup,
  `quick_check`, SHA256 verification, and optional encrypted off-host hook.
- `backup/restore_drill.py`: weekly SHA-verified extraction of `cache.db` into a
  private temporary directory, followed by SQLite `quick_check`, required-table
  reads, and an atomic status attestation. The installer publishes it together
  with the exact event-core schema verifier as an immutable, root-only libexec
  bundle, then atomically switches the selector consumed by a small launcher.
  It never writes the production tree.
- `ai_filter_ops_check.py`: read-only checks for authenticated `/metrics`,
  backup age/hash, off-host upload state, timer state, and the
  immutable release/container state.
- `install.sh` and `verify.sh`: explicit installation and hash-equivalent drift
  detection. Neither script deploys an application image.
- `logrotate/ai-filter`: bounded retention for health, refresh, backup, and
  operational-check logs.

## Install and verify

Review the diff first. Installation is intentionally disabled without the
explicit flag:

```bash
sudo ops/install.sh --apply
sudo ops/verify.sh
ops/verify.sh --source-only
```

`install.sh` preserves unrelated root cron entries and replaces only the block
between `BEGIN AI_FILTER_MANAGED` and `END AI_FILTER_MANAGED`. It also removes
only byte-for-byte legacy unmarked AI-Filter commands to avoid duplicate
schedules. An unbalanced marker or customized unmanaged AI-Filter command makes
installation fail before any host file is changed. The previous crontab is kept
root-only as `/etc/ai-filter/root.crontab.previous`. A failed crontab read or a
concurrent edit aborts installation rather than guessing that the spool is empty.
Running the installer repeatedly is idempotent.

The restore-drill launcher lives at
`/usr/local/sbin/ai-filter-restore-drill`. Its matching script and schema
verifier live under `/usr/local/libexec/ai-filter/restore-drill-<sha256>/`;
`restore-drill-current` selects a complete bundle. This prevents a partial host
asset update from pairing a new drill with an old verifier. Do not copy the
Python entry point into `/usr/local/sbin` by itself.

Each individual host file and the bundle selector are published atomically;
the full multi-asset installation is intentionally not a transaction. If the
installer returns non-zero after changing an asset (for example because the
final crontab compare-and-swap detects a concurrent edit), run `ops/verify.sh`,
resolve the reported drift, and retry. The installer does not guess a global
rollback across systemd, cron, configuration, and libexec assets.

The installer creates `/etc/ai-filter/ops-monitor.env` and
`/etc/ai-filter/backup.env` only when absent and never overwrites operator
configuration. Both are mode `0600`. Production drift is reported by asset
name; no file contents or secrets are printed.

## Weekly database maintenance results

The managed template retains Sunday 03:00 host time and writes to
`/var/log/ai-filter-db-maintenance.log`. The host runs
`scripts/run_db_maintenance.py --host` with Python 3 and streams that same source
to `docker exec -i ... python -u -`; the existing application image needs no new
file. GNU timeout retains `--signal=TERM --kill-after=60s 60m`. This change updates
the versioned template only; it does not install cron or run maintenance.

`--host` and an explicit `--db` are mutually exclusive: their combination exits
with argument-error status 2 before starting Docker or opening SQLite, even when
`--db` names the default path or is empty. Use `--host` for the managed container,
or `--db <path>` without `--host` for a directly selected database. With neither
option, the database path remains `/app/data/cache.db`.

Each JSON log line includes a UTC timestamp and a unique `run_id`. Host and
database start/finish records distinguish the Docker client's exit code from the
database result. Connection, `integrity_check`, `vacuum`, and close have separate
phase records; the final records summarize success, failure, or skipped phases.
The complete integrity result must be one `ok` row before VACUUM starts. A non-ok result
or check exception skips VACUUM and exits nonzero. A VACUUM or close exception
also exits nonzero, and close is attempted after a database exception.

The host records real transport failures, including timeout exit 124 and killed
exit 137. A timeout ends observation of the Docker client; it does **not** prove
the container's Python process stopped. Already observed phase results remain
visible, unfinished results and the container exit are `unknown`, and the run is
not a success. Missing finish records, including abrupt host termination, also
mean completion is unknown. Correlate UTC phase windows with lock errors before
inferring a cause. No retries, new locks, scheduling/concurrency changes,
`busy_timeout` changes, VACUUM tuning, or backup actions are introduced; the
existing SQLite connection defaults and NO_BACKUP policy remain in effect.

## Pending event-outcome prices

The existing 15:10 collector now gives frozen, selected V5 observations a
separate current-day budget of 500 missing symbols, in addition to the latest
500 current event mappings and the four required indices. It uses the frozen
mapping revision, so a mapping retiring or leaving the latest-event ranking
does not by itself end observation. Similarity copies, non-formal paths,
non-equity/concept mappings, invalid snapshots and withdrawn signals do not
gain eligibility. This does not change scores or make a historical mapping
semantically correct.

Needed prices are the exact baseline and T+1/T+3/T+5/T+20 closes, for both the
equity and `sh000300`. A missing date is never shifted to the next trading day.
The collector retries pending equities even when the four indices are complete;
one failed live chunk does not erase other chunks. Its exact-date daily-K fallback
allows up to 32 pending equities plus missing indices, with a 120-second budget
checked before each request. Missing required indices retain the existing
no-write failure behavior; with complete indices, valid partial prices are saved
before unresolved pending demand or truncation produces a nonzero exit.

The existing 15:25 repair also runs when today's indices are already complete.
It checks frozen signals within the preceding 120 calendar days and requests up
to 32 missing symbol/date pairs per run, plus missing current indices. Newest
dates are attempted first; successful pairs are saved individually. The shared
120-second request-start budget does not cancel an in-flight request, which
retains its existing 15-second transport timeout. This is bounded gap repair,
not unlimited historical backfill or a guarantee to clear a large backlog in
one run. Demand beyond the budget stays visible as incomplete.

Tencent responses must pass exact symbol/date, post-close timestamp and numeric
validation. Sina remains an index-only fallback. The existing daily-K provider
does not support Beijing symbols; these stay explicitly unresolved, not
relabelled as Shanghai/Shenzhen. New prices use the existing revision store and
outcome materializer; historical signal/mapping/outcome rows are not overwritten.
The repair refuses mismatched read/write database paths.

Inspect the existing job logs and exit status for pending counts, rejected
symbols/reasons, deferred work and final remaining demand. The ops monitor's
four-index database check is **not** an all-equity completeness assertion.
No new service, schedule, database table or acceptance framework is introduced.
Updating code alone does not deploy the collector image or execute a repair.

## Security-master one-shot

The A-share security master is an explicit maintenance task, not a timer. Its
Compose service is isolated behind the `security-master` profile, loads no
`.env` or Docker secrets, and defaults to a query-only dry run. The host runner
uses only the archived manifest and image digest recorded by the current
release; it never guesses `IMAGE_REF` or falls back to the working-tree
Compose file.

After deploying a release that contains the profile, create and verify a fresh
backup, inspect the dry-run JSON, and only then apply:

```bash
sudo /root/backup_system.sh
sudo scripts/run_security_master_sync.sh --dry-run
sudo scripts/run_security_master_sync.sh --apply
```

Review `candidate_count`, `confirmed_count`, `name_mismatch_count`,
`manual_conflicts`, and `legacy_conflicts` before applying. The importer is
insert-only: it never overwrites or reactivates an existing security and never
creates `event_securities`. Application-image rollback therefore does not undo
successfully inserted database rows; retain the pre-run backup. Do not schedule
this task until source-count drift and conflict output have been observed over
multiple manual runs.

## Host test environment boundary

Do not run pytest directly in the production checkout or inherit its runtime
environment. It contains real `secrets/`; release-test fixtures refuse to
overwrite them. The host's system Python packages are not the pinned test
environment either. A fixture refusal or dependency mismatch is not a passing
test, and must not be reported as full-suite success.

Use the existing isolated runner from a clean, committed checkout instead of
manually copying secrets or building another test wrapper:

```bash
AI_FILTER_TEST_PYTHON=/root/ai-rss-filter/.venv-release-gate/bin/python \
  ./scripts/run-release-tests.sh focused
AI_FILTER_TEST_PYTHON=/root/ai-rss-filter/.venv-release-gate/bin/python \
  ./scripts/run-release-tests.sh full
```

The interpreter must already have the repository's pinned test dependencies.
The runner freezes HEAD, makes a detached clean worktree, clears the inherited
environment, and supplies separate HOME and TMPDIR directories. `focused`
runs release-script tests; `full` runs the complete suite with the coverage
floor. Linux release tests require the Linux/GNU flock and Bash environment
used by CI; a developer-machine run is not evidence for unexecuted Linux paths.
Collection-only checks do not execute tests and are not a substitute.

A successful isolated test run or GitHub CI is test evidence, not a production
release authorization or a SHA-bound release receipt. Before build/deploy use
`scripts/run-release-gate.sh` and the existing release workflow. Do not install
or upgrade the production host's Python packages just to mask a test mismatch.

## Optional Prometheus template (inactive)

Production metrics inspection is performed by `ops/ai_filter_ops_check.py`:
it requests loopback `/metrics` with `X-API-Key` and validates required metric
values. Its schedule is the installed cron configuration; do not infer a
five-minute interval from an old report. This is not a Prometheus time-series
store. `core/metrics.py` also has live scoring/processing callers and is not
an unused class to remove wholesale.

`docker-compose.monitoring.yml` is a legacy optional stack, not part of the
normal application release. Its pinned [Prometheus 2.48 configuration](https://github.com/prometheus/prometheus/blob/v2.48.0/docs/configuration/configuration.md#scrape_config) cannot
send the required custom `X-API-Key` header. The endpoint does not accept a
Bearer token as a replacement; an anonymous request correctly returns 401.
Therefore `monitoring/prometheus.yml` has no active scrape jobs. Its commented
example records the correct service (`ai-filter-api:5000`) and path (`/metrics`)
without claiming an authenticated integration exists.

Do not start that stack as part of this documentation correction. Re-enabling
it requires a separate decision, secret-file/header wiring compatible with
the selected collector, and an actual authenticated scrape check. Do not put
API keys in query strings, checked-in YAML or process arguments, enable query
authentication, or remove endpoint protection to work around the mismatch.
Existing host checks and the NO_BACKUP policy are unchanged.

## Read-only alert check

Before enabling its cron line, run:

```bash
sudo /usr/local/sbin/ai-filter-ops-check --dry-run
sudo /usr/local/sbin/ai-filter-ops-check --dry-run --json
```

The normal mode writes only alert-deduplication state under
`/var/lib/ai-filter-ops-check`. Failures are notified on transition, when the
failure fingerprint changes, or after the configured cooldown. Telegram is
optional; set both `OPS_TELEGRAM_BOT_TOKEN` and `OPS_TELEGRAM_CHAT_ID` through
the root-only configuration. Tokens, API keys, request URLs containing tokens,
and remote response bodies are never logged.

The hourly check has a 55-second internal work budget, leaving time inside
the cron wrapper for notification and atomic state persistence. Critical metric
gauges (refresh freshness/degradation and durable queue alert state) fail closed.
Capacity saturation and cumulative rejection or
replay counters are reported as warnings. A new backup is fully hashed once;
the private verification attestation avoids re-reading the same large archive
on every run and expires after one day. `--dry-run` never writes it.

Safe-neutral scoring resolutions are exported independently for 2-hour and
24-hour windows as count, denominator, and ratio. Their denominator contains
direct model scores, local backend fallbacks, and safe-neutral resolutions; it
excludes similarity copies, fast passes, local rejects, and other non-model
paths. The first version is informational only: it does not change the existing
fallback definition, alert threshold, checker result, or model routing.

The event-enrichment check remains deliberately small: it verifies the host
cron state is recent, completed, and exited successfully. Restore drills still
validate the durable event replay tables needed to recover an existing database.

At 04:30 each Sunday, the restore drill validates the newest archive in an
isolated temporary directory. Its success must remain newer than eight days.
The drill replaces its prior success with a private `running` attestation before
reading the archive. A normal SIGTERM is persisted as failure; an uncatchable
termination remains warning-only for at most 50 minutes and then fails closed.
After first installation, run `/usr/local/sbin/ai-filter-restore-drill` once
before enabling alert delivery so the monitor has an initial attestation.

The drill is intentionally read-only and is not the production restore
orchestrator. After an actual `cache.db` restore, keep every application/cron
writer stopped and run `scripts/rotate_integration_outbox_epoch.py --db <path>
--confirm-writes-stopped` before reopening the API. Then create a fresh backup,
so future restore points contain the Outbox table and the post-restore epoch.

An alert transport is deliberately not guessed. Until Telegram (or an external
consumer of the exit status/log) is configured and test-delivered, this is a
local checker rather than a complete paging path.

The metrics URL must be loopback HTTP. The checker reads `AI_FILTER_API_KEY`
from its environment, a configured key file, or the project's root-only `.env`.

## Encrypted off-host hook

No remote destination is guessed. Without configuration each backup logs:

```text
WARN: offsite_upload=unconfigured local_only=true
```

To add a destination, implement a separate root-owned executable and configure
its absolute path in `/etc/ai-filter/backup.env`:

```bash
SYSTEM_BACKUP_UPLOAD_HOOK=/absolute/path/to/encrypted-upload-hook
SYSTEM_BACKUP_UPLOAD_REQUIRED=1
```

The hook receives the archive and SHA256 manifest paths. It must encrypt before
network transfer, verify the remote object, emit no credentials, and return
zero only after durable completion. The backup script discards hook output and
records a non-secret local success attestation. The monitor treats an unsafe,
stale, mismatched, or lagging upload attestation as a failure.

The weekly drill verifies archive integrity and SQLite readability, but it is not
a complete disaster-recovery rehearsal: it does not restore Redis, start the
read-only API, validate key row counts, or prove an RPO/RTO. Those remain explicit
follow-up work before any recovery-time claim is made.

## 日常查看、发布与恢复

- 一页操作入口：[DAILY-OPS.md](DAILY-OPS.md)（看状态、发布、恢复、失败后处理；含只读与操作命令区分）
