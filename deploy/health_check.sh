#!/usr/bin/env bash
# Core-service health monitor for cron. Recovery is deliberately conservative:
# three consecutive failures, a per-service cooldown, and one targeted
# manifest/digest reconciliation per run.

set -uo pipefail
umask 077

LOG="${HEALTH_LOG:-/var/log/ai-filter-health.log}"
LOCK="${HEALTH_LOCK:-/var/run/ai-filter-health.lock}"
STATE_DIR="${HEALTH_STATE_DIR:-/var/lib/ai-filter-health}"
TOTAL_TIMEOUT_SECONDS="${HEALTH_TOTAL_TIMEOUT_SECONDS:-360}"
FAILURE_THRESHOLD="${HEALTH_FAILURE_THRESHOLD:-3}"
RECOVERY_COOLDOWN_SECONDS="${HEALTH_RECOVERY_COOLDOWN_SECONDS:-1800}"
MIHOMO_ALERT_INTERVAL_SECONDS="${MIHOMO_ALERT_INTERVAL_SECONDS:-1800}"
AI_FILTER_ROOT="${AI_FILTER_ROOT:-/root/ai-rss-filter}"
RELEASE_REGISTRY_DIR="${HEALTH_RELEASE_REGISTRY_DIR:-${AI_FILTER_ROOT}/data/releases}"
RELEASE_LOCK="${RELEASE_REGISTRY_DIR}/release.lock"
RELEASE_STATE_FILE="${RELEASE_REGISTRY_DIR}/current.state"
RELEASE_MANIFEST_DIR="${RELEASE_REGISTRY_DIR}/manifests"
MAINTENANCE_FILE="${HEALTH_MAINTENANCE_FILE:-/var/lib/ai-filter-health/maintenance}"
declare -a RELEASE_STATE_OVERRIDE_ARGS=()
RELEASE_STATE_OVERRIDE_HASH=""

case "$TOTAL_TIMEOUT_SECONDS:$FAILURE_THRESHOLD:$RECOVERY_COOLDOWN_SECONDS:$MIHOMO_ALERT_INTERVAL_SECONDS" in
    *[!0-9:]*|:*|*::*|*:) exit 64 ;;
esac
if (( TOTAL_TIMEOUT_SECONDS == 0 || FAILURE_THRESHOLD == 0 || RECOVERY_COOLDOWN_SECONDS == 0 || MIHOMO_ALERT_INTERVAL_SECONDS == 0 )); then
    exit 64
fi

# Redirect before doing any work so a non-zero cron exit never mails command
# output (or an accidentally printed secret). The script writes only to LOG.
: >> "$LOG" || exit 73
chmod 600 "$LOG" || exit 73
exec </dev/null >> "$LOG" 2>&1

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

# GNU timeout bounds the complete run, including Docker/systemd commands. This
# wrapper is intentionally outside the flock so timeout can kill a wedged child.
if [[ "${HEALTH_CHECK_TIMEOUT_CHILD:-0}" != "1" ]]; then
    if ! command -v timeout >/dev/null 2>&1; then
        log "ERROR: timeout command is unavailable; refusing an unbounded health run"
        exit 69
    fi
    export HEALTH_CHECK_TIMEOUT_CHILD=1
    timeout --signal=TERM --kill-after=5s "${TOTAL_TIMEOUT_SECONDS}s" "$0" "$@"
    wrapper_status=$?
    if (( wrapper_status == 124 || wrapper_status == 137 )); then
        log "ERROR: health run exceeded the ${TOTAL_TIMEOUT_SECONDS}s total deadline"
    fi
    exit "$wrapper_status"
fi

if ! command -v flock >/dev/null 2>&1; then
    log "ERROR: required command is unavailable: flock"
    exit 69
fi

exec 9>"$LOCK" || {
    log "ERROR: cannot open health-check lock"
    exit 73
}
if flock -n -E 75 9; then
    :
else
    lock_status=$?
    if (( lock_status == 75 )); then
        exit 0
    fi
    log "ERROR: could not acquire health-check lock (exit ${lock_status})"
    exit "$lock_status"
fi

mkdir -p "$STATE_DIR" || {
    log "ERROR: cannot create health state directory"
    exit 73
}
chmod 700 "$STATE_DIR" || {
    log "ERROR: cannot secure health state directory"
    exit 73
}

