#!/usr/bin/env bash
# Run the market-close collector against the exact release that is currently
# serving production.  Cron callers must not guess or persist IMAGE_REF: the
# release registry is the single source of truth for both image and manifest.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REGISTRY_DIR="${RELEASE_REGISTRY_DIR:-${ROOT}/data/releases}"
STATE_FILE="${REGISTRY_DIR}/current.state"
MANIFEST_DIR="${REGISTRY_DIR}/manifests"
MARKET_LOCK_FILE="${MARKET_CLOSE_LOCK_FILE:-${REGISTRY_DIR}/market-close.lock}"
RELEASE_LOCK_FILE="${REGISTRY_DIR}/release.lock"
LOCK_TIMEOUT="${MARKET_CLOSE_RELEASE_LOCK_TIMEOUT_SECONDS-300}"
RUN_TIMEOUT="${MARKET_CLOSE_RUN_TIMEOUT_SECONDS-900}"
MAINTENANCE_FILE="${AI_FILTER_MAINTENANCE_FILE:-${HEALTH_MAINTENANCE_FILE:-/var/lib/ai-filter-health/maintenance}}"
RESULT_DIR="${MARKET_CLOSE_RESULT_DIR:-$ROOT/data/market_close}"

if [[ "$#" -ne 0 ]]; then
  echo "Usage: $0" >&2
  exit 1
