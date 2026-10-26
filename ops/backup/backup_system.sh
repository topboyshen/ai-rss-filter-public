#!/usr/bin/env bash
# Verified, atomic system backup. Runtime data is never read into a live tar;
# SQLite databases are copied through SQLite's online backup API instead.
#
# Optional encrypted off-host upload contract:
#   SYSTEM_BACKUP_UPLOAD_HOOK=/absolute/root-owned/executable
# The hook receives ARCHIVE and SHA256_MANIFEST as its only two arguments. It
# must encrypt before network transfer, verify the remote object, and return 0
# only after durable completion. Hook output is discarded so credentials cannot
# reach cron logs. No destination is assumed or provisioned by this script.

set -Eeuo pipefail
umask 077

BACKUP_CONFIG_FILE="${SYSTEM_BACKUP_CONFIG_FILE:-/etc/ai-filter/backup.env}"
if [[ -f "$BACKUP_CONFIG_FILE" ]]; then
    # This file is installed root-only, but it is still data rather than shell
    # code. Parse an allowlisted KEY=VALUE subset so a typo or injected command
    # cannot execute as root before logging is initialized.
    if [[ ! -r "$BACKUP_CONFIG_FILE" ]]; then
        printf 'backup configuration is not readable\n' >&2
        exit 78
    fi
    config_uid=$(stat -c '%u' -- "$BACKUP_CONFIG_FILE")
    config_mode=$(stat -c '%a' -- "$BACKUP_CONFIG_FILE")
    if [[ "$config_uid" != "0" ]] || (( (8#$config_mode & 8#022) != 0 )); then
        printf 'backup configuration ownership or mode is unsafe\n' >&2
        exit 78
    fi
    command -v python3 >/dev/null 2>&1 || {
        printf 'python3 is required to parse backup configuration\n' >&2
        exit 69
    }
    parsed_config=$(python3 - "$BACKUP_CONFIG_FILE" <<'PY'
from pathlib import Path
import re
import sys

allowed = {
    "SYSTEM_BACKUP_DIR",
    "SYSTEM_BACKUP_LOG",
    "SYSTEM_BACKUP_LOCK",
    "SYSTEM_BACKUP_UPLOAD_HOOK",
    "SYSTEM_BACKUP_UPLOAD_REQUIRED",
    "SYSTEM_BACKUP_OFFSITE_STATE",
}
for number, raw in enumerate(Path(sys.argv[1]).read_text(encoding="utf-8").splitlines(), 1):
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        raise SystemExit(f"invalid backup configuration line {number}")
    key, value = line.split("=", 1)
    key, value = key.strip(), value.strip()
    if key not in allowed:
        raise SystemExit(f"unsupported backup configuration key at line {number}")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    if any(character in value for character in "\r\n\t\0"):
        raise SystemExit(f"invalid backup configuration value at line {number}")
    print(f"{key}\t{value}")
PY
    ) || exit 78
    while IFS=$'\t' read -r key value; do
        [[ -n "$key" ]] || continue
        printf -v "$key" '%s' "$value"
    done <<< "$parsed_config"
    unset parsed_config key value
fi

BACKUP_DIR="${SYSTEM_BACKUP_DIR:-/root/backups/system}"
LOG="${SYSTEM_BACKUP_LOG:-${BACKUP_DIR}/backup.log}"
LOCK="${SYSTEM_BACKUP_LOCK:-/var/run/system-backup.lock}"
UPLOAD_HOOK="${SYSTEM_BACKUP_UPLOAD_HOOK:-}"
UPLOAD_REQUIRED="${SYSTEM_BACKUP_UPLOAD_REQUIRED:-0}"
OFFSITE_STATE="${SYSTEM_BACKUP_OFFSITE_STATE:-${BACKUP_DIR}/.offsite-upload.state}"
DATE=$(date +%Y-%m-%d)
STAMP=$(date +%Y-%m-%dT%H%M%S)
ARCHIVE="${BACKUP_DIR}/system_backup_${STAMP}.tar.gz"
MANIFEST="${ARCHIVE}.sha256"
STAGE_DIR=""
SRC_DIR=""
ARCHIVE_TMP=""
MANIFEST_TMP=""
OFFSITE_STATE_TMP=""
AI_RELEASE_LOCK_HELD=0

case "$UPLOAD_REQUIRED" in
    0|1) ;;
    *) printf 'SYSTEM_BACKUP_UPLOAD_REQUIRED must be 0 or 1\n' >&2; exit 64 ;;
esac

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

cleanup() {
    local rc=$?
    trap - EXIT
    set +e
    if [[ -n "$STAGE_DIR" && "$STAGE_DIR" == "${BACKUP_DIR}/.system-backup."* ]]; then
        rm -rf -- "$STAGE_DIR"
    fi
    if [[ -n "$ARCHIVE_TMP" && "$ARCHIVE_TMP" == "${BACKUP_DIR}/.system_backup_"* ]]; then
        rm -f -- "$ARCHIVE_TMP"
    fi
    if [[ -n "$MANIFEST_TMP" && "$MANIFEST_TMP" == "${BACKUP_DIR}/.system_backup_"* ]]; then
        rm -f -- "$MANIFEST_TMP"
    fi
    if [[ -n "$OFFSITE_STATE_TMP" && "$OFFSITE_STATE_TMP" == "${BACKUP_DIR}/.offsite-upload."* ]]; then
        rm -f -- "$OFFSITE_STATE_TMP"
    fi
    exit "$rc"
}

trap cleanup EXIT
trap 'rc=$?; log "ERROR: system backup failed at line ${LINENO} (exit ${rc})"; exit "$rc"' ERR

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
: >> "$LOG"
chmod 600 "$LOG"
exec </dev/null >> "$LOG" 2>&1

for required_command in awk cp crontab find flock git gzip mktemp mv python3 sha256sum stat tar timeout; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
        log "ERROR: required command is unavailable: ${required_command}"
        exit 69
    fi
done

exec 9>"$LOCK"
if flock -n -E 75 9; then
    :
else
    lock_status=$?
    if (( lock_status != 75 )); then
        log "ERROR: could not acquire system backup lock (exit ${lock_status})"
        exit "$lock_status"
    fi
    log "INFO: another system backup is running; skipping this invocation"
    exit 0
fi

STAGE_DIR=$(mktemp -d "${BACKUP_DIR}/.system-backup.${DATE}.XXXXXX")
SRC_DIR="${STAGE_DIR}/${DATE}"
ARCHIVE_TMP=$(mktemp "${BACKUP_DIR}/.system_backup_${DATE}.tar.gz.XXXXXX")
MANIFEST_TMP=$(mktemp "${BACKUP_DIR}/.system_backup_${DATE}.tar.gz.sha256.XXXXXX")
chmod 700 "$STAGE_DIR"
chmod 600 "$ARCHIVE_TMP" "$MANIFEST_TMP"
mkdir -p "$SRC_DIR"

log "Starting verified system backup"

acquire_ai_release_snapshot_lock() {
    local project="/root/ai-rss-filter"
    local registry="${project}/data/releases"
    local lock_status

    if [[ ! -d "$project" ]]; then
        return 0
    fi
    mkdir -p "$registry"
    exec 8>"${registry}/release.lock"
    if flock -s -w 60 8; then
        AI_RELEASE_LOCK_HELD=1
        log "AI-Filter release snapshot lock acquired"
        return 0
    fi
    lock_status=$?
    exec 8>&-
    log "ERROR: could not acquire AI-Filter release snapshot lock (exit ${lock_status})"
    return "$lock_status"
}

release_ai_release_snapshot_lock() {
    if (( AI_RELEASE_LOCK_HELD == 1 )); then
        flock -u 8
        exec 8>&-
        AI_RELEASE_LOCK_HELD=0
        log "AI-Filter release snapshot lock released"
    fi
}

backup_compose_files() {
    local compose_root="${SRC_DIR}/docker-compose"
    local list_file="${STAGE_DIR}/compose-files.list"
    local file rel service_dir dest_dir
    mkdir -p "$compose_root"

    find /root -maxdepth 3 -type f -name docker-compose.yml -print0 > "$list_file"
    while IFS= read -r -d '' file; do
        rel="${file#/root/}"
        service_dir="${rel%/docker-compose.yml}"
        dest_dir="${compose_root}/${service_dir}"
        mkdir -p "$dest_dir"
        cp -a -- "$file" "${dest_dir}/docker-compose.yml"
    done < "$list_file"
    rm -f -- "$list_file"
}

backup_sqlite_file() {
    local src=$1 dst=$2
    mkdir -p "$(dirname "$dst")"
    python3 - "$src" "$dst" <<'PY'
import os
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
tmp = f"{dst}.tmp"
src_conn = None
dst_conn = None
try:
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    dst_conn = sqlite3.connect(tmp, timeout=30)
    src_conn.backup(dst_conn)
    result = dst_conn.execute("PRAGMA quick_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite quick_check failed for {src}: {result!r}")
    dst_conn.close()
    dst_conn = None
    src_conn.close()
    src_conn = None
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst)
finally:
    if dst_conn is not None:
        dst_conn.close()
    if src_conn is not None:
        src_conn.close()
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
}

