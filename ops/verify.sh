#!/usr/bin/env bash
# Read-only verification for source syntax and installed host-asset drift.

set -uo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SYSTEMD_DIR="${OPS_SYSTEMD_DIR:-/etc/systemd/system}"
ROOT_SCRIPT_DIR="${OPS_ROOT_SCRIPT_DIR:-/root}"
LOCAL_SBIN_DIR="${OPS_LOCAL_SBIN_DIR:-/usr/local/sbin}"
LOGROTATE_DIR="${OPS_LOGROTATE_DIR:-/etc/logrotate.d}"
CRONTAB_FILE="${OPS_CRONTAB_FILE:-}"
SKIP_SYSTEMD_ANALYZE="${OPS_SKIP_SYSTEMD_ANALYZE:-0}"
SOURCE_ONLY=0
BEGIN_MARKER="# BEGIN AI_FILTER_MANAGED"
END_MARKER="# END AI_FILTER_MANAGED"
status=0
EXPECTED_UID="${OPS_EXPECTED_UID:-$(id -u)}"
EXPECTED_GID="${OPS_EXPECTED_GID:-$(id -g)}"

if [[ "${1:-}" == "--source-only" && "$#" -eq 1 ]]; then
    SOURCE_ONLY=1
elif [[ "$#" -ne 0 ]]; then
    printf 'Usage: %s [--source-only]\n' "$0" >&2
    exit 64
fi
case "$SKIP_SYSTEMD_ANALYZE" in 0|1) ;; *) exit 64 ;; esac

ok() { printf 'OK %s\n' "$1"; }
fail() { printf 'DRIFT %s\n' "$1" >&2; status=1; }

compare_asset() {
    local source=$1 target=$2 label=$3
    if [[ -f "$target" ]] && cmp -s -- "$source" "$target"; then
        ok "$label"
    else
        fail "$label"
    fi
}

verify_mode_owner() {
    local target=$1 expected_mode=$2 label=$3 actual_mode actual_uid actual_gid
    if [[ ! -e "$target" ]]; then
        fail "$label"
        return
    fi
    if stat --version >/dev/null 2>&1; then
        actual_mode=$(stat -c '%a' -- "$target" 2>/dev/null) || {
            fail "$label"
            return
        }
        actual_uid=$(stat -c '%u' -- "$target" 2>/dev/null) || {
            fail "$label"
            return
        }
        actual_gid=$(stat -c '%g' -- "$target" 2>/dev/null) || {
            fail "$label"
            return
        }
    else
        actual_mode=$(stat -f '%Lp' "$target" 2>/dev/null) || {
            fail "$label"
            return
        }
        actual_uid=$(stat -f '%u' "$target" 2>/dev/null) || {
            fail "$label"
            return
        }
        actual_gid=$(stat -f '%g' "$target" 2>/dev/null) || {
            fail "$label"
            return
        }
    fi
    if [[ "$actual_mode" == "$expected_mode" && "$actual_uid" == "$EXPECTED_UID" && "$actual_gid" == "$EXPECTED_GID" ]]; then
        ok "$label"
    else
        fail "$label"
    fi
}

for script in \
    "$SOURCE_ROOT/ops/install.sh" \
    "$SOURCE_ROOT/ops/verify.sh" \
    "$SOURCE_ROOT/ops/backup/backup_system.sh" \
    "$SOURCE_ROOT/ops/backup/ai-filter-restore-drill" \
    "$SOURCE_ROOT/ops/backup/offsite_upload_hook.example.sh" \
    "$SOURCE_ROOT/deploy/health_check.sh"; do
    if bash -n "$script"; then ok "syntax:${script#${SOURCE_ROOT}/}"; else fail "syntax:${script#${SOURCE_ROOT}/}"; fi
done
if python3 - "$SOURCE_ROOT/ops/ai_filter_ops_check.py" <<'PY'
import pathlib
import sys
source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
compile(source, sys.argv[1], "exec")
PY
then
    ok "syntax:ops/ai_filter_ops_check.py"
else
    fail "syntax:ops/ai_filter_ops_check.py"
fi

restore_bundle_digest=$(python3 - \
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
)
restore_bundle_name="restore-drill-$restore_bundle_digest"
if python3 - "$SOURCE_ROOT/ops/backup/restore_drill.py" <<'PY'
import pathlib
import sys
source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
compile(source, sys.argv[1], "exec")
PY
then
    ok "syntax:ops/backup/restore_drill.py"
