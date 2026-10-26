# Bounded takeover identity preflight

`scripts/takeover_readonly_preflight.py` collects a bounded identity snapshot.
It is not a producer stop, a queue-empty proof, a migration command, or a
deployment permit. It does not connect to SSH, open SQLite/PostgreSQL, call
HTTP operation recovery, inspect message bodies, or run a model.

The default invocation validates an explicit private manifest and prints its
hash without invoking Docker. `--collect` is reserved for a separately
authorized host observation. Preparing or testing this script does not
authorize that observation.

The manifest contains exactly `schema`, `docker_binary`, `docker_sha256`,
`containers`, `images`, and `files`. The schema is
`takeover-readonly-preflight-v1`. Container names are explicit (maximum eight),
retained images use full immutable image IDs (maximum four), and file targets
are explicit absolute paths (maximum sixteen), each with `kind` equal to
`metadata` or `sha256`. There is no wildcard discovery or arbitrary command.
The Docker binary is pinned to a reviewed `/usr/bin/docker` or
`/usr/local/bin/docker` hash and the local Unix socket. Caller Docker contexts,
remote endpoints, loader variables and inherited credentials are not used.

Input manifests must be regular, single-link, owned mode-0600 files. Source
hash targets may also be owned read-only files; symlink components, writable
shared files and changing files are refused. Hash reads are capped at 128 KiB.
Inspect output is bounded at 1 MiB including stderr; each Docker command has
a five-second timeout. A stage needs its complete timeout plus cleanup reserve
within the 25-second overall budget. The external invoker must also bound the
whole SSH/session lifetime: a local filesystem read can block in the kernel,
and killing a timed-out CLI does not prove any daemon work has been cancelled.

Each completed phase is flushed as JSONL before the next begins. The first
failure emits a fixed sanitized reason and stops without retrying. Raw stderr,
environment values, command-line values and mount paths are not printed;
execution configuration and mounts are represented by hashes. Only the owned
CLI subprocess is terminated on timeout, never a container or daemon.

The final record deliberately keeps `release_authorized`,
`legacy_originals_proven`, `queue_empty_proven`, `producer_stop_proven`, and
`database_compatibility_proven` false. Later takeover gates must separately
establish sender raw-original coverage, exact storage and schema bindings,
capacity, stopped producers and a locked independent inventory. An identity
snapshot cannot be reused as proof that live state remains unchanged.

The local regression suite uses synthetic Docker responses, actual bounded
short-lived local Python clients and temporary files. No production or Unraid
host is contacted. Missing historical originals stay unknown; this tool does
not create replacement evidence or refresh an expired approval window.