backup_ai_filter() {
    local project="/root/ai-rss-filter"
    local out="${SRC_DIR}/ai-rss-filter"
    local canonical_db="${project}/data/cache.db"
    local canonical_backup="${out}/sqlite/data/cache.db"
    local db_list="${STAGE_DIR}/ai-filter-sqlite.list"
    local db_src rel db_dst db_count=0

    if [[ ! -d "$project" ]]; then
        log "ERROR: /root/ai-rss-filter is absent; canonical database cannot be backed up"
        return 1
    fi
    if [[ ! -f "$canonical_db" || -L "$canonical_db" ]]; then
        log "ERROR: canonical AI-Filter database is missing or unsafe: ${canonical_db}"
        return 1
    fi
    mkdir -p "$out"

    # data is mutable (SQLite WAL, durable queues, release locks). Excluding it
    # prevents a live tar race; databases are captured consistently below.
    tar -C "$project" -czf "${out}/source-config.tar.gz" \
        --exclude='./.git' \
        --exclude='./.venv' \
        --exclude='./.pytest_cache' \
        --exclude='*/__pycache__' \
        --exclude='./htmlcov' \
        --exclude='./data' \
        --exclude='./cache.db' \
        .
    tar -tzf "${out}/source-config.tar.gz" >/dev/null

    if [[ -d "${project}/.git" ]]; then
        git -C "$project" bundle create "${out}/repo.bundle" --all
        git -C "$project" status -sb > "${out}/git-status.txt"
        git -C "$project" log -10 --oneline --decorate > "${out}/git-log.txt"
    fi

    : > "$db_list"
    if [[ -d "${project}/data" ]]; then
        find "${project}/data" -type f \
            \( -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \) \
            -print0 >> "$db_list"
    fi
    if [[ -f "${project}/cache.db" ]]; then
        printf '%s\0' "${project}/cache.db" >> "$db_list"
    fi

    while IFS= read -r -d '' db_src; do
        rel="${db_src#${project}/}"
        db_dst="${out}/sqlite/${rel}"
        backup_sqlite_file "$db_src" "$db_dst"
        db_count=$((db_count + 1))
    done < "$db_list"
    rm -f -- "$db_list"
    if [[ ! -s "$canonical_backup" ]]; then
        log "ERROR: canonical AI-Filter database was not captured: ${canonical_backup}"
        return 1
    fi
    log "AI-Filter backup captured ${db_count} SQLite database(s) consistently"

    snapshot_ai_runtime "$project" "$out"
}

