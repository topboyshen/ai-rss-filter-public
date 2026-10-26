# Correction manifests in the public distribution

This distribution includes no production event identities, evidence snapshots,
source URLs, correction manifests, or approved legacy exceptions.
`ops.correct_event_mapping_allowlist.LEGACY_ALLOWLIST` is empty by default.

Nine wholly synthetic examples under `tests/fixtures/legacy_correction_manifests`
exercise exact-row, merged-group, evidence-count, identity-drift and write-scope
protections. Tests explicitly install their synthetic allowlist; runtime code
never imports or authorizes these examples. They are not production instructions.

For an independently reviewed correction, retain the existing per-row process:
read-only dry-run, review its exact plan digest, separate approval, then one
apply with a new durable audit file. No batch approval or generic bypass exists.
The original private operational evidence remains outside this repository.
