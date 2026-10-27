# Production migration and closed startup contract

This protocol is separate from `controlled_startup` and its temporary-directory,
pristine-fence isolation guard. It never removes that guard, reopens business,
copies a production DB, invokes models, or offers automatic rollback.

## Initialization audit

The ordinary `db.database.init_db()` remains compatible and is not invoked by
the production migration runner. Its actual behavior is broader than DDL:

- It first checks event-core/backfill state, then selects WAL and synchronous
  NORMAL. Base app-meta/news/article-source DDL precedes news schema migration.
- News schema versions 1 through 7 each have a commit; legacy version
  normalization has another commit path. The first such commit also commits
  preceding base-table changes.
- Event-core helpers use savepoints inside a transaction, but their `_atomic`
  helper commits when no outer transaction exists. Schema repair can queue
  deterministic replay roots; priority reconciliation changes canonical
  priority, version and update time.
- Market/ledger initialization freezes legacy future-price revisions, installs
  pointer guards, and performs existing backtest/security/score-bridge repairs.
  Outbox initialization normally imports historical signals and trims retained
  history. Feed synchronization inserts missing enabled-feed health rows.
- Operational/index/feedback/explanation tables and event-core completion
  markers share the initializer's final commit. An external intent without the
  final marker therefore cannot establish which business writes committed.

The new fixed plan is `base-tables`, `news-schema-1` through `news-schema-7`,
`event-core-backfill`, `event-runtime`, `market-ledgers`, `operational-tables`,
`feed-health`, and `final-verification`. Each step uses BEGIN IMMEDIATE with
synchronous FULL; its before/postcondition and completion journal row commit in
the same transaction. Nested helpers may use savepoints. Independent commits,
transaction escape, ATTACH/DETACH and durability changes are refused. The
existing maintenance backfill runs through its in-transaction entry point;
the normal initializer is never replayed.

The first supported production starts have a valid existing event-core/backfill
boundary, or are empty pristine schemas accepted by the existing read-only
preflight. A populated compatibility/event database with missing or incompatible
markers is refused before authorization. Historical outbox import must already
be recorded when any ledger row exists. The new plan explicitly uses
`backfill=False, trim=False`; this disables historical import and retention,
but the existing helper can still fill mapping dimensions and event references.
Nonempty rows requiring either repair are therefore refused before any step.
Every retained column/value is streamed into a bounded full-row fingerprint;
the same IDs with changed scores, mappings or references cannot pass. The plan
does not manufacture missing historical exports. Missing historical migration
proof needs a separately reviewed migration; it is not treated as compatibility.
The target schema fingerprint is a reviewed input and is checked after all
fixed steps, together with event-core/backfill/outbox and foreign-key validators.

## Bound maintenance session

`services.production_migration.locked_session(root, permit)` yields a live
`LockedMigration` capability. Its methods are `authorize()`, `migrate()` and
`query_migration(operation_id)`. The root must be new for authorization. The
permit binds source/image/tree and the actual sealed private release contract,
runtime bytes, config/effective config, secret/input reference metadata, exact
DB directory/file/WAL/SHM identities, source/target schema, critical app-meta,
retained outbox, a frozen admission operation, finite operation/window, fixed
steps, the sender contract and an independently read inventory fingerprint.
Full source hashes and clean-source private seal validation remain mandatory.
The old frozen binding and candidate source/image are distinct endpoints in a
complete reviewed compatibility structure whose hash is checked. The SQLite
policy proves only the supported forward transition. Old-image execution and
backward compatibility remain explicitly unproven, with no fallback branch;
a random compatibility hash is not accepted as evidence.

The permit sets an explicit bound up to a 16 GiB ceiling. `max_page_count`
limits logical SQLite pages, while progress and precommit checks bound the
observed DB+WAL+SHM aggregate and enforce the current free-space floor. This is
a conservative application check, not an OS disk quota or permission to copy
or load the whole database. SQLite's
cache is 16 MiB and maximum row/scalar length is 8 MiB. Schema/meta inventories,
outbox rows/bytes and SQL/step durations are separately bounded. A synthetic
sparse-file test covers a 2 GiB identity, not production-size SQL performance.

Lock order is release, refresh, receiver controller, receiver activity, actual
spool/journal and journal identity locks, then the sender's actual PostgreSQL
activity/metadata locks. The fixed `PostgresReader.locked()` adapter verifies
the cluster/schema/OIDs/user and schema-134 closed operation, leases and both
outbox/legacy inventories on the same held PG lock session. It does not accept
a caller's empty sample or `locks_held` boolean. The enclosing controller must
retain this context through migration and closed application initialization;
it must not nest a second owner for those same locks.

