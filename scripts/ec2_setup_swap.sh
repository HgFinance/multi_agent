#!/usr/bin/env bash
# One-time swap setup for the AWS EC2 (Ubuntu 24.04) Docker host, to absorb
# memory spikes across many Hermes/worker containers without OOM-killing them.
# Idempotent: safe to re-run, skips if swap already active.
set -euo pipefail

swap_size_gb="${SWAP_SIZE_GB:-4}"
swapfile="${SWAPFILE:-/swapfile}"

if swapon --show | grep -q .; then
  echo "swap already active, skipping:"
  swapon --show
  exit 0
fi

fallocate -l "${swap_size_gb}G" "$swapfile" || dd if=/dev/zero of="$swapfile" bs=1M count=$((swap_size_gb * 1024))
chmod 600 "$swapfile"
mkswap "$swapfile"
swapon "$swapfile"

if ! grep -qF "$swapfile" /etc/fstab; then
  echo "$swapfile none swap sw 0 0" >> /etc/fstab
fi

swapon --show
free -h
