# Exact-original receiver drain

This capability applies only to an already instrumented receiver and sender.
It does not deploy, reopen admission, migrate a database, prove old `.125`
original coverage, or authorize a switch. Existing isolation controls retain
their original scope.

`services.receiver_drain_control` adds authenticated loopback transfer/query
operations to the existing receiver stop control. The host adapter is
`scripts.receiver_drain_driver.ReceiverDrainDriver`. Its parent `ReceiverDriver`
still binds actual container IDs, image, process start/restart identity,
web/worker roles, enforced fence environment, mounts and spool identities.
The original bundle must be within the inspected fence mount. Both POST and
exact-operation GET are followed by another actual identity check. This service
must run with the same reviewed runtime configuration, secret and source image
as the receiver; a synthetic inspect fixture alone is not production evidence.

Transfer is a controlled **business write**, not a read-only capacity reserve.
The sender driver must first actually stop new sender producers and seal its
PostgreSQL originals. The receiver must actually be in the exact draining
generation, with no active intake producer or uncertain/orphaned lease. Every
bundle row is validated before the first queue write: exact body/hash/length,
original signature under the unchanged secret, target endpoint, delivery
contract, user/feed/entry identity and the configured application body limit.
The limit is the smaller of the actual positive `MAX_CONTENT_LENGTH` and
1 MiB; missing configuration cannot relax it. No original is rewritten or
re-signed. The pure bundle parser is not proof of a live PostgreSQL observation;
the combined host driver must obtain the bundle through the real SenderDriver.

Normalization occurs once, before transfer, and is frozen with source,
configuration and strategy bindings. Transfer uses the normal spool lock,
ReceiptStore reservations, strict queue/receipt barriers, aliases and capacity
limits. The transfer plan caps normalized evidence at 32 MiB and 4096 originals,
each attempt at 16 originals, retained batches at 256, and repeated HTTP
delivery attempts at 4096 (a plan may lower these limits). Partial capacity or
I/O failure retains the original bundle and every queue, receipt and pending
artifact. Query resumes the same frozen task; it does not normalize again.

Only after every original is durably transferred may ordinary authenticated
`POST /webhook` repeat an exact listed body, endpoint and signature. This path
holds a real drain lease and rechecks its existing queue/receipt chain. New
correctly signed bodies, forged drain headers, source/configuration changes,
expired windows and exhausted replay budgets are refused. Existing receipt
queries and consumers retain their normal drain behavior. A repeated delivery
cannot create a missing accepted queue or trigger a second score.

A lost response permits only the same operation query. If the last batch's
per-original confirmations exist but its completion journal was not acknowledged,
query may recheck the actual receipt/queue chains and sync only completion
evidence after expiry or batch exhaustion. It cannot import another original.
A real process crash leaves an orphan lease and blocks automatic recovery;
the controller never deletes that evidence or guesses that the activity ended
successfully. Host queries have a separately retained 32-attempt budget.

`complete` proves the listed originals reached the real durable receiver path.
It does **not** prove queue drain: receipts may still be accepted and tasks may
still await consumers. Full sender pause follows independently verified sender
drain. Receiver freezing must follow an independent zero inventory while the
actual activity/spool/journal locks are held. Migration/startup must acquire
their full lock set and independently recheck again; an old sample, an HTTP
202, an idle lock, or a caller boolean cannot substitute. Historical originals
from the uninstrumented sender remain an explicit first-takeover blocker.

Synthetic regression entry points are `tests/test_receiver_drain_transfer.py`
and `tests/test_receiver_drain_driver.py`. They use actual Flask authentication,
durable spool/ReceiptStore files, loopback HTTP and a crashing child process,
with a fake scoring function. Docker inspect rows in these unit fixtures are
explicitly synthetic. Native container/PG and Go-to-Flask integration require
the separate combined fixture; these tests do not claim that integration.

The host-only `scripts/joint_drain_controller.py` composes actual SenderDriver
and ReceiverDriver instances. Its fixed stages are `stop_new`,
`transfer_originals`, `freeze_sender` and `freeze_receiver`; `query_transfer`
recovers only an existing transfer after expiry. A retained sender pause intent
goes directly to the same operation query even if the sender is already closed
or the write window has ended. Each controller retains at most 32 attempts.
For a host process that already completed the individual driver stages,
`confirm_existing_boundaries` queries their original stop/pause operations;
it takes no caller-provided receipts or empty booleans.

Final receiver freeze holds the specified existing release/refresh locks, then
the actual controller/activity locks, spool/journal and journal identity locks,
then both PostgreSQL advisory locks. It independently reads queue envelopes,
raw receipt records, continuation states, publication watermarks and both sender
tables. Nonzero/unknown storage leaves the receiver in draining with no new
freeze intent. A WAL-mode database missing either sidecar is refused before a
SQLite connection could create it. The final receipt records this held-lock
inventory but still reports switch, legacy-original and external-ingress
authorization as false. Actual cron/ingress binding is an additional site fact;
caller-selected release/refresh lock paths alone cannot prove its coverage.

`tests/test_joint_drain_controller.py` runs the real receiver HTTP, durable files
and SQLite boundaries with an explicitly synthetic sender PostgreSQL protocol.
`scripts/receiver_drain_fixture_runtime.py` is a separate, explicitly marked
synthetic entry point for the native Go/Flask/PG rehearsal. Its fixed authenticated
test consumer calls the real queue claim/execute/complete path with a fake
scorer, while its worker role holds only a bounded fixture identity. It is never
a production startup command or evidence of a real model/worker rollout.
