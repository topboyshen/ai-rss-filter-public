# Controlled cutover readiness and remaining field evidence

This implementation combines the staged sender, exact-original receiver drain,
transaction-step migration, closed application startup and bounded identity
preflight. It is an implementation and isolation-test proposal, not a release
permit. It does not change an existing `.126` image or tag, contact production,
reopen business admission, or offer automatic rollback.

## What each result can establish

| Component | Evidence it establishes | Evidence it cannot replace |
| --- | --- | --- |
| `takeover_readonly_preflight.py` | Explicit container/image/file identity observations, bounded subprocesses and preserved partial results | Queue drain, historical raw originals, live database compatibility or future unchanged state |
| `production_sender_driver.py` | Actual stop-new operation, producer completion, exact persisted originals, independently queried sender drain and full pause | Raw messages from before the instrumented sender existed |
| `receiver_drain_driver.py` | Exact listed bodies enter the ordinary strict durable queue and receipt path; repeated delivery is constrained to that list | Consumer completion or permission to freeze a nonempty queue |
| `joint_drain_controller.py` | Bound stage outcomes and an independent empty inventory under the actual receiver and sender locks before receiver freeze | A reusable authorization to migrate after the locks have been released |
| `services.production_migration` | Ordered real SQLite steps and completion markers committed together, then strict host confirmation | Automatic recovery by rerunning initialization, downgrade or old-image compatibility |
| Closed startup protocol | Exact application/slot identities bound to completed migration while business remains closed | Permission to resume producers or proof of normal live traffic |

The migration session reacquires its complete lock set and resamples independently.
It never accepts an earlier empty-inventory JSON as proof that the live system is
still empty. Missing or uncertain activity, held messages, pending archives,
unconfirmed receipts and unsupported migration markers remain blockers.

## Capacity and retention

Existing queue, receipt, alias, sender, held and continuation limits remain in
force. Exact-original transfer has additional finite batch, normalized-byte,
replay and deadline limits; those are lower-level bounds, not extra capacity or
a daily model-call allowance. An application body limit is read from the actual
configuration and constrained by the sender limit, never inferred from a sample.

Capacity errors retain originals and partial durable evidence. No stage deletes
held or terminal records, resets attempts, invents an original body, resends with
a changed identity or increases limits to make a drain pass. Outcome queries
can perform the exact synchronization required to confirm a prior operation;
they must not be described as ordinary read-only monitoring.

Native application scenarios retain 768 MiB / 256 PID. PostgreSQL retains
384 MiB / 64 PID. Historical PG pressure and failed artifacts are separate
evidence and are not invalidated by a later successful run. Process-crash tests
do not establish physical power-loss durability.

## Before any production execution

1. Lock the reviewed full source SHA, immutable candidate images, private
   configuration contract and actual old-image compatibility matrix. Public
   default configuration is not a substitute for the private runtime contract.
2. Obtain a separately authorized bounded field observation of actual host,
   containers, all producer entrances and cron, storage/lock identities,
   PostgreSQL identity/permissions, exact retained originals and capacity.
   A timed-out Docker observation remains unknown; it is not retried silently.
3. Resolve the first-takeover historical-original boundary. If exact old
   messages or complete in-flight coverage never existed, neither current feed
   entries nor new outbox records can establish them retrospectively. Preserve
   that gap and obtain a specific decision before proposing a different risk
   boundary; never silently label it lossless.
4. Bind one finite operation, window, owner and exact old-to-new transition to
   those facts. Stop new producers, transfer only retained originals and drain
   consumers before the independently verified freeze. Every uncertain outcome
   permits only the original bounded outcome query, not a new action or retry.
5. Reacquire all required locks for migration and closed startup. Partial
   migration or uncertain process ownership keeps business closed. Confirm each
   real web worker and worker-main slot; a healthy container alone is inadequate.
6. Review any subsequent producer-resume and observation plan separately against
   the actual retained state. The current test harness must never serve as a
   production command or substitute synthetic identities for field evidence.

## Failure and rollback boundary

Before a state-changing action, a failed identity or capacity check stops that
attempt with all evidence intact. After a possibly committed action, preserve
the exact operation and query its outcome within its separate query budget.
Partially committed migration requires a narrowly reviewed forward resolution;
it is not retried as a whole. Missing process ownership or a dead startup slot
does not establish that its work finished.

Retaining an old image and Compose is necessary but not sufficient for rollback.
An old image may not understand the new admission, receipt, journal, archive,
sender schema or held state. No direct old-image restart is authorized by these
tools. A rollback needs independently stopped producers and consumers, retained
originals and terminal evidence, and an exact compatibility decision for the
actual state. Existing no-backup policy is not changed by a hidden database copy.

## Validation reporting

Report local regression, actual Go/PostgreSQL integration, real Flask durable
handoff, actual application-image closed startup and full hosted CI separately,
with each exact SHA and original failure retained. A fake scorer in the handoff
fixture is intentional and must be disclosed; it proves no model quality.
Likewise a synthetic PG initializer is not the actual Go migration test.

Human quality labels remain a separate deliverable. Two independent human
reviews and adjudication are required for the agreed Gold evaluation; neither
CI success nor deployment readiness fills those labels or measures accuracy.
