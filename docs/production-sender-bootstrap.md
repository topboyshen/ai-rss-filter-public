# Retained sender bootstrap before receiver activation

`production_sender_bootstrap.py` is a separate production preparation adapter.
It consumes a reviewed sealed Compose file, environment, offline Go build
attestation and original build evidence. It does not clone or compile Go and does
not treat the source lock as a build receipt. The attestation binds public source
revision, fixed upstream/patch lock, Dockerfile, Go 1.26.4, binary hash and image
ID. A newly **created, unstarted** container supplies `/usr/bin/miniflux` through
a bounded tar stream; only one regular member is hashed, without extraction or
following links. A binary mismatch leaves that container unstarted and retained.
The formal release gate remains responsible for producing/reviewing the actual
offline build evidence and old-image/schema compatibility experiment.

Every API requires the real live `LegacyTakeover` capability and fresh
`check_stopped()` evidence under its registered root-cron writer lock. This
adapter does not stop any previous container, change crontab, remove/recreate
anything, or assert historical raw coverage. All registered old IDs stay stopped
and retained. Business binds, their device/inode/owner/mode, network and host
ports must match the stopped sender. The new one-service Compose project has no
dependencies, hooks, command override, scaling, privileged mode or resource
increase. It is read-only, UID:GID 65534:65534, cap-drop ALL, no-new-privileges,
restart=no, at most 768 MiB including swap / 256 PIDs / one CPU.

There is no `FIRST_CLOSED` environment flag. The fixed patched binary must run
with HANDOFF_ENABLED=1 and WEBHOOK_OUTBOX=1, plus the exact original contract and
manifest. Its absent handoff gate refuses business until the explicit
`/initialize-closed` transaction creates generation zero. Startup admin creation
and DATABASE_URL_FILE overrides are forbidden. The application DATABASE_URL and
independent host metadata reader must use the exact same sealed DSN.

The four explicit phases are:

1. `create(legacy, plan, evidence_root)`: verify clean source, sealed offline build,
   old stopped runtime, existing network and read-only PG predecessor metadata;
   require no prior control rows; issue one Compose create; verify actual image,
   Env, permissions, resources, mounts, ports and the binary bytes. Lost create
   acknowledgment never authorizes another create or automatic start.
2. `start(...)`: recheck exact created ID and original prerequisites, retain the
   start intent, then issue one `docker start` for that ID. No Compose up,
   force-recreate or remove-orphans operation exists.
3. `initialize(...)`: independently read actual migration135 OIDs and PG identity,
   preserve the resulting exact sender plan, then use the real
   `SenderActivationDriver.initialize_closed` once. A nonempty/previous handoff
   history cannot be reset. Existing held originals are retained; this receipt
   does not declare their queue empty or authorize activation.
4. `query(...)`: query only the original retained candidate/control operation.
   An unknown start can be inspected, but query does not start it or invent a
   first initialization. A lost initialize ACK is recovered by GET of the same
   operation, including after the write window expires; never repeat POST.

Accepted source schemas are deliberately restricted to canonical
131/132/133/134/135. The separate native host-loopback131 CI job must prove
the fixed upstream image's actual 131 migration directly to 135, CLOSED
initialization, retained-original restart and the old CLI's actual migration
behavior before release. A successful old `-migrate` no-op is not schema refusal
or evidence that the old sender can safely resume.
The fixture never manufactures a 131 marker. The original 132/bridge and full
activation proofs remain required. Earlier, future, malformed or unknown schema
markers stop before create. PG metadata uses a read-only transaction, bounded LIMIT queries,
3-second statement timeout and a 10-second observation deadline; no body,
credential or whole-table inventory is returned. A failed or not-yet-complete
migration leaves initialization blocked.

For a Miniflux using external PostgreSQL, the component records
`external_postgres={dsn_sha256,host,read_only_rootfs:true}` from actual Docker Env
and HostConfig. Missing Mounts, extra mounts, changed DSN, libpq host overrides,
loopback DB, writable rootfs, application roles and old isolation scope still
fail. No dummy mount is added to satisfy an old fixture assumption.

A separate `host_loopback_postgres` capability supports the exact existing
host-network topology: read-only sender, zero mounts, zero published ports,
`network_mode: host`, and literal `127.0.0.1` with port 5432. Omitting the default
port preserves the original DSN bytes/hash; an empty port or any PG-prefixed
environment override is rejected. This does not permit loopback under the
external-host policy or aliases on Docker's host network.

The sealed transport binds host boot/net namespace plus the exact healthy PG
container image, start/restart/PID, config, mounts, independent bridge and sole
`127.0.0.1:5432` publication. The collector must run in the real root daemon-host
namespace. Every PG operation rechecks this binding and verifies server address,
port and instance/session identity on its actual connection. It cannot replace
this check with a second connection. Migration permits and all reader factories
retain the same transport capability.

Control HTTP also requires the unique IPv4 loopback LISTEN socket to belong to
the exact sender PID's FD. `pidfd_getfd` holds that listening FD through the
request and cleanup, with process/start/netns/FD checks before and after. This
requires Linux support and existing ptrace/LSM/seccomp permission. Unsupported
or denied duplication is UNKNOWN and blocks; no capability, sysctl, listener
fallback or timeout extension is authorized. A shared host namespace alone is
never listener ownership proof.

These checks do not make a schema downgrade safe. The fixed upstream 131
implementation only migrates when the database version is lower than its own;
its CLI can return success without changing a newer schema. The native check
must observe the exact image, normal exit, the actual 135/131 migration log and
unchanged retained original/CLOSED state. This proves only that CLI no-op,
not old-service compatibility, startup refusal or safe rollback. Rollback cannot
consist of merely switching a sender image. Preserve all original PG/control/outbox/held records,
keep producers and consumers closed, and separately review recovery before any
rollback. Historical messages whose old sender never saved raw bytes remain an
explicit gap and must not be replayed or invented.

The resulting `driver-plan.json` contains real table OIDs, container ID and start
identity. Receiver migration/activation permits must be sealed using those actual
facts **after** closed bootstrap, not fabricated beforehand. The root controller
must also complete the gateway, pre-open watchdog and formal release registry
protocols before reporting a deployed service.
