#!/usr/bin/env bash
# Install the versioned AI-Filter host assets. Nothing is changed unless the
# caller passes --apply. Existing non-AI root crontab entries are preserved.

set -euo pipefail
umask 077

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SYSTEMD_DIR="${OPS_SYSTEMD_DIR:-/etc/systemd/system}"
ROOT_SCRIPT_DIR="${OPS_ROOT_SCRIPT_DIR:-/root}"
LOCAL_SBIN_DIR="${OPS_LOCAL_SBIN_DIR:-/usr/local/sbin}"
CONFIG_DIR="${OPS_CONFIG_DIR:-/etc/ai-filter}"
LOGROTATE_DIR="${OPS_LOGROTATE_DIR:-/etc/logrotate.d}"
CRONTAB_FILE="${OPS_CRONTAB_FILE:-}"
CRONTAB_LOCK="${OPS_CRONTAB_LOCK:-/var/lib/ai-filter-ops-install/crontab.lock}"
SKIP_SYSTEMCTL="${OPS_SKIP_SYSTEMCTL:-0}"
INSTALL_UID="$(id -u)"

usage() {
    printf 'Usage: %s --apply\n' "$0" >&2
}

if [[ "${1:-}" != "--apply" || "$#" -ne 1 ]]; then
    usage
    exit 64
fi
case "$SKIP_SYSTEMCTL" in 0|1) ;; *) exit 64 ;; esac

for command in bash cmp flock install mktemp python3; do
    command -v "$command" >/dev/null 2>&1 || {
        printf 'missing required command: %s\n' "$command" >&2
        exit 69
    }
done
if [[ -z "$CRONTAB_FILE" ]]; then
    command -v crontab >/dev/null 2>&1 || exit 69
fi
if [[ "$SKIP_SYSTEMCTL" == "0" ]]; then
    command -v systemctl >/dev/null 2>&1 || exit 69
fi
if ! canonical_local_sbin=$(python3 - "$LOCAL_SBIN_DIR" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.is_absolute():
    raise SystemExit(1)
try:
    print(path.resolve(strict=False))
except (OSError, RuntimeError):
    raise SystemExit(1)
PY
); then
    printf 'refusing to install: local sbin directory is unsafe\n' >&2
    exit 78
fi
if [[ "$LOCAL_SBIN_DIR" != "$canonical_local_sbin" || \
      -L "$LOCAL_SBIN_DIR" || \
      ( -e "$LOCAL_SBIN_DIR" && ! -d "$LOCAL_SBIN_DIR" ) ]]; then
    printf 'refusing to install: local sbin directory must be a canonical physical path\n' >&2
    exit 78
fi
LOCAL_SBIN_DIR="$canonical_local_sbin"
LIBEXEC_DIR="$(dirname "$LOCAL_SBIN_DIR")/libexec/ai-filter"

# Refuse to install assets that fail syntax or systemd verification.
bash "$SOURCE_ROOT/ops/verify.sh" --source-only

install_if_changed() {
    local source=$1 target=$2 mode=$3 temporary
    mkdir -p "$(dirname "$target")"
    if [[ -f "$target" ]] && cmp -s -- "$source" "$target"; then
        chmod "$mode" "$target"
        if (( INSTALL_UID == 0 )); then chown 0:0 "$target"; fi
        return 0
    fi
    temporary=$(mktemp "$(dirname "$target")/.ai-filter-install.XXXXXX")
    install -m "$mode" -- "$source" "$temporary"
    mv -f -- "$temporary" "$target"
    chmod "$mode" "$target"
    if (( INSTALL_UID == 0 )); then chown 0:0 "$target"; fi
}

restore_drill_bundle_digest() {
    python3 - \
        "$SOURCE_ROOT/ops/backup/restore_drill.py" \
        "$SOURCE_ROOT/db/event_core.py" <<'PY'
import hashlib
from pathlib import Path
import sys

digest = hashlib.sha256()
for name in sys.argv[1:]:
    data = Path(name).read_bytes()
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)
print(digest.hexdigest())
PY
}