fi
if [[ "$MAINTENANCE_FILE" != /* ]]; then
  echo "Maintenance sentinel path must be absolute" >&2
  exit 78
fi
# A broken symlink also pauses collection. This check is intentionally before
# release-state reads, Docker inspection and either lock acquisition.
# Trading-day 15:10 must not record systemd success for a skipped collection:
# ops-check treats Finished as market_timer OK even when no closes were written.
if [[ -e "$MAINTENANCE_FILE" || -L "$MAINTENANCE_FILE" ]]; then
  echo "[MarketClose] maintenance active; collection skipped ($MAINTENANCE_FILE)" >&2
  RESULT_DIR="${MARKET_CLOSE_RESULT_DIR:-$ROOT/data/market_close}"
  mkdir -p "$RESULT_DIR"
  if ! python3 - "$RESULT_DIR" <<'PY'
import json, sys, time, uuid
from pathlib import Path
directory = Path(sys.argv[1])
if not directory.is_absolute():
    raise SystemExit("MARKET_CLOSE_RESULT_DIR must be absolute")
path = directory / "collector_last.json"
payload = {
    "version": 1,
    "kind": "collector",
    "run_id": uuid.uuid4().hex,
    "trade_date": "",
    "status": "skipped_maintenance",
    "started_at": int(time.time()),
    "completed_at": int(time.time()),
    "due_total": 0,
    "unresolved": 0,
    "truncated": False,
    "written": 0,
    "exit_code": 75,
    "detail": "maintenance sentinel active",
}
tmp = path.with_name(path.name + ".tmp")
tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="ascii")
tmp.chmod(0o600)
tmp.replace(path)
PY
  then
    rm -f "$RESULT_DIR/collector_last.json"
  fi
  exit 75
fi
if [[ ! "$LOCK_TIMEOUT" =~ ^[0-9]+$ ]]; then
  echo "MARKET_CLOSE_RELEASE_LOCK_TIMEOUT_SECONDS must be a non-negative integer" >&2
  exit 2
fi
if [[ ! "$RUN_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
  echo "MARKET_CLOSE_RUN_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
fi
if [[ ! -d "$REGISTRY_DIR" ]]; then
  echo "Release registry is missing: $REGISTRY_DIR" >&2
  exit 3
fi

validate_lock_path() {
  python3 "$ROOT/scripts/validate_lock_path.py" "$1" "${2:-}"
}

# Reject a duplicate immediately, even if the first invocation is waiting for
# a deployment to release its lock.
if ! validate_lock_path "$MARKET_LOCK_FILE"; then
  echo "Unsafe market-close lock path: $MARKET_LOCK_FILE" >&2
  exit 78
fi
exec 8>>"$MARKET_LOCK_FILE"
if ! validate_lock_path "$MARKET_LOCK_FILE" 8; then
  echo "Market-close lock changed while opening: $MARKET_LOCK_FILE" >&2
  exit 78
fi
if ! flock -n 8; then
  echo "The market-close collector is already running" >&2
  exit 7
fi

# Own this attempt before any release preflight; an interrupted run must not
# leave the previous successful receipt looking like this invocation's result.
STARTED_AT="$(date +%s)"
RUN_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
PREVIOUS_RESULT="$(python3 - "$ROOT" "$RESULT_DIR" "$RUN_ID" "$STARTED_AT" <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from services.market_close_result import read_result, result_path, write_result
directory = Path(sys.argv[2])
if not directory.is_absolute():
    raise SystemExit("MARKET_CLOSE_RESULT_DIR must be absolute")
try:
    previous = read_result(result_path(directory, "collector"))
except (OSError, ValueError, TypeError):
    previous = None
write_result(directory, kind="collector", status="error", run_id=sys.argv[3],
             started_at=int(sys.argv[4]), completed_at=int(sys.argv[4]),
             detail="collector attempt has not completed", exit_code=1)
print(json.dumps(previous))
PY
)"

# Keep current.state and its archived manifest stable until the one-shot
# container exits.  A normal release can legitimately hold this lock for its
# full health-check window, hence the five-minute default wait.
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

if [[ ! -s "$STATE_FILE" ]]; then
  echo "Current release state is missing: $STATE_FILE" >&2
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

if [[ -z "$IMAGE_REF" || "$IMAGE_REF" == *$'\n'* || "$IMAGE_REF" == *$'\r'* ]]; then
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
if [[ ! -f "$MANIFEST_FILE" || ! -r "$MANIFEST_FILE" ]]; then
  echo "Archived Compose manifest is missing: $MANIFEST_FILE" >&2
  exit 3
fi
ACTUAL_MANIFEST_HASH="$(sha256sum -- "$MANIFEST_FILE" | awk '{print $1}')"
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

CONFIGURED_SERVICES="$(
  IMAGE_REF="$RECORDED_DIGEST" docker compose --project-directory "$ROOT" \
    -f "$MANIFEST_FILE" --profile cron config --services
)"
if ! grep -Fqx -- "market-close-collector" <<< "$CONFIGURED_SERVICES"; then
  echo "Current release manifest does not define market-close-collector" >&2
  exit 3
fi

echo "[MarketClose] release=$IMAGE_REF digest=$RECORDED_DIGEST manifest=$MANIFEST_HASH"
mkdir -p "$RESULT_DIR"
set +e
OUTPUT="$(IMAGE_REF="$RECORDED_DIGEST" timeout --signal=TERM --kill-after=30s \
  "${RUN_TIMEOUT}s" docker compose --project-directory "$ROOT" \
    -f "$MANIFEST_FILE" --profile cron run --pull never --rm --no-deps \
    -e "MARKET_CLOSE_RUN_ID=$RUN_ID" \
    market-close-collector 2>&1)"
RC=$?
set -e
printf '%s\n' "$OUTPUT"
if ! MARKET_CLOSE_HOST_OUTPUT="$OUTPUT" MARKET_CLOSE_PREVIOUS_RESULT="$PREVIOUS_RESULT" \
  python3 - "$ROOT" "$RESULT_DIR" "$RC" "$STARTED_AT" "$RUN_ID" <<'PY'
import json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from services.market_close_result import persist_host_fallback
directory = Path(sys.argv[2])
if not directory.is_absolute():
    raise SystemExit("MARKET_CLOSE_RESULT_DIR must be absolute")
persist_host_fallback(
    directory,
    exit_code=int(sys.argv[3]),
    started_at=int(sys.argv[4]),
    run_id=sys.argv[5],
    previous_result=json.loads(os.environ["MARKET_CLOSE_PREVIOUS_RESULT"]),
    output=os.environ.get("MARKET_CLOSE_HOST_OUTPUT", ""),
)
PY
then
  echo "[MarketClose] host fallback result write failed" >&2
  exit 1
fi
exit "$RC"
