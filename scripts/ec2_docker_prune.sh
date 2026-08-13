#!/usr/bin/env bash
# Reclaim EBS disk space from stopped containers, unused networks, and
# dangling images. Never touches volumes (`-a`/`--volumes` intentionally
# omitted) since those hold TimescaleDB/Postgres data.
#
# Cron (daily at 03:00, root crontab):
#   0 3 * * * /path/to/repo/scripts/ec2_docker_prune.sh >> /var/log/docker-prune.log 2>&1
set -euo pipefail

date
docker system prune -f
