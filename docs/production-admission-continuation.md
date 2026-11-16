# Bounded production admission continuation

This protocol extends time for the **same already activated receiver and sender**.
It does not reopen a stopped fence, reset its generation, remove evidence, increase
its original 4,096 admission / 256 MiB limits, or recover unknown pre-T0 originals.
Go OPEN does not automatically expire. The receiver rejects new work when its
host guardian heartbeat or current continuation lease expires. Existing durable
consumers may continue; sender originals remain subject to the existing hard cap.

The host must retain the original source/config/image/container/PG identities.
The runtime grant directory must remain read-only inside both application roles.
A changed owner, failed pulse, unknown lease, held/legacy original, original cap,
retained-operation limit, disk floor, clock regression or boot change cannot be
made healthy by granting another window.

## Required execution order

1. Under the real `ReceiverActivationSession`, complete closed consumer prepare
   and confirm. Keep the actual sender initialize-closed receipt.
2. Call `production_admission_watchdog.prepare(session, sender)`, then `start()`
   and `query_ready(plan_path, child.pid)`. The fixed isolated Python child binds
   its actual PID/start/boot and the exact parent. A caller boolean is insufficient.
   **Do not execute either first OPEN before this succeeds.**
3. Execute the original receiver and sender resume operations. Unknown sender
   results permit only the original GET. `abort_activation` and the watchdog can
   query the original intent without inventing a cross-service confirmed receipt,
   then perform restrictive sender stop-new and receiver stop-new independently.
4. `production_admission_lifecycle.initialize(session, sender, policy)` seals the
   reviewed term and its first lease. `production_admission_supervisor.prepare`
   seals the fixed host plan; complete formal release registration separately.
5. Release the original host locks, then call `watchdog.handoff(watchdog_plan,
   supervisor_plan)`. The **same** child becomes the supervisor. Require a fresh
   `supervisor.query_started(supervisor_plan, watchdog_pid)` before reporting a
   supervised deployment. Calling this while holding the release/controller lock
   causes contention and must not be treated as startup success.

The supervisor accepts a fixed private plan, runs no shell, uses `-I -B` and a
minimal environment, and reconnects only through the exact retained sender
container network namespace when configured. It does not inherit PYTHONPATH,
Python startup hooks, LD_PRELOAD or arbitrary host configuration overrides. If
sealed runtime configuration still requires an unprovided environment value,
the effective-config check fails and the restrictive stop path runs.

Before handoff the guardian checks its original parent every two seconds and
receiver admission accepts a heartbeat for at most eight seconds. After handoff,
a real identity/capacity check runs every fifteen seconds; the heartbeat is valid
for at most forty-five seconds. The current immutable lease is independently
finite. If the guardian itself is killed, **sender closure is not proven**:
receiver new admission fails closed, originals remain retained, and an operator
must recover the same private plan or complete a reviewed stopped drain. Neither
host process death nor a lease file can revoke Go OPEN remotely.

## Policy and retained bounds

The exact policy keys are `schema`, `operation_id`, `approved_by`, `not_before`,
`expires_at`, `lease_seconds`, `renew_before_seconds`, `max_renewals`,
`receiver_stop_records`, `receiver_stop_bytes`, `sender_stop_records`,
`sender_stop_bytes`, and `minimum_free_bytes`. Schema is
`production-admission-continuation-v1`. Terms are at most thirty days; each lease
is 300–3,600 seconds, renewed only within its reviewed 60–1,800 second lead time,
with at most 4,096 retained renewals. Stop thresholds are strictly below the
existing receiver/sender limits and retain control-operation headroom. These
observations are not atomic reservations across two services; original storage
and admission caps remain the final enforcement boundary.

Authority, lease intent/result, predecessor hash, wall/elapsed/boot clock and exact
CAS sequence are retained with strict file/rename/directory synchronization. A
query can confirm an already published original after expiry but cannot publish
a missing lease after the preceding window expired. Concurrent renewals share
actual fence metadata locking; only one expected sequence can advance. No
expired term, completed shutdown, source change or different generation can
restart automatically.

A confirmed dual stop is reported as `producers_stopped_drain_pending`, never as
queue-empty or switch-ready. The existing exact-original transfer and independent
inventory/freeze protocol is still required to drain; held/unknown originals
remain blockers. Finite authority, retained evidence and original capacity must
be reviewed before exhaustion. This is a bounded operation policy, **not a claim
of unlimited unattended retention or an automatic evidence-rotation protocol**.
