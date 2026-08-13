#!/usr/bin/env bash
# Reclaim EBS disk space from stopped containers, unused networks, and
# dangling images. Never touches volumes (`-a`/`--volumes` intentionally
# omitted) since those hold TimescaleDB/Postgres data.
#
# Cron (daily at 03:00 KST; EC2 host clock is UTC, so schedule at 18:00 UTC):
#   0 18 * * * /home/ubuntu/hgfinance/scripts/ec2_docker_prune.sh >> /home/ubuntu/docker-prune.log 2>&1
set -euo pipefail

date
docker system prune -f