snapshot_ai_runtime() {
    local project=$1 out=$2
    local data_dir="${project}/data"
    local runtime_out="${out}/runtime"
    mkdir -p "$runtime_out"

    if [[ -d "${data_dir}/releases" ]]; then
        # The caller already holds LOCK_SH on release.lock across source,
        # Compose, SQLite, and this release-state snapshot. Do not acquire the
        # same coordination lock again inside the snapshot.
        tar -C "$data_dir" -czf "${runtime_out}/releases.tar.gz" \
            --exclude='releases/*.lock' \
            releases
        tar -tzf "${runtime_out}/releases.tar.gz" >/dev/null
    else
        log "INFO: AI-Filter releases directory is absent; skipping runtime release snapshot"
    fi

    if [[ -d "${data_dir}/webhook_spool" ]]; then
        # Web/API workers take LOCK_EX on this exact file for every durable
        # spool mutation. LOCK_SH produces a point-in-time queue snapshot.
        (
            exec 7>"${data_dir}/webhook_spool/.lock"
            flock -s -w 60 7
            tar -C "$data_dir" -czf "${runtime_out}/webhook_spool.tar.gz" \
                --exclude='webhook_spool/.lock' \
                webhook_spool
        )
        tar -tzf "${runtime_out}/webhook_spool.tar.gz" >/dev/null
    else
        log "INFO: AI-Filter webhook spool is absent; skipping runtime queue snapshot"
    fi
}

