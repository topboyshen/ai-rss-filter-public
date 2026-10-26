#!/usr/bin/env bash
# Interface example only; it intentionally has no remote destination and never
# reports success. Copy it outside the repository, implement authenticated
# client-side encryption + remote verification, then configure
# SYSTEM_BACKUP_UPLOAD_HOOK with that absolute root-owned executable path.

set -euo pipefail
umask 077

if [[ "$#" -ne 2 ]]; then
    printf 'usage: encrypted-upload-hook ARCHIVE SHA256_MANIFEST\n' >&2
    exit 64
fi
archive=$1
manifest=$2
[[ -r "$archive" && -r "$manifest" ]] || exit 66

printf 'No encrypted offsite destination is configured.\n' >&2
exit 78