else
    fail "syntax:ops/backup/restore_drill.py"
fi
if python3 -I "$SOURCE_ROOT/ops/backup/restore_drill.py" --help >/dev/null 2>&1; then
    ok "runtime:ops/backup/restore_drill.py"
else
    fail "runtime:ops/backup/restore_drill.py"
fi

if [[ "$SKIP_SYSTEMD_ANALYZE" == "0" ]]; then
    if command -v systemd-analyze >/dev/null 2>&1 && \
       systemd-analyze verify \
           "$SOURCE_ROOT/ops/systemd/ai-filter-market-close.service" \
           "$SOURCE_ROOT/ops/systemd/ai-filter-market-close.timer" \
           "$SOURCE_ROOT/ops/systemd/ai-filter-market-close-repair.service" \
           "$SOURCE_ROOT/ops/systemd/ai-filter-market-close-repair.timer" \
           "$SOURCE_ROOT/ops/systemd/ai-filter-market-close-history.service" \
           "$SOURCE_ROOT/ops/systemd/ai-filter-market-close-history.timer" >/dev/null; then
        ok "systemd-analyze"
    else
        fail "systemd-analyze"
    fi
fi

if (( SOURCE_ONLY == 1 )); then
    exit "$status"
fi

canonical_local_sbin=$(python3 - "$LOCAL_SBIN_DIR" <<'PY'
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
) || canonical_local_sbin=""
if [[ -z "$canonical_local_sbin" || \
      "$LOCAL_SBIN_DIR" != "$canonical_local_sbin" || \
      -L "$LOCAL_SBIN_DIR" || \
      ( -e "$LOCAL_SBIN_DIR" && ! -d "$LOCAL_SBIN_DIR" ) ]]; then
    fail "path:local-sbin"
fi
if [[ -n "$canonical_local_sbin" ]]; then
    LOCAL_SBIN_DIR="$canonical_local_sbin"
fi
LIBEXEC_DIR="$(dirname "$LOCAL_SBIN_DIR")/libexec/ai-filter"
restore_bundle_dir="$LIBEXEC_DIR/$restore_bundle_name"

compare_asset "$SOURCE_ROOT/ops/systemd/ai-filter-market-close.service" \
    "$SYSTEMD_DIR/ai-filter-market-close.service" "systemd:service"
compare_asset "$SOURCE_ROOT/ops/systemd/ai-filter-market-close.timer" \
    "$SYSTEMD_DIR/ai-filter-market-close.timer" "systemd:timer"
compare_asset "$SOURCE_ROOT/ops/systemd/ai-filter-market-close-repair.service" \
    "$SYSTEMD_DIR/ai-filter-market-close-repair.service" "systemd:repair-service"
compare_asset "$SOURCE_ROOT/ops/systemd/ai-filter-market-close-repair.timer" \
    "$SYSTEMD_DIR/ai-filter-market-close-repair.timer" "systemd:repair-timer"
compare_asset "$SOURCE_ROOT/ops/systemd/ai-filter-market-close-history.service" \
    "$SYSTEMD_DIR/ai-filter-market-close-history.service" "systemd:history-service"
compare_asset "$SOURCE_ROOT/ops/systemd/ai-filter-market-close-history.timer" \
    "$SYSTEMD_DIR/ai-filter-market-close-history.timer" "systemd:history-timer"
compare_asset "$SOURCE_ROOT/ops/backup/backup_system.sh" \
    "$ROOT_SCRIPT_DIR/backup_system.sh" "root-script:backup"
if [[ ! -L "$LOCAL_SBIN_DIR/ai-filter-restore-drill" ]]; then
    compare_asset "$SOURCE_ROOT/ops/backup/ai-filter-restore-drill" \
        "$LOCAL_SBIN_DIR/ai-filter-restore-drill" "local-sbin:restore-drill-launcher"
else
    fail "local-sbin:restore-drill-launcher"
fi
if [[ ! -L "$LIBEXEC_DIR/restore-drill-current" ]] && \
   [[ -f "$LIBEXEC_DIR/restore-drill-current" ]] && \
   [[ "$(wc -l < "$LIBEXEC_DIR/restore-drill-current")" -eq 1 ]] && \
   [[ "$(sed -n '1p' "$LIBEXEC_DIR/restore-drill-current")" == "$restore_bundle_name" ]]; then
    ok "libexec:restore-drill-selector"
