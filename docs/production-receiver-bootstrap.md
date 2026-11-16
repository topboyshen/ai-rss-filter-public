# Retained receiver receipt metadata before M1

`scripts/production_receiver_bootstrap.py` prepares only the new empty
`receipts/` and `receipts/tasks/` directories required by independent M1
inventory. It does not drain tasks, create delivery receipts, change the
database schema, establish protocol T0, or open business admission. Missing
historical raw messages remain **unknown and not replayed**; empty new metadata
is never evidence that old messages were durably accepted.

Run this separate host adapter only after the original legacy stop and actual
sender CLOSED bootstrap have been independently confirmed. It requires the
real retained release lock, registered legacy cron-writer capability, exact
stopped container IDs, and the canonical sender driver's original CLOSED
operation. It retains the real PostgreSQL gate/table locks together with the
existing spool, journal and per-article locks throughout preparation and final
rechecks. Test-shaped capabilities cannot authorize the production CLI.

The sealed private plan binds a clean full source revision and helper hashes,
host boot ID, original stop operation, current release-lock inode, exact sender
namespace/PG/DSN/token references, and actual old data mount paths. Its execute
window is at most ten minutes. The API and worker must expose the same single
read-write `/app/data` bind. The continuation journal must be the existing
`natural_score_continuations` directory next to the actual database, not a newly
invented empty directory. Evidence must be outside the retained business tree.

All known queue/inflight/archive-pending/terminal-pending work and active
continuations must be zero. Retained dead letters, exhausted continuation
records and legacy `.completed-digests` are permitted only as original retained
evidence. Unknown, partial, malformed or oversized artifacts block preparation.
No missing spool/journal lock is created and no original evidence is removed.

Under the retained locks, the adapter binds SQLite main/WAL/SHM identities and
reads bounded schema, outbox and publication metadata in one read-only
transaction. It requires published input to be caught up. A closed, fully
checkpointed WAL database may have neither sidecar: that case uses immutable
read-only access so observation creates no sidecars. A one-sided pair is
rejected. If both sidecars exist, normal read-only WAL access is required so
committed WAL data is not ignored. The main file is not copied or fully hashed.
SQL observation is bounded by the five-second inventory budget and a maximum
three-second progress deadline for each metadata phase.

Preparation retains original file hashes and inode/permission identities,
durably writes the intent, creates only the two private 0700 directories, then
syncs each new directory and its parents. Before/after inventories must match
and all retained capabilities are checked again before the final record.
Successful `fsync` is not a power-loss experiment.

The CLI exposes `execute` and `query`, with exact `--plan`, `--legacy-plan`,
`--legacy-evidence`, and `--evidence` inputs. Execute is single-attempt. Partial
creation, lost acknowledgments, sync failures and uncertain final records keep
their original evidence and allow only query of the original operation. Query
does not recreate metadata, rerun execute, manufacture missing records, or
authorize migration. Even a confirmed final result is only
`new_metadata_prepared`, with T0, migration and business-open flags false.
M1 must subsequently reacquire its own locks and resample all inventories.

Local tests use real SQLite, files and flocks with explicit Docker/PG doubles.
The native sender bootstrap invokes the same production execute/query methods
under actual Docker, legacy/release capabilities and PostgreSQL locks using a
tiny synthetic old database and retained queue/journal evidence. It keeps the
existing 768 MiB / 256 PID application limit, 384 MiB / 64 PID PG limit and
180-second operation / 240-second outer bounds. That synthetic metadata schema
does not prove compatibility with an actual `.125` image, replace formal clean
release gates, or justify switching a production installation.