The initial production authorization currently requires an existing WAL/SHM
pair whose identities are retained by the maintenance read handle. Missing
or half-present sidecars block authorization; the runner does not create them
to satisfy the binding. Free disk is freshly checked against a fixed floor for
new actions, not included as an immutable byte count in the inventory hash.
Changes to unrelated disk usage cannot invalidate an exact outcome query.

An attempt writes its host intent before changing SQLite. Any exception or
lost ACK ends execution. Subsequent calls can only query the same operation:
they read the committed ordered journal and verify current postconditions,
never execute missing steps, reset the window, clear evidence or rerun init_db.
A partial journal reports `migration_uncertain` and the known committed steps.
Final DB markers, postconditions, fsync confirmation and the host receipt must
agree before completion is reported. A separate reviewed forward repair would
be required for a partial migration. Power-loss durability is not claimed from
process-fault tests alone.

The local migration tests use real synthetic SQLite and file locks, with test
doubles for the already separately tested private release loader and PG adapter.
They do not constitute native PostgreSQL or real application startup proof.
The actual PG session and local lock identities are rechecked immediately
before and after each SQLite COMMIT. PostgreSQL and SQLite are not one atomic
system: a lock loss detected after commit leaves the committed journal and an
unknown result, never a claim that the transaction rolled back. Future or
noncanonical news schema versions are refused before authorization.

## Closed process startup and replacement

`production_startup_control` is host-only. Its `authorize`, `grant`, `query`
and `replace` operations require the live migration lock capability. Every
new action rechecks the independent raw inventory without reacquiring its
already held locks. It accepts only a completed exact migration operation.
The descriptor contains narrow runtime paths, source/config hashes, the full
completed DB-journal hash and frozen binding; no Git checkout or PG DSN is
mounted into the application. Only actual created, never-started containers
with zero restart count may receive initial grants. Host inspection checks
image, command, working directory, healthcheck configuration, user/groups,
privileges/resources, exact environment, project, mounts and network/ports.

The runtime independently binds the actual admission environment to that same
frozen root and binding and checks actual DB inode, schema, full journal and
postconditions. The configured private runtime file and runtime source bytes
must match. It never calls `init_db` or the host private release/Git/PG loader.
The descriptor/grants directory is mounted read-only and must be disjoint from
writable state; writable bind aliases or ancestors exposing it or the runtime
config are rejected. The ordinary isolation entry and its guards are retained.

Gunicorn N=1..4 has independent `web-1`..`web-N` slots, each with a distinct
operation/process nonce, dispatch intent, PID/start ticks/namespace/boot,
parent/master identity and held owner flock. Dispatch must come from the
actual container PID 1 Gunicorn master; claim must come from that master's
live direct child. One actual process cannot fill multiple slots. The separate
worker must itself be container PID 1; init wrappers and other process trees
are unsupported. Its closed branch starts no Redis probe, bootstrap,
dashboard recovery, quotes, quant or webhook dispatcher. This protocol does
not subsequently activate business subsystems or open admission.

A child exit leaves its slot vacant. The master does not silently grant a
replacement or kill healthy siblings. At most one replacement per slot needs
a new finite operation and the exact retained original receipt, independent
proof that its old process/lease is dead, a fresh inventory check and the same
frozen migration/config/source contract. A worker replacement requires a new
never-started container. All original intents/receipts remain. Unknown or
missing receipts cannot be treated as dead or cleared. Same-operation queries
remain available after expiry; expiry never creates a new grant or window.
An initialized slot is not a health, business-opening or full cutover proof.

The native CLI is `scripts/verify_production_startup_native.py --repo ...
--output ... --postgres-dsn-file ... --miniflux-source ...`. It builds the real
application Dockerfile from the clean archive, exercises N=1..4 Gunicorn and
closed worker containers with 768 MiB/256 PID bounds and no network, and retains
actual host PostgreSQL activity/metadata locks throughout migration/startup.
The PostgreSQL fixture is a new schema-134 seed from hash-locked source DDL;
it does not prove a legacy PG upgrade or execution of the previous production
binary. The native cases include closed HTTP, distinct real processes, one
web-child crash preserving siblings, and one bounded replacement of web and
worker. Synthetic source/build/pull, DB/WAL/SHM, intents/slots and actual
container inspect/log/stats/cgroup evidence are retained under the private
output tree. Evidence collection failure retains containers and does not
replace the original failure. No power-loss or production cutover is claimed.
