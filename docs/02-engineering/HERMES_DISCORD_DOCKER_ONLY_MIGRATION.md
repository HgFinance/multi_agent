# Hermes Discord Docker-only migration

AWS에서 Discord Gateway 소유권을 Host systemd에서 Docker Compose로 단계적으로 넘긴다.
저장소는 Host systemd unit을 생성·삭제·시작하지 않는다. 각 단계의 결과를 확인한
뒤 다음 단계로 진행한다.

## 목표와 불변식

| Profile | Docker service | Host unit |
|---|---|---|
| ceo-agent | ceo-hermes | hermes-gateway-ceo-agent |
| hr-department | workforce-hermes | hermes-gateway-hr-department |
| research-department | research-hermes | hermes-gateway-research-department |
| quant-backtest-department | quant-hermes | hermes-gateway-quant-backtest-department |
| accounting-portfolio-department | accounting-hermes | hermes-gateway-accounting-portfolio-department |
| trading-department | trading-hermes | hermes-gateway-trading-department |
| risk-management | risk-hermes | hermes-gateway-risk-management |
| qa-department | qa-hermes | hermes-gateway-qa-department |

기본 Compose의 8개 Gateway는 모두 upstream `nousresearch/hermes-agent:latest`
image-only 서비스다. 모두 `gateway run`, `HERMES_HOME=/opt/data`, canonical
profile mount, shared-Kanban mount, `restart: unless-stopped`를 사용한다.

`kanban-dispatcher`와 `ceo-kanban-supervisor`는 Gateway가 아니며 기존
standalone dispatcher/supervisor command와 공식 image를 유지한다.
department Gateway의 `HERMES_KANBAN_DISPATCH_IN_GATEWAY`는 `false`다.

`Dockerfile.hermes-discord`, `deploy/hermes-discord/gateway_patch.py`,
`orchestration/discord_idempotency.py`는 삭제하지 않는다. 기본 production
path에서는 활성화하지 않고, single-owner upstream 경로에서도 duplicate가
재현될 때만 `docker-compose.discord-idempotency.yml` override로 선택한다.

Discord permission, mention requirement, history backfill, profile data, auth,
Kanban DB/schema는 변경하지 않는다.

## 공통 변수

```bash
cd ~/hgfinance
GATEWAY_CONTAINERS=(hedgefund-ceo-hermes hedgefund-workforce-hermes hedgefund-research-hermes hedgefund-quant-hermes hedgefund-accounting-hermes hedgefund-trading-hermes hedgefund-risk-hermes hedgefund-qa-hermes)
HOST_UNITS=(hermes-gateway-ceo-agent hermes-gateway-hr-department hermes-gateway-research-department hermes-gateway-quant-backtest-department hermes-gateway-accounting-portfolio-department hermes-gateway-trading-department hermes-gateway-risk-management hermes-gateway-qa-department)
```

## A. PRECHECK

```bash
docker info >/dev/null
systemctl is-enabled docker
docker compose config --quiet
docker compose ps
systemctl --user list-unit-files 'hermes-gateway-*' --no-legend || true
systemctl --user --type=service --state=running --no-legend | grep -E 'hermes-gateway-' || true
```

Expected: Docker daemon enabled, Compose config valid, current Docker/Host state
recorded. 이 단계에서는 Host unit을 stop/disable하지 않는다.

실패하면 중단하고 Host Gateway를 변경하지 않는다.

## B. BACKUP

```bash
BACKUP_DIR="$HOME/hgfinance-docker-gateway-backup-$(date +%Y%m%d%H%M%S)"
mkdir -p "$BACKUP_DIR"
docker compose config > "$BACKUP_DIR/compose.rendered.yaml"
for unit in "${HOST_UNITS[@]}"; do
  systemctl --user cat "$unit" > "$BACKUP_DIR/$unit.service.txt" 2>&1 || true
done
```

`profile`, `auth.json`, `.env`, shared Kanban DB는 이동·삭제하지 않는다.

## C. PULL / IMAGE WIRING

기본 production path는 image-only이므로 build가 아니라 pull을 사용한다.

```bash
docker compose pull ceo-hermes workforce-hermes research-hermes quant-hermes accounting-hermes trading-hermes risk-hermes qa-hermes
docker image inspect nousresearch/hermes-agent:latest >/dev/null
```

