#!/bin/sh
# Make the shared, redacted D5 ledger writable before Hermes drops to uid 1000.
# The BFF starts as root and may create the file with root ownership; the
# supervisor itself drops to uid 1000 without retaining supplementary groups.
# Give the shared redacted ledger to that runtime uid so D5 candidate writes do
# not depend on group propagation through the upstream Hermes entrypoint.
# The empty file is intentionally created before the drop so a supervisor-first
# startup cannot race a later BFF schema initialization.
set -eu

umask 0007
state_dir=/var/lib/portfolio
state_path=/var/lib/portfolio/langsmith-feedback.sqlite3
mkdir -p "$state_dir"
if [ ! -e "$state_path" ]; then
    : > "$state_path"
fi
chown 1000:1000 "$state_dir" "$state_path" "$state_path-wal" "$state_path-shm" 2>/dev/null || true
chmod 0660 "$state_path" "$state_path-wal" "$state_path-shm" 2>/dev/null || true
chmod 0770 "$state_dir" 2>/dev/null || true

exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
