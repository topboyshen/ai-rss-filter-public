# Active release runtime overrides

A forced deployment may have an explicitly recorded Compose override in
addition to its immutable image and archived base manifest. The override is
runtime configuration; it does not change the image's source identity or prove
that a mapping migration was accepted.

`deploy/health_check.sh` and `scripts/cron_event_enrichment.sh` consume the
existing `data/releases/current.override` and `current.override-sha256`
sidecars. The override must be a regular file in the same registry directory.
Its sibling `.receipt` must bind its hash and path to the current release ref,
image digest and base manifest hash. Invalid or stale bindings stop recovery
or the scheduled job with an explicit error.

Use the same override for all services whose runtime environment needs it,
including the `event-enrichment` cron profile. Recording overrides for API and
Worker alone does not change the cron profile's environment.

Keep deployment and current-state updates inside the existing `release.lock`.
Otherwise the health monitor can reconcile a newly started image back to the
version still recorded in `current.state`.

The standard `scripts/release.sh` flow does not inherit forced overrides. A
successful standard release removes the two active override pointers while
preserving their original files and receipts. A failed standard release keeps
the current runtime pointers. A future intentional forced redeployment must
record its own matching runtime configuration explicitly.

## Restoring the recorded .19 configuration from .20

The 2026-09-06 production release is `my-ai-filter:2026.09.06.20`, image ID
`sha256:f6bf784637a50658dd69f2dc3927a7a1c53de61cde3b09ff762e3e3e865c9b4d`.
Its recorded previous release is `my-ai-filter:2026.08.31.19`, image ID
`sha256:7e5e75b3301524c3f7b1f3ca076f1fea03872f65a66f8b2554a16d7dd51463dd`.
Both use base manifest
`d42d1278ce539870f9876fa7ec456112ca773704b8b817fed6353f3a8b7a3b57`.
These are historical identities; use this procedure only while the recorded
current/previous pair still describes that exact transition.

`release.sh rollback` is not equivalent to restoring this forced configuration:
it runs the standard migration checks and does not load the override. Also,
`previous.state` contains only four lines, not a reference to an override.
The retained .19 override is
`data/releases/force-v19-no-migration-fence.compose.yml` and its sibling
`force-v19-no-migration-fence.receipt`. That original override disables the gate
for API and Worker only. Restoring it does not preserve .20's additional
event-enrichment exception; that scheduled job can encounter the former gate
error. An expanded .19 exception would be a separate runtime change, not an
exact restoration.

When this specific recovery has been chosen, use the existing deployment and
registry operations in this order; no new recovery runner is required:

1. Hold the existing `data/releases/release.lock` exclusively through the
   container switch and final registry updates. Keep its inode intact. Retain
   .20's current state, override and receipt before changing active pointers;
   the lock prevents the health monitor from treating the switch as drift.
2. Read .19's four-line `previous.state`: ref, image ID, base manifest hash,
   space-separated health services. Use the matching archived file under
   `data/releases/manifests/`. Select the retained .19 override and its receipt,
   and check their already-recorded binding before using them. Never reuse .20's
   receipt with a .19 ref or edit the archived base manifest in place.
3. Use .19's image ID and both Compose files for every application replacement,
   with image build/pull disabled. Keep the normal dependency order: API,
   gateway, Worker. Check each service against its own expected image and
   health state; the Nginx gateway, Redis and RSSHub are not application images.
   Preserve the running infrastructure where no configuration change is needed.
4. After the application services are healthy, save .20's former four-tuple as
   `previous.state` with matching `.ref`, `.digest`, `.manifest` and
   `.health-services` mirrors. Stage .19's matching mirrors, point
   `current.override` at the absolute .19 override path and write its actual
   SHA-256 to `current.override-sha256`. Publish the four-line `current.state`
   atomically, with a final newline, before releasing the lock. Retain both
   releases' override files and receipts. The health service list remains
   `redis rsshub ai-filter-api ai-filter-web ai-filter-worker`; the temporary
   event-enrichment job does not belong in it.
5. If any switch or metadata update fails, stop and record the actual running
   versions and pointer state. Do not mark recovery successful, repeat the
   deployment, or automatically chain another version. If the runtime and
   registry differ, use the existing maintenance sentinel to keep automated
   reconciliation paused before releasing the lock, until recovery is resolved.

The receipt is newline-separated `key=value` text without duplicate keys. Its
required binding is:

```text
status=forced_gate_bypass_healthy
ref=<the .19 ref>
digest=<the .19 image ID>
base_manifest_sha256=<the archived base manifest hash>
override_path=<absolute override path in the release registry directory>
override_sha256=<SHA-256 of the exact override bytes>
migration_gate_enforced=false
AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE=0
```

The override filename ends in `.compose.yml`; replace that suffix with
`.receipt` to locate its receipt. Both pointer files, the override and receipt
must be regular, non-symlink files. Keep the two pointer files together: either
both are absent for a standard deployment or both describe the active override.
Changing any override bytes requires updating its binding, not just its path.

This restores application image/configuration only. It does not undo refreshes,
mapping writes or event merges already committed to SQLite by .20. The .20
report records successful recent-window enrichment with history still pending;
neither an image rollback nor `gate=0` marks that history complete. In the current
implementation, `gate=0` permits writing when the gate file is absent; an
existing gate file is still checked for its state and authorization token.

## Performance changes

The public event feed drives evidence lookups from the selected event page,
using the existing `event_evidence(event_id, announcement_at)` index. This
avoids an SQLite plan that scans all evidence before joining the small page.
The event order, merged descendants and non-public evidence filtering are
unchanged.

Intelligence feeds batch ordinary active event identity lookups within one
request. Merged or invalid references retain the existing resolver. No cache
survives across requests.