backup_npm() {
    local project="/root/npm"
    local db_src="${project}/data/database.sqlite"
    local db_dst="${SRC_DIR}/npm/database.sqlite"

    if [[ ! -d "$project" ]]; then
        log "INFO: /root/npm is absent; skipping"
        return 0
    fi

    # Nginx Proxy Manager writes logs and SQLite WAL state continuously. Keep
    # both out of tar and capture the database through SQLite's backup API.
    tar -C /root -czf "${SRC_DIR}/npm_backup.tar.gz" \
        --exclude='npm/data/logs' \
        --exclude='npm/data/logs/**' \
        --exclude='npm/data/database.sqlite' \
        --exclude='npm/data/database.sqlite-*' \
        npm
    tar -tzf "${SRC_DIR}/npm_backup.tar.gz" >/dev/null

    if [[ -f "$db_src" ]]; then
        backup_sqlite_file "$db_src" "$db_dst"
        log "Nginx Proxy Manager SQLite database captured consistently"
    else
        log "INFO: Nginx Proxy Manager database.sqlite is absent; skipping SQLite snapshot"
    fi
}

backup_service_dirs() {
    local svc
    for svc in miniflux dozzle glance rsshub; do
        if [[ -d "/root/${svc}" ]]; then
            tar -C /root -czf "${SRC_DIR}/${svc}_backup.tar.gz" "$svc"
            tar -tzf "${SRC_DIR}/${svc}_backup.tar.gz" >/dev/null
        else
            log "INFO: /root/${svc} is absent; skipping"
        fi
    done
}

backup_host_task_config() {
    local out="${SRC_DIR}/host-task-config"
    local script
    local -a etc_paths=(crontab cron.d logrotate.d systemd/system)
    mkdir -p "${out}/root-scripts"

    if crontab -l > "${out}/root.crontab" 2>/dev/null; then
        chmod 600 "${out}/root.crontab"
    else
        rm -f -- "${out}/root.crontab"
        log "INFO: root has no readable crontab"
    fi

    if [[ -d /etc/ai-filter ]]; then
        etc_paths+=(ai-filter)
    fi
    tar -C /etc -czf "${out}/etc-task-config.tar.gz" "${etc_paths[@]}"
    tar -tzf "${out}/etc-task-config.tar.gz" >/dev/null

    for script in \
        /root/health_check.sh \
        /root/backup_miniflux.sh \
        /root/backup_system.sh; do
        if [[ -f "$script" ]]; then
            cp -a -- "$script" "${out}/root-scripts/"
        fi
    done
}