for required_command in bash basename chmod curl date docker env flock mkdir mv python3 rm sha256sum sleep systemctl timeout; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
        log "ERROR: required command is unavailable: ${required_command}"
        exit 69
    fi
done

overall_status=0
recovery_performed=0

read_uint() {
    local file=$1 value=""
    if [[ -r "$file" ]]; then
        IFS= read -r value < "$file" || value=""
    fi
    if [[ "$value" =~ ^[0-9]+$ ]]; then
        printf '%s\n' "$value"
    else
        printf '0\n'
    fi
}

write_uint() {
    local file=$1 value=$2 tmp
    tmp="${file}.tmp.$$"
    if ! printf '%s\n' "$value" > "$tmp" || ! chmod 600 "$tmp" || ! mv -f -- "$tmp" "$file"; then
        rm -f -- "$tmp"
        log "ERROR: cannot update health state for $(basename "$file")"
        overall_status=1
        return 1
    fi
}

throttled_log() {
    local key=$1 interval=$2
    shift 2
    local state_file="${STATE_DIR}/${key}.last-log" now last
    now=$(date +%s)
    last=$(read_uint "$state_file")
    if (( now - last >= interval )); then
        log "$*"
        write_uint "$state_file" "$now"
    fi
}

probe_http() {
    local port=$1 endpoint=$2 host=${3:-127.0.0.1} http_code=""
    http_code=$(timeout --signal=TERM --kill-after=1s 7s \
        curl --silent --show-error --noproxy '*' --output /dev/null \
        --write-out '%{http_code}' --connect-timeout 2 --max-time 5 \
        "http://${host}:${port}${endpoint}" 2>/dev/null) || return 1
    [[ "$http_code" =~ ^(200|301|302)$ ]]
}

probe_tcp() {
    local port=$1
    timeout --signal=TERM --kill-after=1s 4s \
        bash -c 'exec 3<>"/dev/tcp/127.0.0.1/$1"' _ "$port" \
        >/dev/null 2>&1
}

