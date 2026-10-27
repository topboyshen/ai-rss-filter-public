# Clean public source with a sealed private deployment

The optional `RELEASE_PRIVATE_CONTRACT` mode separates five identities: the
clean source commit/tree, its embedded public example `config.yaml`, the public
source Compose file, the mounted private runtime YAML, and the private deployment
manifest. The public example remains in the image and its existing label/hash
check remains mandatory. Every application role mounts the private YAML read-only
at `/app/config.yaml`; building never copies that YAML or credentials into the
Git archive, Docker context, image layers, or public CI artifacts.

Without this option, the existing release commands and gate remain unchanged.
Private mode requires `RELEASE_ALLOW_DIRTY_TREE=0`,
`RELEASE_REQUIRE_BUILD_CONTRACT=1`, `RELEASE_AUTO_ROLLBACK=0`, diagnostics off,
and no `RELEASE_COMPOSE_FILE`. It does not accept an emergency dirty override,
direct rollback, or an arbitrary external Compose file. The normal full-suite
release attestation, clean source check, unused immutable image ref, build
contract, image labels, embedded config hash and migration fences still apply.
This mode is a configuration contract, **not a production cutover authorization**.
The production controller must independently stop producers, drain durable work,
freeze consumers, and keep its release/admission locks and stopping conditions.

## Private review and sealing

Use CPython 3.11 with the project's pinned PyYAML available. Private files must
be absolute, owned by the releasing user, regular, single-link, mode 0400/0600,
and have no symlink in any path component. Keep inputs and the new output below
an owned mode-0700 private directory outside the source checkout. Preserve input
files: later checks reject content, inode, ownership, mode, timestamp, or secret
reference drift. A fresh seal requires a new output directory; failures are not
overwritten or silently retried. Frozen files are mode 0400 and their directory
0500, with file, directory, and parent-directory fsync. These permissions protect
against accidental edits, not a hostile root user.

The input deployment manifest uses canonical JSON (Compose accepts JSON). It
must explicitly set `name` to the existing project and use absolute bind paths,
long mount and port syntax, `bind.create_host_path: false`, and named volumes
and networks with `external: true` and exact existing `name`. Application images
use exactly `${IMAGE_REF:?}`; other images require immutable repository digests.
Every app role, including optional cron/maintenance roles, must mount source
`@RUNTIME_CONFIG@` at `/app/config.yaml` with `read_only: true` and set
`AI_FILTER_CONFIG_PATH=/app/config.yaml`. Sealing substitutes only that source.
No build context, include/extends, env_file, source-code mount, privileged mode,
container name, or indirect namespace reuse is accepted. Volume/bind targets
are restricted to explicit data directories and the frozen runtime YAML;
interpreter/site-packages mounts are rejected. Inline configs are allowed only
for a pinned official Nginx gateway at `/etc/nginx/nginx.conf` or
`/etc/nginx/conf.d/default.conf`; application roles cannot mount inline configs.
Secret names must be safe basenames. Volume, config and secret targets cannot
overlap or repeat within a service.

Runtime YAML must be distinct from the public example; example.invalid and
inline credentials in recognized sensitive fields are rejected. Credentials
belong in file-backed Compose secrets or a private environment input. That input
uses unique plain `NAME=value` records, with no quotes, escapes, dollar expansion,
Docker/Compose control names, host loader/executable controls such as PATH,
HOME, LD_PRELOAD and PYTHONPATH, or unused names. The manifest references them as
`${NAME:?}`. The environment file is itself frozen privately; the contract stores
hashes/reference metadata, never credential values. Ambient environment variables
cannot override those sealed values. Private values are supplied through the
explicit environment file only; they are never copied into the host subprocess
environment. Public CLI failures contain fixed messages,
not parser content, Docker stderr, private paths, or resolved Compose values.

An independently reviewed, private JSON receipt must contain:

- `approved: true`, `approved_by`, integer `approved_at`/`expires_at` Unix times,
  with a validity period no longer than six hours.
- `source_sha`, `source_tree`, `example_config_sha256`,
  `source_manifest_sha256`, `runtime_config_sha256`, `input_manifest_sha256`,
  and `environment_sha256`, calculated from exact reviewed bytes.
- `strategy`, the expected full scoring strategy version for this source and YAML;
  `project`, the exact existing Compose project; `project_directory`, its existing
  absolute working directory; and an explicit `health_services` array.
- `topology`, mapping each candidate service to its exact `mounts`, `networks`,
  and `ports` arrays, with `@RUNTIME_CONFIG@` retained in the reviewed mount.
- `existing_topology`, independently captured under the controller's locks from
  actual old `docker inspect` records. `runtime_topology()` supplies the canonical
  representation: each service maps to a sorted list of `{id, image, mounts,
  networks, user, group_add, ports}`. It is not inferred from the candidate or current entries.
- `service_mapping`, mapping every candidate role to an explicit list of old
  roles whose writable data mounts it may reuse. This supports a reviewed
  old-web → new-api role split without permitting a cross-role data-path swap.
  The full project data/network/port set must still be preserved.

The `seal` command validates and consumes this receipt; it does not approve it:

```sh
python3 -I scripts/private_release_contract.py seal --root /clean/public-checkout \
  --config /private/release-inputs/runtime.yaml \
  --manifest /private/release-inputs/compose.json \
  --environment /private/release-inputs/environment \
  --review /private/release-inputs/review.json \
  --output /private/release-inputs/new-sealed-release
```