run_offsite_upload() {
    local archive=$1 manifest=$2 archive_hash=$3 uploaded_at

    if [[ -z "$UPLOAD_HOOK" ]]; then
        log "WARN: offsite_upload=unconfigured local_only=true"
        if (( UPLOAD_REQUIRED == 1 )); then
            log "ERROR: encrypted offsite upload is required but no hook is configured"
            return 78
        fi
        return 0
    fi
    if [[ "$UPLOAD_HOOK" != /* || ! -f "$UPLOAD_HOOK" || ! -x "$UPLOAD_HOOK" ]]; then
        log "ERROR: encrypted offsite upload hook must be an absolute executable file"
        return 78
    fi
    # Reject hooks not owned by root or writable by group/other.
    hook_uid=$(stat -c '%u' -- "$UPLOAD_HOOK")
    hook_mode=$(stat -c '%a' -- "$UPLOAD_HOOK")
    if [[ "$hook_uid" != "0" ]] || (( (8#$hook_mode & 8#022) != 0 )); then
        log "ERROR: encrypted offsite upload hook ownership or mode is unsafe"
        return 78
    fi

    if ! timeout --signal=TERM --kill-after=30s 30m \
        "$UPLOAD_HOOK" "$archive" "$manifest" >/dev/null 2>&1; then
        log "ERROR: encrypted offsite upload hook failed"
        return 74
    fi

    uploaded_at=$(date +%s)
    OFFSITE_STATE_TMP=$(mktemp "${BACKUP_DIR}/.offsite-upload.XXXXXX")
    printf 'version=1\nuploaded_at=%s\narchive=%s\nsha256=%s\n' \
        "$uploaded_at" "$(basename "$archive")" "$archive_hash" > "$OFFSITE_STATE_TMP"
    chmod 600 "$OFFSITE_STATE_TMP"
    mv -f -- "$OFFSITE_STATE_TMP" "$OFFSITE_STATE"
    OFFSITE_STATE_TMP=""
    chmod 600 "$OFFSITE_STATE"
    log "OK: offsite_upload=verified archive=$(basename "$archive")"
}

# release.sh owns LOCK_EX for its entire deploy/rollback, including container
# startup migrations. Hold one LOCK_SH while the AI-Filter recovery set is
# copied into the private staging directory. Once these staged files are
# complete they are immutable, so compression, hashing, retention, and offsite
# transfer below do not prolong a deployment block.
acquire_ai_release_snapshot_lock
backup_compose_files
backup_ai_filter
release_ai_release_snapshot_lock
backup_npm
backup_service_dirs
backup_host_task_config

tar -C "$STAGE_DIR" -czf "$ARCHIVE_TMP" "$DATE"
tar -tzf "$ARCHIVE_TMP" >/dev/null
archive_size=$(stat -c%s "$ARCHIVE_TMP")
if (( archive_size < 1024 )); then
    log "ERROR: archive is unexpectedly small (${archive_size} bytes)"
    exit 1
fi

archive_hash=$(sha256sum "$ARCHIVE_TMP" | awk '{print $1}')
printf '%s  %s\n' "$archive_hash" "$(basename "$ARCHIVE")" > "$MANIFEST_TMP"
chmod 600 "$ARCHIVE_TMP" "$MANIFEST_TMP"

# Publish the manifest first and the archive last. Consumers discover backups by
# the archive name, so a crash can leave only an ignored manifest rather than a
# visible archive with no integrity proof.
mv -f -- "$MANIFEST_TMP" "$MANIFEST"
MANIFEST_TMP=""
mv -f -- "$ARCHIVE_TMP" "$ARCHIVE"
ARCHIVE_TMP=""
chmod 600 "$ARCHIVE" "$MANIFEST"
(
    cd "$BACKUP_DIR"
    sha256sum --check "$(basename "$MANIFEST")" >/dev/null
)

# Enforce permissions on historical files too, then retain seven days.
find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'system_backup_*.tar.gz' -o -name 'system_backup_*.tar.gz.sha256' \) \
    -exec chmod 600 {} +
find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'system_backup_*.tar.gz' -o -name 'system_backup_*.tar.gz.sha256' \) \
    -mtime +7 -delete

log "OK: verified system backup completed: ${ARCHIVE} (${archive_size} bytes)"
run_offsite_upload "$ARCHIVE" "$MANIFEST" "$archive_hash"