probe_compose_health() {
    local compose_dir=$1 service=$2 manifest=$3 image_ref=$4 expected_image_id=$5
    local container_ids="" container_id inspect_state state_status actual_image_id found=0

    [[ "$expected_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1

    container_ids=$(
        IMAGE_REF="$image_ref" timeout --signal=TERM --kill-after=1s 5s \
            docker compose --project-directory "$compose_dir" -f "$manifest" \
            ${RELEASE_STATE_OVERRIDE_ARGS[@]+"${RELEASE_STATE_OVERRIDE_ARGS[@]}"} \
            ps -q "$service"
    ) 2>/dev/null || return 1

    while IFS= read -r container_id; do
        [[ -n "$container_id" ]] || continue
        found=1
        inspect_state=$(timeout --signal=TERM --kill-after=1s 4s \
            docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}|{{.Image}}' \
            "$container_id" 2>/dev/null) || return 1
        state_status=${inspect_state%%|*}
        actual_image_id=${inspect_state#*|}
        [[ "$actual_image_id" == "$expected_image_id" ]] || return 1
        # Old archived releases did not give every process a Docker
        # HEALTHCHECK.  They remain valid rollback targets, so a service with
        # no Health object is accepted only when Docker reports it running.
        [[ "$state_status" == "healthy" || "$state_status" == "running" ]] || return 1
    done <<< "$container_ids"

    (( found == 1 ))
}

AI_RELEASE_EXPECTED_IMAGE_MAP=""

expected_release_image_id() {
    local wanted=$1 service image_id
    while IFS=$'\t' read -r service image_id; do
        if [[ "$service" == "$wanted" ]]; then
            printf '%s\n' "$image_id"
            return 0
        fi
    done <<< "$AI_RELEASE_EXPECTED_IMAGE_MAP"
    return 1
}

resolve_release_image_ids() {
    local manifest=$1 image_ref=$2 services=$3 config_json mapping
    local service expected_ref expected_id count=0 expected_count=0

    config_json=$(
        IMAGE_REF="$image_ref" timeout --signal=TERM --kill-after=1s 7s \
            docker compose --project-directory "$AI_FILTER_ROOT" -f "$manifest" \
            ${RELEASE_STATE_OVERRIDE_ARGS[@]+"${RELEASE_STATE_OVERRIDE_ARGS[@]}"} \
            config --format json
    ) 2>/dev/null || return 1
    mapping=$(printf '%s' "$config_json" | python3 -c '
import json
import sys

config = json.load(sys.stdin)
declared = config.get("services")
if not isinstance(declared, dict):
    raise SystemExit(1)
for service in sys.argv[1:]:
    item = declared.get(service)
    image = item.get("image") if isinstance(item, dict) else None
    if not isinstance(image, str) or not image or "\t" in image or "\n" in image:
        raise SystemExit(1)
    print(f"{service}\t{image}")
' $services) || return 1

    AI_RELEASE_EXPECTED_IMAGE_MAP=""
    for service in $services; do
        expected_count=$((expected_count + 1))
    done
    while IFS=$'\t' read -r service expected_ref; do
        [[ -n "$service" && -n "$expected_ref" ]] || return 1
        expected_id=$(timeout --signal=TERM --kill-after=1s 5s \
            docker image inspect "$expected_ref" --format '{{.Id}}' 2>/dev/null) || return 1
        [[ "$expected_id" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
        if [[ -n "$AI_RELEASE_EXPECTED_IMAGE_MAP" ]]; then
            AI_RELEASE_EXPECTED_IMAGE_MAP+=$'\n'
        fi
        AI_RELEASE_EXPECTED_IMAGE_MAP+="${service}"$'\t'"${expected_id}"
        count=$((count + 1))
    done <<< "$mapping"
    (( count == expected_count ))
}

load_release_override() {
    local binding override_path
    RELEASE_STATE_OVERRIDE_ARGS=()
    RELEASE_STATE_OVERRIDE_HASH=""
    # These sidecars belong to the explicitly forced active release only.
    # Never read an arbitrary Compose override from the working tree, and never
    # allow an old receipt to authorize recovery of a different release.
    binding=$(python3 - "$RELEASE_REGISTRY_DIR" "$RELEASE_STATE_REF" \
        "$RELEASE_STATE_DIGEST" "$RELEASE_STATE_MANIFEST_HASH" <<'PY'
import hashlib
import re
import sys
from pathlib import Path

registry = Path(sys.argv[1])
path_file = registry / "current.override"
hash_file = registry / "current.override-sha256"

def read_regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("override metadata is not a regular file")
    return path.read_text(encoding="utf-8").strip()

try:
    if not any(p.exists() or p.is_symlink() for p in (path_file, hash_file)):
        raise SystemExit(0)
    override_text = read_regular(path_file)
    override = Path(override_text)
    expected_hash = read_regular(hash_file)
    if (not override.is_absolute() or override.parent != registry
            or not override.name.endswith(".compose.yml")
            or any(c in override_text for c in "\r\n\t")
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        raise ValueError("invalid override path or hash")
    read_regular(override)
    if hashlib.sha256(override.read_bytes()).hexdigest() != expected_hash:
        raise ValueError("override hash mismatch")
    receipt = override.with_name(override.name.removesuffix(".compose.yml") + ".receipt")
    fields = {}
    for line in read_regular(receipt).splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in fields:
            raise ValueError("invalid forced release receipt")
        fields[key] = value
    expected = {
        "status": "forced_gate_bypass_healthy",
        "ref": sys.argv[2],
        "digest": sys.argv[3],
        "base_manifest_sha256": sys.argv[4],
        "override_path": override_text,
        "override_sha256": expected_hash,
        "migration_gate_enforced": "false",
        "AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE": "0",
    }
    if any(fields.get(key) != value for key, value in expected.items()):
        raise ValueError("forced receipt does not match the active release")
    print(override_text)
    print(expected_hash)
except (OSError, UnicodeError, ValueError) as exc:
    print(f"ERROR: forced release override invalid; automatic recovery disabled: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
    ) || return 1
    if [[ -n "$binding" ]]; then
        override_path=${binding%%$'\n'*}
        RELEASE_STATE_OVERRIDE_HASH=${binding#*$'\n'}
        RELEASE_STATE_OVERRIDE_ARGS=(-f "$override_path")
    fi
}

load_release_state() {
    local validate_manifest_services=${1:-1}
    local value actual_hash configured service found
    local -a state=() configured_services=()

    if [[ -L "$RELEASE_STATE_FILE" || ! -f "$RELEASE_STATE_FILE" || ! -r "$RELEASE_STATE_FILE" || ! -s "$RELEASE_STATE_FILE" ]]; then
        return 1
    fi
    while IFS= read -r value; do
        state[${#state[@]}]="$value"
    done < "$RELEASE_STATE_FILE"
    if [[ "${#state[@]}" -ne 4 ]]; then
        return 1
    fi
    for value in "${state[@]}"; do
        [[ -n "$value" && "$value" != *$'\r'* ]] || return 1
    done

    RELEASE_STATE_REF="${state[0]}"
    RELEASE_STATE_DIGEST="${state[1]}"
    RELEASE_STATE_MANIFEST_HASH="${state[2]}"
    RELEASE_STATE_HEALTH_SERVICES="${state[3]}"
    if [[ "$RELEASE_STATE_REF" == "latest" || "$RELEASE_STATE_REF" == *":latest" ]]; then
        return 1
    fi
    if [[ ! "$RELEASE_STATE_REF" =~ ^sha256:[0-9a-f]{64}$ && \
          ! "$RELEASE_STATE_REF" =~ ^[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}$ && \
          ! "$RELEASE_STATE_REF" =~ ^[A-Za-z0-9._:/-]+:v?[0-9][A-Za-z0-9._-]*$ ]]; then
        return 1
    fi
    [[ "$RELEASE_STATE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
    [[ "$RELEASE_STATE_MANIFEST_HASH" =~ ^[0-9a-f]{64}$ ]] || return 1

    RELEASE_STATE_MANIFEST_FILE="${RELEASE_MANIFEST_DIR}/${RELEASE_STATE_MANIFEST_HASH}.compose.yml"
    if [[ -L "$RELEASE_STATE_MANIFEST_FILE" || ! -f "$RELEASE_STATE_MANIFEST_FILE" || ! -r "$RELEASE_STATE_MANIFEST_FILE" ]]; then
        return 1
    fi
    actual_hash=$(sha256sum -- "$RELEASE_STATE_MANIFEST_FILE") || return 1
    actual_hash=${actual_hash%% *}
    [[ "$actual_hash" == "$RELEASE_STATE_MANIFEST_HASH" ]] || return 1
    load_release_override || return 1

    IFS=' ' read -r -a configured_services <<< "$RELEASE_STATE_HEALTH_SERVICES"
    for service in "${configured_services[@]}"; do
        [[ "$service" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || return 1
    done
    (( ${#configured_services[@]} > 0 )) || return 1

    if [[ "$validate_manifest_services" == "1" ]]; then
        configured=$(
            IMAGE_REF="$RELEASE_STATE_DIGEST" \
                timeout --signal=TERM --kill-after=1s 7s \
                docker compose --project-directory "$AI_FILTER_ROOT" \
                -f "$RELEASE_STATE_MANIFEST_FILE" \
                ${RELEASE_STATE_OVERRIDE_ARGS[@]+"${RELEASE_STATE_OVERRIDE_ARGS[@]}"} config --services
        ) 2>/dev/null || return 1
        for service in "${configured_services[@]}"; do
            found=0
            while IFS= read -r value; do
                if [[ "$value" == "$service" ]]; then
                    found=1
                    break
                fi
            done <<< "$configured"
            (( found == 1 )) || return 1
        done
    elif [[ "$validate_manifest_services" != "0" ]]; then
        return 1
    fi
}

release_state_matches_snapshot() {
    [[ "$RELEASE_STATE_DIGEST" == "$AI_RELEASE_DIGEST" && \
       "$RELEASE_STATE_MANIFEST_HASH" == "$AI_RELEASE_MANIFEST_HASH" && \
       "$RELEASE_STATE_OVERRIDE_HASH" == "$AI_RELEASE_OVERRIDE_HASH" && \
       "$RELEASE_STATE_HEALTH_SERVICES" == "$AI_RELEASE_HEALTH_SERVICES" ]]
}

prepare_release_snapshot() {
    local lock_status
    exec 6>"$RELEASE_LOCK" || return 1
    if flock -s -w 1 6; then
        :
    else
        lock_status=$?
        (( lock_status == 1 )) && return 75
        return "$lock_status"
    fi
    if ! load_release_state; then
        flock -u 6
        return 1
    fi
    AI_RELEASE_DIGEST="$RELEASE_STATE_DIGEST"
    AI_RELEASE_MANIFEST_HASH="$RELEASE_STATE_MANIFEST_HASH"
    AI_RELEASE_HEALTH_SERVICES="$RELEASE_STATE_HEALTH_SERVICES"
    AI_RELEASE_MANIFEST_FILE="$RELEASE_STATE_MANIFEST_FILE"
    AI_RELEASE_OVERRIDE_HASH="$RELEASE_STATE_OVERRIDE_HASH"
    if ! resolve_release_image_ids "$AI_RELEASE_MANIFEST_FILE" \
        "$AI_RELEASE_DIGEST" "$AI_RELEASE_HEALTH_SERVICES"; then
        flock -u 6
        return 1
    fi
    flock -u 6
}

perform_probe() {
    local probe_kind=$1 compose_dir=$2 target_service=$3
    shift 3
    case "$probe_kind" in
        http)
            probe_http "$@"
            ;;
        compose-health)
            # Release-managed Compose probes are dispatched by run_probe so
            # the archived manifest and recorded digest are always explicit.
            return 64
            ;;
        tcp)
            probe_tcp "$1"
            ;;
        *)
            return 64
            ;;
    esac
}

run_probe() {
    local probe_kind=$1 compose_dir=$2 release_guard=$3 target_service=$4
    shift 4

    if [[ "$release_guard" == "release" ]]; then
        # Avoid false failures while deploy/rollback owns the lock. Re-read the
        # atomic release bundle under that lock and make sure it still matches
        # the snapshot used to enumerate services for this health run.
        (
            exec 7>"$RELEASE_LOCK"
            flock -s -w 1 7 || exit 75
            load_release_state 0 || exit 1
            release_state_matches_snapshot || exit 75
            case "$probe_kind" in
                compose-health)
                    probe_compose_health "$compose_dir" "$target_service" \
                        "$RELEASE_STATE_MANIFEST_FILE" "$RELEASE_STATE_DIGEST" \
                        "$(expected_release_image_id "$target_service")"
                    ;;
                *)
                    perform_probe "$probe_kind" "$compose_dir" "$target_service" "$@"
                    ;;
            esac
        )
        return $?
    fi

    perform_probe "$probe_kind" "$compose_dir" "$target_service" "$@"
}

begin_recovery_attempt() {
    local key=$1 recovery_file=$2 display_name=$3 service=$4
    # Maintenance may have started since the initial probe. Check again at the
    # actual recovery boundary (under release.lock for managed services).
    if [[ -e "$MAINTENANCE_FILE" || -L "$MAINTENANCE_FILE" || \
          -e "${STATE_DIR}/${key}.maintenance" || -L "${STATE_DIR}/${key}.maintenance" ]]; then
        log "INFO: ${display_name} recovery deferred by maintenance sentinel"
        return 75
    fi
    # Persist only an attempt that is about to run. Keep it even when Docker
    # fails or times out, to prevent a restart storm on the next minute.
    if ! write_uint "$recovery_file" "$(date +%s)"; then
        log "ERROR: recovery skipped because the cooldown state could not be persisted"
        return 1
    fi
    recovery_performed=1
    log "RECOVER: reconciling only ${display_name} target service: ${service}"
}

reconcile_recorded_service() {
    local compose_dir=$1 service=$2 key=$3 recovery_file=$4 display_name=$5
    local command_timeout_seconds=110

    # The API has a 45-second graceful stop window. Compose up returns once the
    # replacement container is created; application readiness is verified by
    # check_service below. Keep this command plus the API's 90-second readiness
    # window inside the health script's 360-second total deadline.
    if [[ "$service" == "ai-filter-api" ]]; then
        command_timeout_seconds=55
    fi
    load_release_state 0 || return 1
    release_state_matches_snapshot || return 75
    begin_recovery_attempt "$key" "$recovery_file" "$display_name" "$service" || return $?
    # A plain restart preserves drifted image/config; reconcile exactly the
    # manifest, override and digest revalidated under the exclusive lock.
    timeout --signal=TERM --kill-after=5s "${command_timeout_seconds}s" \
        env IMAGE_REF="$RELEASE_STATE_DIGEST" \
        docker compose --project-directory "$compose_dir" \
        -f "$RELEASE_STATE_MANIFEST_FILE" \
        ${RELEASE_STATE_OVERRIDE_ARGS[@]+"${RELEASE_STATE_OVERRIDE_ARGS[@]}"} \
        up -d --no-deps --force-recreate --pull never "$service" >/dev/null 2>&1
}

restart_compose_service() {
    local compose_dir=$1 release_guard=$2 service=$3 key=$4 recovery_file=$5 display_name=$6
    local restart_status
    if [[ "$release_guard" == "release" ]]; then
        # release.sh takes an exclusive lock on the same inode. A health run
        # must never race a deployment or rollback. Run in this shell so the
        # one-attempt budget changes only after the recheck succeeds.
        exec 8>"$RELEASE_LOCK" || return 1
        if flock -w 1 8; then
            reconcile_recorded_service "$compose_dir" "$service" "$key" "$recovery_file" "$display_name"
            restart_status=$?
        else
            restart_status=75
        fi
        exec 8>&-
        return "$restart_status"
    fi

    if [[ ! -f "${compose_dir}/docker-compose.yml" ]]; then
        log "ERROR: Compose file is missing under ${compose_dir}"
        return 1
    fi
    begin_recovery_attempt "$key" "$recovery_file" "$display_name" "$service" || return $?
    (
        cd "$compose_dir" || exit 1
        timeout --signal=TERM --kill-after=3s 20s \
            docker compose restart "$service"
    ) >/dev/null 2>&1
}

check_service() {
    local key=$1 display_name=$2 compose_dir=$3 release_guard=$4 target_service=$5 probe_kind=$6
    shift 6
    local failures_file="${STATE_DIR}/${key}.failures"
    local recovery_file="${STATE_DIR}/${key}.last-recovery"
    local failures now last_recovery elapsed attempt probe_status restart_status
    local post_restart_attempts=3
    local post_restart_deadline=0

    failures=$(read_uint "$failures_file")
    if [[ -e "${STATE_DIR}/${key}.maintenance" ]]; then
        if (( failures > 0 )); then
            write_uint "$failures_file" 0
        fi
        throttled_log "${key}.maintenance" 600 \
            "INFO: ${display_name} probe paused by per-service maintenance sentinel"
        return 0
    fi
    run_probe "$probe_kind" "$compose_dir" "$release_guard" "$target_service" "$@"
    probe_status=$?
    if (( probe_status == 0 )); then
        if (( failures > 0 )); then
            log "OK: ${display_name} healthy again; clearing ${failures} recorded failure(s)"
            write_uint "$failures_file" 0
        fi
        return 0
    fi
    if (( probe_status == 75 )); then
        throttled_log "${key}.release-busy" 600 \
            "INFO: ${display_name} probe deferred while a release command holds release.lock"
        return 0
    fi

    overall_status=1
    failures=$((failures + 1))
    write_uint "$failures_file" "$failures"
    log "WARN: ${display_name} health probe failed (${failures}/${FAILURE_THRESHOLD})"

    if (( failures < FAILURE_THRESHOLD )); then
        return 1
    fi

    now=$(date +%s)
    last_recovery=$(read_uint "$recovery_file")
    elapsed=$((now - last_recovery))
    if (( elapsed < RECOVERY_COOLDOWN_SECONDS )); then
        throttled_log "${key}.cooldown" 600 \
            "WARN: ${display_name} recovery suppressed by ${RECOVERY_COOLDOWN_SECONDS}s cooldown"
        return 1
    fi

    if (( recovery_performed > 0 )); then
        throttled_log "${key}.recovery-budget" 600 \
            "WARN: ${display_name} recovery deferred; this run already attempted one recovery"
        return 1
    fi

    restart_compose_service "$compose_dir" "$release_guard" "$target_service" \
        "$key" "$recovery_file" "$display_name"
    restart_status=$?
    if (( restart_status == 75 && recovery_performed == 0 )); then
        log "INFO: ${display_name} recovery deferred because the active release changed, release.lock is busy, or maintenance started"
        return 1
    fi
    if (( restart_status != 0 )); then
        log "ERROR: targeted recovery command failed for ${display_name}"
        return 1
    fi

    # A fresh API process initializes the existing database before Gunicorn is
    # ready. Production measurements put that path near 46 seconds, so allow
    # the same 90-second window advertised by Compose's start_period. Other
    # services retain the short verification loop.
    if [[ "$target_service" == "ai-filter-api" ]]; then
        post_restart_attempts=0
        post_restart_deadline=$(($(date +%s) + 90))
    fi
    attempt=0
    while true; do
        if (( post_restart_deadline > 0 )); then
            (( $(date +%s) < post_restart_deadline )) || break
        else
            (( attempt < post_restart_attempts )) || break
        fi
        attempt=$((attempt + 1))
        sleep 2
        run_probe "$probe_kind" "$compose_dir" "$release_guard" "$target_service" "$@"
        probe_status=$?
        if (( probe_status == 0 )); then
            log "OK: ${display_name} recovered after targeted reconciliation"
            write_uint "$failures_file" 0
            return 0
        fi
        if (( probe_status == 75 )); then
            log "INFO: ${display_name} post-restart verification deferred because the active release changed"
            return 1
        fi
    done

    log "ERROR: ${display_name} remains unhealthy after targeted reconciliation"
    return 1
}

check_mihomo_systemd() {
    # systemd owns restart policy. Cron only observes ActiveState and never
    # probes Telegram or starts/restarts mihomo itself.
    if timeout --signal=TERM --kill-after=1s 4s \
        systemctl is-active --quiet mihomo.service >/dev/null 2>&1; then
        return 0
    fi
    overall_status=1
    throttled_log "mihomo" "$MIHOMO_ALERT_INTERVAL_SECONDS" \
        "WARN: mihomo.service is not active; recovery is delegated to systemd"
    return 1
}

release_service_key() {
    # Preserve the existing counter/cooldown files for the current topology;
    # unknown services from an older rollback manifest get a deterministic,
    # already-validated filename.
    case "$1" in
        redis) printf '%s\n' "ai-redis" ;;
        rsshub) printf '%s\n' "ai-rsshub" ;;
        ai-filter-api) printf '%s\n' "ai-api" ;;
        ai-filter-worker) printf '%s\n' "ai-worker" ;;
        ai-filter-web) printf '%s\n' "ai-gateway" ;;
        *) printf 'ai-release-%s\n' "$1" ;;
    esac
}

if [[ -e "$MAINTENANCE_FILE" ]]; then
    shopt -s nullglob
    for failures_file in "${STATE_DIR}"/*.failures; do
        write_uint "$failures_file" 0
    done
    shopt -u nullglob
    throttled_log "global-maintenance" 600 \
        "INFO: health recovery paused by maintenance sentinel: ${MAINTENANCE_FILE}"
    exit 0
fi

check_mihomo_systemd

# External services retain host HTTP probes. AI-Filter services are enumerated
# from current.state and checked through the exact archived Compose manifest.
check_service "external-rsshub" "external RSSHub" "/root/rsshub" "none" "rsshub" \
    "http" "1200" "/healthz" "172.17.0.1"

prepare_release_snapshot
release_snapshot_status=$?
if (( release_snapshot_status == 0 )); then
    declare -a ai_release_services=()
    IFS=' ' read -r -a ai_release_services <<< "$AI_RELEASE_HEALTH_SERVICES"
    for release_service in "${ai_release_services[@]}"; do
        check_service "$(release_service_key "$release_service")" \
            "AI-Filter ${release_service}" "$AI_FILTER_ROOT" "release" \
            "$release_service" "compose-health"
    done
elif (( release_snapshot_status == 75 )); then
    throttled_log "ai-release-busy" 600 \
        "INFO: AI-Filter release probes deferred while release.lock is busy"
else
    overall_status=1
    throttled_log "ai-release-state" 600 \
        "ERROR: AI-Filter current.state or archived release manifest is invalid"
fi

check_service "glance" "Glance" "/root/glance" "none" "glance" \
    "http" "9998" "/" "172.17.0.1"
check_service "miniflux" "Miniflux" "/root/miniflux" "none" "miniflux" \
    "http" "8888" "/healthcheck" "172.17.0.1"

exit "$overall_status"