install_restore_drill_bundle() {
    local bundle_digest bundle_name bundle_dir selector_source rc
    if [[ "$LIBEXEC_DIR" != /* || -L "$LIBEXEC_DIR" ]]; then
        printf 'refusing to install: restore drill libexec path is unsafe\n' >&2
        return 78
    fi
    if [[ -L "$LOCAL_SBIN_DIR/ai-filter-restore-drill" || \
          ( -e "$LOCAL_SBIN_DIR/ai-filter-restore-drill" && \
            ! -f "$LOCAL_SBIN_DIR/ai-filter-restore-drill" ) ]]; then
        printf 'refusing to install: restore drill launcher path is unsafe\n' >&2
        return 78
    fi
    mkdir -p "$LIBEXEC_DIR"
    chmod 0700 "$LIBEXEC_DIR"
    if (( INSTALL_UID == 0 )); then chown 0:0 "$LIBEXEC_DIR"; fi

    bundle_digest=$(restore_drill_bundle_digest)
    if [[ ! "$bundle_digest" =~ ^[0-9a-f]{64}$ ]]; then
        printf 'refusing to install: restore drill bundle digest is invalid\n' >&2
        return 78
    fi
    bundle_name="restore-drill-$bundle_digest"
    bundle_dir="$LIBEXEC_DIR/$bundle_name"
    if [[ -L "$bundle_dir" || ( -e "$bundle_dir" && ! -d "$bundle_dir" ) ]]; then
        printf 'refusing to install: restore drill bundle path is unsafe\n' >&2
        return 78
    fi
    mkdir -p "$bundle_dir"
    chmod 0700 "$bundle_dir"
    if (( INSTALL_UID == 0 )); then chown 0:0 "$bundle_dir"; fi
    if [[ -L "$bundle_dir/restore_drill.py" || \
          ( -e "$bundle_dir/restore_drill.py" && ! -f "$bundle_dir/restore_drill.py" ) || \
          -L "$bundle_dir/event_core.py" || \
          ( -e "$bundle_dir/event_core.py" && ! -f "$bundle_dir/event_core.py" ) ]]; then
        printf 'refusing to install: restore drill bundle member is unsafe\n' >&2
        return 78
    fi
    install_if_changed \
        "$SOURCE_ROOT/ops/backup/restore_drill.py" \
        "$bundle_dir/restore_drill.py" 0600
    install_if_changed \
        "$SOURCE_ROOT/db/event_core.py" \
        "$bundle_dir/event_core.py" 0600
    if ! python3 -I "$bundle_dir/restore_drill.py" --help >/dev/null; then
        printf 'refusing to install: restore drill bundle smoke test failed\n' >&2
        return 78
    fi

    selector_source=$(mktemp "$LIBEXEC_DIR/.restore-drill-selector.XXXXXX")
    printf '%s\n' "$bundle_name" > "$selector_source"
    if [[ -L "$LIBEXEC_DIR/restore-drill-current" || \
          ( -e "$LIBEXEC_DIR/restore-drill-current" && \
            ! -f "$LIBEXEC_DIR/restore-drill-current" ) ]]; then
        rm -f -- "$selector_source"
        printf 'refusing to install: restore drill selector is unsafe\n' >&2
        return 78
    fi
    if install_if_changed \
        "$selector_source" "$LIBEXEC_DIR/restore-drill-current" 0600; then
        :
    else
        rc=$?
        rm -f -- "$selector_source"
        return "$rc"
    fi
    rm -f -- "$selector_source"
    install_if_changed \
        "$SOURCE_ROOT/ops/backup/ai-filter-restore-drill" \
        "$LOCAL_SBIN_DIR/ai-filter-restore-drill" 0700
}

read_crontab() {
    local destination=$1 rc
    if [[ -n "$CRONTAB_FILE" ]]; then
        if [[ ! -f "$CRONTAB_FILE" || ! -r "$CRONTAB_FILE" ]]; then
            printf 'refusing to install: configured crontab file is not a readable regular file\n' >&2
            return 78
        fi
        if ! cat -- "$CRONTAB_FILE" > "$destination"; then
            printf 'refusing to install: configured crontab file could not be read\n' >&2
            return 78
        fi
    else
        # `crontab -l` uses a non-zero status both for an empty spool and for
        # real read/permission failures on common cron implementations. Never
        # guess that either case means an empty crontab: an explicit operator
        # migration is safer than replacing jobs we could not inspect.
        if crontab -l > "$destination" 2>/dev/null; then
            :
        else
            rc=$?
            printf 'refusing to install: existing root crontab could not be read (exit %s)\n' "$rc" >&2
            return 78
        fi
    fi
}

write_crontab() {
    local source=$1 temporary
    if [[ -n "$CRONTAB_FILE" ]]; then
        mkdir -p "$(dirname "$CRONTAB_FILE")"
        temporary=$(mktemp "$(dirname "$CRONTAB_FILE")/.ai-filter-crontab.XXXXXX")
        install -m 0600 -- "$source" "$temporary"
        mv -f -- "$temporary" "$CRONTAB_FILE"
    else
        crontab "$source"
    fi
}

secure_operator_file() {
    local target=$1 mode=$2
    chmod "$mode" "$target"
    if (( INSTALL_UID == 0 )); then chown 0:0 "$target"; fi
}

# Validate and build the desired crontab before changing any host asset. A
# malformed marker must never make the installer consume all following user
# jobs. Only byte-for-byte legacy lines from the pre-versioned production
# schedule are migrated; customized unmanaged commands fail closed.
current=$(mktemp)
observed=$(mktemp)
stripped=$(mktemp)
desired=$(mktemp)
trap 'rm -f -- "$current" "$observed" "$stripped" "$desired"' EXIT

# Serialize this installer with itself. A final compare-and-swap read below
# additionally catches edits made by tools that do not honor this lock. Keep
# the default lock below a root-only directory, and reject symlinks before the
# shell opens/truncates anything as root.
if [[ "$CRONTAB_LOCK" != /* ]]; then
    printf 'refusing to install: crontab lock path must be absolute\n' >&2
    exit 78
fi
crontab_lock_dir=$(dirname "$CRONTAB_LOCK")
if [[ -L "$crontab_lock_dir" ]]; then
    printf 'refusing to install: crontab lock directory must not be a symlink\n' >&2
    exit 78
fi
mkdir -p "$crontab_lock_dir"
chmod 0700 "$crontab_lock_dir"
if (( INSTALL_UID == 0 )); then chown 0:0 "$crontab_lock_dir"; fi
if [[ -L "$CRONTAB_LOCK" || ( -e "$CRONTAB_LOCK" && ! -f "$CRONTAB_LOCK" ) ]]; then
    printf 'refusing to install: crontab lock must be a regular file\n' >&2
    exit 78
fi
exec 8>>"$CRONTAB_LOCK"
chmod 0600 "$CRONTAB_LOCK"
if (( INSTALL_UID == 0 )); then chown 0:0 "$CRONTAB_LOCK"; fi
if ! flock -n 8; then
    printf 'refusing to install: another crontab installation is in progress\n' >&2
    exit 75
fi
read_crontab "$current"
if ! python3 - "$current" "$stripped" <<'PY'
from pathlib import Path
import sys

source, destination = map(Path, sys.argv[1:])
begin = "# BEGIN AI_FILTER_MANAGED"
end = "# END AI_FILTER_MANAGED"
legacy = {
    "30 3 * * * /root/backup_system.sh",
    "30 3 * * * timeout --signal=TERM --kill-after=60s 45m /root/backup_system.sh",
    "* * * * * /root/health_check.sh",
    "*/30 8-16 * * 1-5 /root/ai-rss-filter/scripts/cron_refresh.sh",
    "*/30 8-16 * * 1-5 /root/ai-rss-filter/scripts/cron_refresh.sh >> /var/log/ai-filter-refresh.log 2>&1",
    "5 0,2,4,6,18,20,22 * * 1-5 /root/ai-rss-filter/scripts/cron_refresh.sh >> /var/log/ai-filter-refresh.log 2>&1",
    "5 */2 * * 0,6 /root/ai-rss-filter/scripts/cron_refresh.sh >> /var/log/ai-filter-refresh.log 2>&1",
}
legacy_comments = {
    "# Trading session: fresh intelligence every 30 minutes on weekdays.",
    "# Weekday off-hours and weekends: retain coverage without 48 full sweeps/day.",
}
managed_paths = (
    "/root/backup_system.sh",
    "/root/health_check.sh",
    "/root/ai-rss-filter/scripts/cron_refresh.sh",
    "/usr/local/sbin/ai-filter-ops-check",
    "/usr/local/sbin/ai-filter-restore-drill",
    "/root/ai-rss-filter/scripts/cron_event_enrichment.sh",
)

output: list[str] = []
inside = False
blocks = 0
for line in source.read_text(encoding="utf-8").splitlines():
    if line == begin:
        if inside or blocks:
            raise SystemExit("invalid or duplicate AI-Filter cron begin marker")
        inside = True
        blocks += 1
        continue
    if line == end:
        if not inside:
            raise SystemExit("AI-Filter cron end marker has no matching begin")
        inside = False
        continue
    if inside:
        continue
    if line in legacy or line in legacy_comments:
        continue
    if any(path in line for path in managed_paths):
        raise SystemExit("custom unmanaged AI-Filter cron entry requires manual migration")
    output.append(line)
if inside:
    raise SystemExit("AI-Filter cron begin marker has no matching end")
while output and not output[-1]:
    output.pop()
destination.write_text("\n".join(output) + ("\n" if output else ""), encoding="utf-8")
PY
then
    printf 'refusing to modify an unsafe or customized root crontab\n' >&2
    exit 78
fi

cat "$stripped" > "$desired"
if [[ -s "$desired" ]]; then printf '\n' >> "$desired"; fi
cat "$SOURCE_ROOT/ops/cron/ai-filter.crontab" >> "$desired"

install_if_changed \
    "$SOURCE_ROOT/ops/systemd/ai-filter-market-close.service" \
    "$SYSTEMD_DIR/ai-filter-market-close.service" 0644
install_if_changed \
    "$SOURCE_ROOT/ops/systemd/ai-filter-market-close.timer" \
    "$SYSTEMD_DIR/ai-filter-market-close.timer" 0644
install_if_changed \
    "$SOURCE_ROOT/ops/systemd/ai-filter-market-close-repair.service" \
    "$SYSTEMD_DIR/ai-filter-market-close-repair.service" 0644
install_if_changed \
    "$SOURCE_ROOT/ops/systemd/ai-filter-market-close-repair.timer" \
    "$SYSTEMD_DIR/ai-filter-market-close-repair.timer" 0644
install_if_changed \
    "$SOURCE_ROOT/ops/systemd/ai-filter-market-close-history.service" \
    "$SYSTEMD_DIR/ai-filter-market-close-history.service" 0644
install_if_changed \
    "$SOURCE_ROOT/ops/systemd/ai-filter-market-close-history.timer" \
    "$SYSTEMD_DIR/ai-filter-market-close-history.timer" 0644
install_if_changed \
    "$SOURCE_ROOT/ops/backup/backup_system.sh" \
    "$ROOT_SCRIPT_DIR/backup_system.sh" 0700
install_restore_drill_bundle
install_if_changed \
    "$SOURCE_ROOT/deploy/health_check.sh" \
    "$ROOT_SCRIPT_DIR/health_check.sh" 0700
install_if_changed \
    "$SOURCE_ROOT/ops/ai_filter_ops_check.py" \
    "$LOCAL_SBIN_DIR/ai-filter-ops-check" 0700
install_if_changed \
    "$SOURCE_ROOT/ops/logrotate/ai-filter" \
    "$LOGROTATE_DIR/ai-filter" 0644

mkdir -p "$CONFIG_DIR"
chmod 0700 "$CONFIG_DIR"
if (( INSTALL_UID == 0 )); then chown 0:0 "$CONFIG_DIR"; fi
if [[ ! -e "$CONFIG_DIR/ops-monitor.env" ]]; then
    install -m 0600 -- "$SOURCE_ROOT/ops/ops-monitor.env.example" \
        "$CONFIG_DIR/ops-monitor.env"
fi
secure_operator_file "$CONFIG_DIR/ops-monitor.env" 0600
if [[ ! -e "$CONFIG_DIR/backup.env" ]]; then
    install -m 0600 -- "$SOURCE_ROOT/ops/backup.env.example" \
        "$CONFIG_DIR/backup.env"
fi
secure_operator_file "$CONFIG_DIR/backup.env" 0600

if [[ "$SKIP_SYSTEMCTL" == "0" ]]; then
    systemctl daemon-reload
    systemctl enable --now ai-filter-market-close.timer
    systemctl enable --now ai-filter-market-close-repair.timer
    systemctl enable --now ai-filter-market-close-history.timer
fi

# Do not overwrite a root crontab that changed after the validated snapshot was
# read. Keep this check immediately adjacent to the write to minimize the
# residual race with non-cooperating external editors.
read_crontab "$observed"
if ! cmp -s -- "$current" "$observed"; then
    printf 'refusing to install: root crontab changed concurrently\n' >&2
    exit 75
fi

if ! cmp -s -- "$current" "$desired"; then
    install_if_changed "$current" "$CONFIG_DIR/root.crontab.previous" 0600
    write_crontab "$desired"
fi

printf 'AI-Filter host assets installed. Run ops/verify.sh to audit drift.\n'