else
    fail "libexec:restore-drill-selector"
fi
compare_asset "$SOURCE_ROOT/ops/backup/restore_drill.py" \
    "$restore_bundle_dir/restore_drill.py" "libexec:restore-drill"
compare_asset "$SOURCE_ROOT/db/event_core.py" \
    "$restore_bundle_dir/event_core.py" "libexec:event-core-verifier"
compare_asset "$SOURCE_ROOT/deploy/health_check.sh" \
    "$ROOT_SCRIPT_DIR/health_check.sh" "root-script:health"
compare_asset "$SOURCE_ROOT/ops/ai_filter_ops_check.py" \
    "$LOCAL_SBIN_DIR/ai-filter-ops-check" "local-sbin:ops-check"
compare_asset "$SOURCE_ROOT/ops/logrotate/ai-filter" \
    "$LOGROTATE_DIR/ai-filter" "logrotate:ai-filter"
verify_mode_owner "$SYSTEMD_DIR/ai-filter-market-close.service" 644 "mode:systemd-service"
verify_mode_owner "$SYSTEMD_DIR/ai-filter-market-close.timer" 644 "mode:systemd-timer"
verify_mode_owner "$SYSTEMD_DIR/ai-filter-market-close-repair.service" 644 "mode:systemd-repair-service"
verify_mode_owner "$SYSTEMD_DIR/ai-filter-market-close-repair.timer" 644 "mode:systemd-repair-timer"
verify_mode_owner "$SYSTEMD_DIR/ai-filter-market-close-history.service" 644 "mode:systemd-history-service"
verify_mode_owner "$SYSTEMD_DIR/ai-filter-market-close-history.timer" 644 "mode:systemd-history-timer"
verify_mode_owner "$ROOT_SCRIPT_DIR/backup_system.sh" 700 "mode:backup-script"
verify_mode_owner "$LOCAL_SBIN_DIR/ai-filter-restore-drill" 700 "mode:restore-drill"
verify_mode_owner "$LIBEXEC_DIR" 700 "mode:restore-drill-libexec"
verify_mode_owner "$restore_bundle_dir" 700 "mode:restore-drill-bundle"
verify_mode_owner "$LIBEXEC_DIR/restore-drill-current" 600 "mode:restore-drill-selector"
verify_mode_owner "$restore_bundle_dir/restore_drill.py" 600 "mode:restore-drill-libexec-script"
verify_mode_owner "$restore_bundle_dir/event_core.py" 600 "mode:event-core-verifier"
if [[ ! -L "$LOCAL_SBIN_DIR/ai-filter-restore-drill" ]] && \
   "$LOCAL_SBIN_DIR/ai-filter-restore-drill" --help >/dev/null 2>&1; then
    ok "runtime:restore-drill-installed-layout"
else
    fail "runtime:restore-drill-installed-layout"
fi
verify_mode_owner "$ROOT_SCRIPT_DIR/health_check.sh" 700 "mode:health-script"
verify_mode_owner "$LOCAL_SBIN_DIR/ai-filter-ops-check" 700 "mode:ops-check"
verify_mode_owner "$LOGROTATE_DIR/ai-filter" 644 "mode:logrotate"
verify_mode_owner "${OPS_CONFIG_DIR:-/etc/ai-filter}/ops-monitor.env" 600 "mode:ops-config"
verify_mode_owner "${OPS_CONFIG_DIR:-/etc/ai-filter}/backup.env" 600 "mode:backup-config"

actual_cron=$(mktemp)
actual_block=$(mktemp)
trap 'rm -f -- "$actual_cron" "$actual_block"' EXIT
if [[ -n "$CRONTAB_FILE" ]]; then
    [[ -r "$CRONTAB_FILE" ]] && cat -- "$CRONTAB_FILE" > "$actual_cron"
else
    crontab -l > "$actual_cron" 2>/dev/null || true
fi
awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
    $0 == begin { capture=1 }
    capture { print }
    $0 == end { capture=0; found=1 }
    END { if (!found) exit 1 }
' "$actual_cron" > "$actual_block" || true
if cmp -s -- "$SOURCE_ROOT/ops/cron/ai-filter.crontab" "$actual_block"; then
    ok "crontab:managed-block"
else
    fail "crontab:managed-block"
fi

exit "$status"