Use the resulting `contract.json` for all three steps, after the normal release
gate passes for the exact clean SHA:

```sh
export RELEASE_PRIVATE_CONTRACT=/private/release-inputs/new-sealed-release/contract.json
export RELEASE_AUTO_ROLLBACK=0
./scripts/release.sh build my-ai-filter:NEW-IMMUTABLE-VERSION
# The production controller owns any subsequent deploy and explicit reopening.
./scripts/release.sh deploy my-ai-filter:NEW-IMMUTABLE-VERSION
./scripts/release.sh verify my-ai-filter:NEW-IMMUTABLE-VERSION
```

Replace the illustrative tag with a supported unused numeric version; never
reuse `.126`. Build/deploy require the review to remain fresh. Historical verify
retains integrity checks after expiry. Changing private inputs, strategy,
topology, source or credentials requires a new review, seal and image identity.
The local build contract records the private contract hash; omitting the private
option later cannot turn that image into a public-mode deployment. Additional
image labels bind the private contract hash and mounted runtime-config hash.

Compose uses the **existing project name and working directory**, regardless of
where the clean checkout or frozen manifest lives. Before mutation, actual old
containers must match the reviewed service-by-service identities and topology;
data mounts, ports and networks must also be preserved across the candidate.
Existing volumes/networks must already exist. Profile jobs already running stop
the preflight. Previous legacy config binds must be independently sealed before
using this mode; it will not snapshot the public example as an old production
config or rewrite rollback provenance during the cutover.

Build and deploy probe the actual image by immutable ID, network disabled,
read-only root, all capabilities dropped, no-new-privileges, 768 MiB/256 PID
limits, and the frozen YAML plus required private secret references. The probe
imports only the config loader; it makes no model call or database connection.
Each role's probe uses its reviewed numeric `user: UID:GID`, or the actual
candidate image's default identity (empty Docker User means `0:0`). Named users
and UID-only declarations/defaults are unsupported and refused; no identity is
guessed. The role UID must own the sealed mode-0400 YAML and its scoped secret
files. A mismatch stops release instead of changing ownership/permissions or
adding DAC capabilities. A root-based production role therefore requires a
root-owned private seal; this helper performs no host data ownership migration.

The probe receives image defaults plus that role's resolved environment through
a private mode-0600 temporary env file. Interpolation aliases are resolved to
actual role variable names, and only the role's declared secret files are mounted.
Its UID/GID and supplementary groups match the role contract while all probe
capabilities remain dropped. Temporary env files are removed after each probe;
values never enter host subprocess environment, Docker argv, or diagnostics.
Application roles and their loader probes must use `/app` as their working
directory, inherited from the image or explicitly frozen in the manifest. This
prevents a writable data directory from supplying the loader's Python imports.

After startup, verification checks actual app container image IDs, config mount
source/read-only status, effective environment, config bytes, and the strategy
computed by the loader inside each running role. It also checks each service's
mounts, networks, ports, replicas and embedded gateway-config bytes. Every
running service, including infrastructure, must use its exact immutable image,
reviewed Cmd/Entrypoint, image-plus-manifest environment (including role),
configured WorkingDir, Healthcheck, User/GroupAdd, and memory/swap/PID/CPU limits.
Execution context and actual mounts are checked before invoking the loader.
Healthcheck verification merges image defaults with the frozen manifest using
Docker's per-field inheritance; a healthy status cannot substitute for the
reviewed command. Supported fields are explicit list-form `test`, `disable`,
`interval`, `timeout`, `start_period`, `start_interval`, and `retries`. Durations
use one `ns`, `us`, `ms`, `s`, `m`, or `h` unit, optionally fractional;
`start_interval` requires an explicit `start_period`. Other forms are refused.
Privileged mode and added
capabilities are rejected; CapDrop and SecurityOpt must match the frozen manifest.
Supported capability drops are empty or ALL; supported extra security options are
empty or no-new-privileges. No new permission is granted to make a probe pass. Each manifest service requires explicit finite memory
(no more than 768 MiB) and PID limits (no more than 256). Omitted or implicit
new networks are rejected before mutation. This proves
the container's loader result; it is not independent proof that every long-lived
application object has reloaded its configuration, nor proof that old work drained.

## Validation and limits

`tests/test_private_release_contract.py` uses only synthetic files and Docker
inspect records. It covers source/input/permission/link/secret/reference drift,
stale receipts, wrong project/data/network/port/image/strategy, per-service mount
swaps, duplicate environment keys, and bounded private-safe subprocess failures.
The release script regressions still cover the unchanged full-suite gate and
embedded-config checks.

The `Private release config native` workflow builds the real production Dockerfile
from a Git archive on native amd64, creates a synthetic old Compose project,
reuses its volume/network/data paths, independently proves an incorrect UID
cannot read the mode-0400 seal while the reviewed role UID can, and verifies real labels, read-only mounts,
config hashes and strategy, plus the explicitly reviewed fixture command and
resource limits. Its sleeping fixture containers do not run the app,
producers, or models. It tests refusal after input mutation and wrong-image
verification. Only an allowlisted public result JSON is uploaded; private files
and Docker diagnostics never become workflow artifacts. This native workflow is
additional evidence, not a substitute for full CI, release attestation, old-image
compatibility testing, or the production takeover adapter.
