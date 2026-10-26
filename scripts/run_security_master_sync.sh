#!/usr/bin/env bash
# Run the security-master synchronizer against the exact release currently
# serving production.  This maintenance entrypoint never reads the worktree
# Compose file and never executes a floating image reference.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REGISTRY_DIR="${RELEASE_REGISTRY_DIR:-${ROOT}/data/releases}"
STATE_FILE="${REGISTRY_DIR}/current.state"
MANIFEST_DIR="${REGISTRY_DIR}/manifests"
TASK_LOCK_FILE="${SECURITY_MASTER_LOCK_FILE:-${REGISTRY_DIR}/security-master-sync.lock}"
RELEASE_LOCK_FILE="${REGISTRY_DIR}/release.lock"
LOCK_TIMEOUT="${SECURITY_MASTER_RELEASE_LOCK_TIMEOUT_SECONDS-300}"
MAINTENANCE_FILE="${AI_FILTER_MAINTENANCE_FILE:-${HEALTH_MAINTENANCE_FILE:-/var/lib/ai-filter-health/maintenance}}"
RUN_TIMEOUT=900

if [[ "$MAINTENANCE_FILE" != /* ]]; then
  echo "Maintenance sentinel path must be absolute" >&2
  exit 78
fi
# Refuse both a regular sentinel and a broken sentinel symlink.  This check is
# intentionally before argument handling, release-state reads, lock creation,
# and Docker inspection so a published maintenance boundary wins every race
# with a newly triggered job.
if [[ -e "$MAINTENANCE_FILE" || -L "$MAINTENANCE_FILE" ]]; then
  echo "[SecurityMaster] maintenance active; synchronization refused ($MAINTENANCE_FILE)" >&2
  exit 75
fi

if [[ "$#" -ne 1 ]]; then
  echo "Usage: $0 --dry-run|--apply" >&2
  exit 1
fi
if [[ "$1" != "--dry-run" && "$1" != "--apply" ]]; then
  echo "Usage: $0 --dry-run|--apply" >&2
  exit 1
fi
MODE="$1"

if [[ ! "$LOCK_TIMEOUT" =~ ^[0-9]+$ ]]; then
  echo "SECURITY_MASTER_RELEASE_LOCK_TIMEOUT_SECONDS must be a non-negative integer" >&2
  exit 2
fi
for command_name in docker flock python3 sha256sum timeout; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command is unavailable: $command_name" >&2
    exit 69
  fi
done
if [[ ! -d "$REGISTRY_DIR" ]]; then
  echo "Release registry is missing: $REGISTRY_DIR" >&2
  exit 3
fi

validate_lock_path() {
  python3 "$ROOT/scripts/validate_lock_path.py" "$1" "${2:-}"
}

# A second synchronizer is never useful, including while the first invocation
# is waiting behind a deployment.
if ! validate_lock_path "$TASK_LOCK_FILE"; then
  echo "Unsafe security-master lock path: $TASK_LOCK_FILE" >&2
  exit 78
fi
exec 8>>"$TASK_LOCK_FILE"
if ! validate_lock_path "$TASK_LOCK_FILE" 8; then
  echo "Security-master lock changed while opening: $TASK_LOCK_FILE" >&2
  exit 78
fi
if ! flock -n 8; then
  echo "The security-master synchronizer is already running" >&2
  exit 7
fi

# Hold the release registry stable from state read through container exit.
if ! validate_lock_path "$RELEASE_LOCK_FILE"; then
  echo "Unsafe release lock path: $RELEASE_LOCK_FILE" >&2
  exit 78
fi
exec 9>>"$RELEASE_LOCK_FILE"
if ! validate_lock_path "$RELEASE_LOCK_FILE" 9; then
  echo "Release lock changed while opening: $RELEASE_LOCK_FILE" >&2
  exit 78
fi
if ! flock -s -w "$LOCK_TIMEOUT" 9; then
  echo "Timed out waiting for the release lock" >&2
  exit 6
fi

if [[ -L "$STATE_FILE" || ! -f "$STATE_FILE" || ! -r "$STATE_FILE" || ! -s "$STATE_FILE" ]]; then
  echo "Current release state is missing or unsafe: $STATE_FILE" >&2
  exit 3
fi

declare -a state=()
while IFS= read -r value; do
  state[${#state[@]}]="$value"
done < "$STATE_FILE"
if [[ "${#state[@]}" -ne 4 ]]; then
  echo "Invalid current release state: expected exactly four lines" >&2
  exit 3
fi
for value in "${state[@]}"; do
  if [[ -z "$value" || "$value" == *$'\r'* ]]; then
    echo "Invalid current release state: empty or malformed field" >&2
    exit 3
  fi
done

IMAGE_REF="${state[0]}"
RECORDED_DIGEST="${state[1]}"
MANIFEST_HASH="${state[2]}"
HEALTH_SERVICES="${state[3]}"

if [[ "$IMAGE_REF" == *$'\n'* || "$IMAGE_REF" == *$'\r'* ]]; then
  echo "Invalid image ref in current release state" >&2
  exit 3
fi
if [[ "$IMAGE_REF" == "latest" || "$IMAGE_REF" == *":latest" ]]; then
  echo "Refusing floating latest image: $IMAGE_REF" >&2
  exit 3
fi
if [[ ! "$IMAGE_REF" =~ ^sha256:[0-9a-f]{64}$ ]] && \
   [[ ! "$IMAGE_REF" =~ ^[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}$ ]] && \
   [[ ! "$IMAGE_REF" =~ ^[A-Za-z0-9._:/-]+:v?[0-9][A-Za-z0-9._-]*$ ]]; then
  echo "Invalid immutable image ref in current release state: $IMAGE_REF" >&2
  exit 3
fi
if [[ ! "$RECORDED_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "Invalid image digest in current release state" >&2
  exit 3
fi
if [[ ! "$MANIFEST_HASH" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Invalid manifest hash in current release state" >&2
  exit 3
fi

MANIFEST_FILE="${MANIFEST_DIR}/${MANIFEST_HASH}.compose.yml"
if [[ -L "$MANIFEST_FILE" || ! -f "$MANIFEST_FILE" || ! -r "$MANIFEST_FILE" ]]; then
  echo "Archived Compose manifest is missing or unsafe: $MANIFEST_FILE" >&2
  exit 3
fi
ACTUAL_MANIFEST_HASH="$(sha256sum -- "$MANIFEST_FILE")"
ACTUAL_MANIFEST_HASH="${ACTUAL_MANIFEST_HASH%% *}"
if [[ "$ACTUAL_MANIFEST_HASH" != "$MANIFEST_HASH" ]]; then
  echo "Archived Compose manifest hash mismatch: $MANIFEST_FILE" >&2
  exit 3
fi

if ! docker image inspect "$IMAGE_REF" >/dev/null 2>&1; then
  echo "Recorded production image is not available locally: $IMAGE_REF" >&2
  exit 3
fi
ACTUAL_DIGEST="$(docker image inspect "$IMAGE_REF" --format '{{.Id}}')"
if [[ "$ACTUAL_DIGEST" != "$RECORDED_DIGEST" ]]; then
  echo "Recorded production image digest mismatch for $IMAGE_REF" >&2
  exit 3
fi

if ! CONFIGURED_SERVICES="$(
  IMAGE_REF="$RECORDED_DIGEST" docker compose --project-directory "$ROOT" \
    -f "$MANIFEST_FILE" --profile security-master config --services
)"; then
  echo "Unable to validate the archived Compose manifest" >&2
  exit 3
fi
SERVICE_FOUND=0
while IFS= read -r service_name; do
  if [[ "$service_name" == "security-master-sync" ]]; then
    SERVICE_FOUND=1
  fi
done <<< "$CONFIGURED_SERVICES"
if [[ "$SERVICE_FOUND" -ne 1 ]]; then
  echo "Current release manifest does not define security-master-sync" >&2
  exit 3
fi

if ! CONFIG_JSON="$(
  IMAGE_REF="$RECORDED_DIGEST" docker compose --project-directory "$ROOT" \
    -f "$MANIFEST_FILE" --profile security-master config --format json
)"; then
  echo "Unable to render the archived Compose manifest" >&2
  exit 3
fi
if ! CONFIGURED_IMAGE="$(
  printf '%s' "$CONFIG_JSON" | python3 -c '
import json
import sys

try:
    config = json.load(sys.stdin)
    image = config["services"]["security-master-sync"]["image"]
except (KeyError, TypeError, ValueError):
    raise SystemExit(1)
if not isinstance(image, str):
    raise SystemExit(1)
print(image)
'
)"; then
  echo "Unable to verify the security-master-sync image" >&2
  exit 3
fi
if [[ "$CONFIGURED_IMAGE" != "$RECORDED_DIGEST" ]]; then
  echo "security-master-sync does not resolve to the recorded production digest" >&2
  exit 3
fi

echo "[SecurityMaster] mode=${MODE#--} release=$IMAGE_REF digest=$RECORDED_DIGEST manifest=$MANIFEST_HASH"
run_worker() {
  IMAGE_REF="$RECORDED_DIGEST" timeout --signal=TERM --kill-after=30s \
    "${RUN_TIMEOUT}s" docker compose --project-directory "$ROOT" \
      -f "$MANIFEST_FILE" --profile security-master run --pull never --rm \
      --no-deps --entrypoint python security-master-sync \
      workers/security_master_collector.py "$@"
}
if [[ "$MODE" == "--apply" ]]; then
  run_worker --apply
else
  run_worker
fi
