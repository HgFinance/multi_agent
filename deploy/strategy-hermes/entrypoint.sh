#!/bin/sh
set -eu

# The named lab volume is owned exclusively by Strategy Hermes. Bootstrap it
# before the upstream Hermes entrypoint drops from root to the hermes user.
lab_root="${AUTONOMOUS_RESEARCH_LAB_ROOT:-/var/lib/autonomous-research}"
runtime_home="${HERMES_HOME:-/opt/data}"
mkdir -p "$lab_root/intake" "$lab_root/labs" "$lab_root/errors"
# BFF runs as root while Strategy Hermes runs as uid 1000. Intake is a narrow
# file-backed IPC directory: both sides must be able to consume a manifest.
# Lab/state files are chowned below and remain private to Hermes.
chmod 0777 "$lab_root/intake"
target_uid="${HERMES_UID:-$(id -u hermes)}"
target_gid="${HERMES_GID:-$(id -g hermes)}"
chown -R "$target_uid:$target_gid" "$lab_root"

# Seed only the Codex credential into the dedicated Strategy Hermes home. The
# source mount is read-only and remains owned by the existing profile; the
# strategy runtime gets its own mutable auth/session state.
mkdir -p "$runtime_home"
if [ ! -e "$runtime_home/auth.json" ] && [ -r /opt/strategy-credentials/auth.json ]; then
    cp /opt/strategy-credentials/auth.json "$runtime_home/auth.json"
    chown "$target_uid:$target_gid" "$runtime_home/auth.json"
    chmod 600 "$runtime_home/auth.json"
fi

exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
