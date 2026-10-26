#!/usr/bin/env bash
# Check every tracked shell file, not just the first argument to bash -n.
set -euo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$ROOT"
files="$(mktemp "${TMPDIR:-/tmp}/ai-filter-shell-check.XXXXXX")"
trap 'rm -f -- "$files"' EXIT

# Materialize the list so a failed git invocation cannot silently pass through
# a process substitution. NUL delimiting also preserves spaces in filenames.
git ls-files -z -- '*.sh' > "$files"
count=0
while IFS= read -r -d '' script; do
    printf 'Checking shell syntax: %s\n' "$script"
    bash -n -- "$script"
    count=$((count + 1))
done < "$files"
if (( count == 0 )); then
    printf 'ERROR: no tracked shell scripts found\n' >&2
    exit 1
fi
printf 'Shell syntax OK: %s tracked file(s)\n' "$count"