기본 경로에서는 `docker compose build`를 실행하지 않는다. 실패 시 Host
Gateway는 그대로 두고 image pull 원인을 해결한다.

Idempotency override가 필요한 경우에만 다음을 실행한다. 이후 모든 Compose
명령에 동일한 두 `-f` 옵션을 유지한다.

```bash
docker compose -f docker-compose.yml -f docker-compose.discord-idempotency.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.discord-idempotency.yml build ceo-hermes workforce-hermes research-hermes quant-hermes accounting-hermes trading-hermes risk-hermes qa-hermes
docker image inspect hgfinance/hermes-discord:discord-idempotency-v1 >/dev/null
```

## D. CEO Host Gateway stop

CEO 하나만 먼저 stop한다. 아직 disable하지 않는다.

```bash
systemctl --user stop hermes-gateway-ceo-agent
if systemctl --user is-active --quiet hermes-gateway-ceo-agent; then
  echo 'Host CEO gateway is still active'
  exit 1
fi
```

실패 시:

```bash
systemctl --user enable --now hermes-gateway-ceo-agent
```

## E. Docker CEO recreate 및 identity 확인

기본 upstream path:

```bash
docker compose up -d --no-build --no-deps --force-recreate ceo-hermes
```

override path:

```bash
docker compose -f docker-compose.yml -f docker-compose.discord-idempotency.yml up -d --no-build --no-deps --force-recreate ceo-hermes
```

```bash
docker compose ps ceo-hermes
docker exec hedgefund-ceo-hermes sh -lc 'printf "HERMES_HOME=%s\nHERMES_PROFILE=%s\nHERMES_KANBAN_HOME=%s\n" "$HERMES_HOME" "$HERMES_PROFILE" "$HERMES_KANBAN_HOME"'
docker inspect -f 'restart={{.HostConfig.RestartPolicy.Name}} status={{.State.Status}}' hedgefund-ceo-hermes
docker top hedgefund-ceo-hermes -eo pid,args
gateway_processes="$(docker top hedgefund-ceo-hermes -eo pid,args | awk 'NR > 1 && /gateway[[:space:]]+run/ {count++} END {print count + 0}')"
if [ "$gateway_processes" -ne 1 ]; then
  echo "expected exactly one CEO gateway process, got $gateway_processes"
  exit 1
fi
docker logs --since=3m hedgefund-ceo-hermes 2>&1 | grep -Ei 'discord|connected as|active profile|gateway' | tail -50
```

Expected:

- `HERMES_HOME=/opt/data`
- `HERMES_PROFILE=ceo-agent`
- `HERMES_KANBAN_HOME=/opt/kanban`
- restart policy `unless-stopped`
- `docker top`의 `gateway run` process 정확히 1개
- logs의 CEO profile, CEO Discord bot identity, Discord connected/readiness

검증 실패 시 다음 단계로 진행하지 않는다.

## F. CEO canary + Kanban 검증

Discord에서 정확히 한 번 보낸다.

```text
@CEO 테스트-CEO-CANARY
```

Acceptance:

- 답변 정확히 1개
- 응답 bot이 CEO bot
- Kanban task가 기존 dispatcher/supervisor를 통해 정상 진행
- 다른 7개 bot은 해당 메시지를 처리하지 않음

```bash
docker logs --since=5m hedgefund-ceo-hermes 2>&1 | grep -Ei 'discord|connected as|active profile|gateway|error|traceback' | tail -80
docker compose ps kanban-dispatcher ceo-kanban-supervisor
```

기본 upstream path에서는 HgFinance idempotency structured log를 기대하지 않는다.
single-owner 상태에서도 duplicate가 계속되면 message ID, Docker process 목록,
gateway/profile identity, Kanban task 생성 횟수를 기록한 뒤 중단한다. 그 후에만
idempotency override를 별도 canary로 적용한다.

실패 시:

```bash
docker compose stop ceo-hermes
systemctl --user enable --now hermes-gateway-ceo-agent
```

## G. 나머지 7개 Gateway 전환

CEO canary 성공 후에만 실행한다.

```bash
systemctl --user stop hermes-gateway-hr-department hermes-gateway-research-department hermes-gateway-quant-backtest-department hermes-gateway-accounting-portfolio-department hermes-gateway-trading-department hermes-gateway-risk-management hermes-gateway-qa-department
docker compose up -d --no-build --no-deps --force-recreate workforce-hermes research-hermes quant-hermes accounting-hermes trading-hermes risk-hermes qa-hermes
docker compose ps
```

override path라면 위 명령에 같은 `-f` 옵션을 붙인다.

Expected: 8개 Docker Gateway가 모두 Up이고 profile, mount, restart가 canonical
mapping과 일치한다.

실패 시:

```bash
docker compose stop workforce-hermes research-hermes quant-hermes accounting-hermes trading-hermes risk-hermes qa-hermes
systemctl --user enable --now hermes-gateway-hr-department hermes-gateway-research-department hermes-gateway-quant-backtest-department hermes-gateway-accounting-portfolio-department hermes-gateway-trading-department hermes-gateway-risk-management hermes-gateway-qa-department
```

## H. Host systemd 8개 disable

나머지 7개 canary와 8개 bot online 상태를 확인한 뒤에만 실행한다.
unit 파일은 삭제하지 않는다.

```bash
for unit in "${HOST_UNITS[@]}"; do
  systemctl --user stop "$unit"
  systemctl --user disable "$unit"
done
running_host_gateways="$(systemctl --user --type=service --state=running --no-legend | grep -E 'hermes-gateway-' || true)"
if [ -n "$running_host_gateways" ]; then
  printf '%s\n' "$running_host_gateways"
  exit 1
fi
```

Expected: Host hermes-gateway-* running process 0개, service file은 남아 있고
disabled다.

## I. Single-owner 검증

```bash
docker compose ps
for c in "${GATEWAY_CONTAINERS[@]}"; do
  docker inspect -f '{{.Name}} status={{.State.Status}} restart={{.HostConfig.RestartPolicy.Name}}' "$c"
  docker top "$c" -eo pid,args
  docker exec "$c" sh -lc 'printf "profile=%s " "$HERMES_PROFILE"; ps -eo args | grep -E "[h]ermes.*gateway[[:space:]]+run" | wc -l'
done
running_host_gateways="$(systemctl --user --type=service --state=running --no-legend | grep -E 'hermes-gateway-' || true)"
if [ -n "$running_host_gateways" ]; then
  printf '%s\n' "$running_host_gateways"
  exit 1
fi
```

Expected: Docker Gateway 8개, 각 profile gateway process 1개, Host gateway
0개다. dispatcher, supervisor, BFF, workers, Redis는 기존 구조대로 running이어야 한다.

## J. EC2 reboot

```bash
sudo reboot
```

## K. Reboot acceptance

SSH 재접속 후 어떤 `hermes gateway run` 또는 `systemctl --user start`도 실행하지 않는다.

```bash
cd ~/hgfinance
docker compose ps
systemctl --user --type=service --state=running --no-legend | grep -E 'hermes-gateway-' || true
docker compose logs --since=5m ceo-hermes workforce-hermes research-hermes quant-hermes accounting-hermes trading-hermes risk-hermes qa-hermes | tail -200
```

Expected: Docker daemon이 8개 Gateway를 자동 복구하고 Host Gateway는 실행되지
않는다. Discord 8개 bot이 online이며 CEO canary mention 한 번에 응답 하나만 온다.

## L. Rollback

어느 canary라도 실패하면 profile/auth/DB를 건드리지 않고 Docker Gateway를
중지한 뒤 Host unit을 복구한다.

```bash
docker compose stop ceo-hermes workforce-hermes research-hermes quant-hermes accounting-hermes trading-hermes risk-hermes qa-hermes
for unit in "${HOST_UNITS[@]}"; do
  systemctl --user enable --now "$unit"
done
systemctl --user --type=service --state=running --no-legend | grep -E 'hermes-gateway-'
```

## Local acceptance

```bash
docker compose config --quiet
python3 -m unittest tests.contracts.test_discord_gateway_wiring -v
python3 -m unittest tests.orchestration.test_discord_gateway_patch tests.orchestration.test_discord_idempotency -v
bash -n scripts/sync_hermes_profiles.sh
git diff --check
```
