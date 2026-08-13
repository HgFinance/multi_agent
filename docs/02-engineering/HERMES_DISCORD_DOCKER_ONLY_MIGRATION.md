# Hermes Discord Docker-only migration

AWS에서 Discord Gateway 소유권을 Host systemd에서 Docker Compose로 단계적으로 넘긴다. Host systemd unit 파일은 삭제하지 않고, CEO canary와 8개 bot 검증이 끝난 뒤에만 stop/disable한다. 한 단계가 실패하면 다음 단계로 진행하지 않는다.

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

기본 Compose의 8개 Gateway는 모두 upstream nousresearch/hermes-agent:latest image-only 서비스다. Compose에는 command: ["gateway", "run"]을 지정하지 않는다. pinned Hermes image의 /opt/hermes/docker/entrypoint-dispatch.sh가 PID 1에서 s6 /init을 실행하고, main-wrapper.sh의 인자 없는 경로는 hermes를 실행한다.

따라서 Gateway lifecycle은 다음 하나뿐이다.

```bash
container
  -> entrypoint-dispatch.sh
  -> s6 /init
  -> 02-reconcile-profiles
  -> gateway-default s6 slot
  -> hermes gateway run --replace
```

필수 invariant:

- profile당 Discord Gateway process는 정확히 1개
- Docker Gateway 8개가 canonical profile을 사용
- HERMES_HOME=/opt/data
- HERMES_KANBAN_DISPATCH_IN_GATEWAY=false
- shared Kanban mount와 profile/skill mount 유지
- Compose restart: unless-stopped
- kanban-dispatcher는 별도 standalone daemon
- 기본 production path에서 idempotency patch는 비활성

## Hermes gateway state 처리

Hermes container reconciler는 각 profile의 persisted gateway_state.json을 읽어 s6 service slot을 만들고, running 또는 desired_state=running일 때만 gateway-default를 자동 시작한다. 기존 gateway_state.json은 덮어쓰거나 삭제하지 않는다.

Hermes는 빈 volume의 최초 부팅에 한해 HERMES_GATEWAY_BOOTSTRAP_STATE=running으로 state file을 seed하는 공식 경로를 제공한다. 이 저장소의 기존 AWS profile에는 persisted state가 있으므로 Compose에 이 환경변수를 기본 주입하지 않는다. 이 원칙으로 operator가 저장한 stopped 상태가 다음 container restart에서 running으로 바뀌지 않는다.

새로운 빈 profile volume을 별도로 provision할 때만 해당 변수를 검토한다. 그 경우에도 기존 state file을 먼저 확인하고, 기존 파일이 있으면 변수로 덮어쓰지 않는다.

Dockerfile.hermes-discord, gateway_patch.py, discord_idempotency.py는 별도 docker-compose.discord-idempotency.yml override에만 남겨둔다. 기본 Compose에서는 upstream image를 사용하며 이 override를 함께 지정하지 않는다.

## AWS 공통 변수

```bash
cd ~/hgfinance
GATEWAY_SERVICES="ceo-hermes workforce-hermes research-hermes quant-hermes accounting-hermes trading-hermes risk-hermes qa-hermes"
GATEWAY_CONTAINERS="hedgefund-ceo-hermes hedgefund-workforce-hermes hedgefund-research-hermes hedgefund-quant-hermes hedgefund-accounting-hermes hedgefund-trading-hermes hedgefund-risk-hermes hedgefund-qa-hermes"
HOST_UNITS="hermes-gateway-ceo-agent hermes-gateway-hr-department hermes-gateway-research-department hermes-gateway-quant-backtest-department hermes-gateway-accounting-portfolio-department hermes-gateway-trading-department hermes-gateway-risk-management hermes-gateway-qa-department"
```

## A. PRECHECK

```bash
docker info >/dev/null
docker compose config --quiet
docker compose ps
systemctl --user list-unit-files 'hermes-gateway-*' --no-legend || true
systemctl --user --type=service --state=running --no-legend | grep -E 'hermes-gateway-' || true
```

Expected: Docker daemon enabled, Compose config valid, current Docker/Host state recorded. Host unit을 이 단계에서 stop/disable하지 않는다. 실패하면 중단한다.

## B. BACKUP

```bash
BACKUP_DIR="$HOME/hgfinance-docker-gateway-backup-$(date +%Y%m%d%H%M%S)"
mkdir -p "$BACKUP_DIR"
docker compose config > "$BACKUP_DIR/compose.rendered.yaml"
for unit in $HOST_UNITS; do
  systemctl --user cat "$unit" > "$BACKUP_DIR/$unit.service.txt" 2>&1 || true
done
```

profile, auth.json, .env, gateway_state.json, shared Kanban DB는 이동·삭제하지 않는다.

## C. PULL / IMAGE WIRING

기본 production path는 image-only이므로 build가 아니라 pull을 사용한다.

```bash
docker compose pull $GATEWAY_SERVICES
docker image inspect nousresearch/hermes-agent:latest >/dev/null
```

기본 path에서는 Dockerfile.hermes-discord를 build하지 않는다. idempotency defense-in-depth가 실제 필요하다는 별도 증거가 있을 때만 override를 사용한다. 그 경우 이후 모든 Compose 명령에 동일한 두 -f 옵션을 붙인다.

```bash
docker compose -f docker-compose.yml -f docker-compose.discord-idempotency.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.discord-idempotency.yml build $GATEWAY_SERVICES
docker image inspect hgfinance/hermes-discord:discord-idempotency-v1 >/dev/null
```

이번 migration에서는 위 override를 실행하지 않는다.

## D. CEO Host Gateway stop

CEO canary를 위해 Host CEO Gateway만 먼저 stop한다. 아직 disable하지 않는다.

```bash
systemctl --user stop hermes-gateway-ceo-agent
if systemctl --user is-active --quiet hermes-gateway-ceo-agent; then
  echo 'Host CEO gateway is still active' >&2
  exit 1
fi
```

실패 시 즉시 rollback한다.

## E. Docker CEO recreate 및 identity 확인

```bash
docker compose up -d --no-build --no-deps --force-recreate ceo-hermes
docker compose ps ceo-hermes
docker inspect -f 'restart={{.HostConfig.RestartPolicy.Name}} status={{.State.Status}}' hedgefund-ceo-hermes
docker exec hedgefund-ceo-hermes sh -lc 'printf "HERMES_HOME=%s\nHERMES_PROFILE=%s\nHERMES_KANBAN_HOME=%s\nDISPATCH_IN_GATEWAY=%s\n" "$HERMES_HOME" "$HERMES_PROFILE" "$HERMES_KANBAN_HOME" "$HERMES_KANBAN_DISPATCH_IN_GATEWAY"'
docker top hedgefund-ceo-hermes -eo pid,args
gateway_processes="$(docker top hedgefund-ceo-hermes -eo pid,args | awk 'NR > 1 && /gateway[[:space:]]+run/ {count++} END {print count + 0}')"
test "$gateway_processes" -eq 1
docker logs --since=3m hedgefund-ceo-hermes 2>&1 | grep -Ei 'discord|connected as|active profile|gateway' | tail -50
```

Expected: HERMES_HOME=/opt/data, HERMES_PROFILE=ceo-agent, HERMES_KANBAN_DISPATCH_IN_GATEWAY=false, restart policy unless-stopped, docker top의 gateway run process 정확히 1개, logs의 CEO profile/CEO bot identity/Discord connected.

gateway_state.json을 이 단계에서 수정하지 않는다. 필요하면 state 존재 여부만 확인한다.

```bash
docker exec hedgefund-ceo-hermes sh -lc 'if test -f /opt/data/gateway_state.json; then echo gateway_state_present; else echo gateway_state_absent; fi'
```

## F. CEO canary + Kanban 검증

CEO bot을 식별한 뒤 Discord에서 다음 멘션을 정확히 한 번 보낸다.

```bash
@CEO 테스트-CEO-CANARY
```

Acceptance:

- 답변 정확히 1개
- 응답 bot이 CEO bot
- Kanban task가 기존 dispatcher/supervisor를 통해 정상 진행
- 다른 7개 bot은 이 메시지를 처리하지 않음

```bash
docker logs --since=5m hedgefund-ceo-hermes 2>&1 | grep -Ei 'discord|connected as|active profile|gateway|error|traceback' | tail -80
docker compose ps kanban-dispatcher ceo-kanban-supervisor
```

실패하면 다음 단계로 진행하지 않는다.

```bash
docker compose stop ceo-hermes
systemctl --user enable --now hermes-gateway-ceo-agent
```

single-owner 상태에서도 duplicate가 계속되면 message ID, Docker process 목록, profile identity, Kanban task 생성 횟수를 기록하고 중단한다. 그때만 idempotency override를 조사한다.

## G. 나머지 7개 Gateway 전환

CEO canary 성공 후에만 실행한다.

```bash
systemctl --user stop hermes-gateway-hr-department hermes-gateway-research-department hermes-gateway-quant-backtest-department hermes-gateway-accounting-portfolio-department hermes-gateway-trading-department hermes-gateway-risk-management hermes-gateway-qa-department
docker compose up -d --no-build --no-deps --force-recreate workforce-hermes research-hermes quant-hermes accounting-hermes trading-hermes risk-hermes qa-hermes
docker compose ps
```

Expected: 8개 Docker Gateway가 모두 Up이고 canonical profile, mount, restart가 일치한다. 실패 시 Docker 7개를 stop하고 해당 Host unit을 다시 enable/start한다.

## H. Host systemd 8개 disable

나머지 7개 canary와 8개 bot online 상태를 확인한 뒤에만 실행한다. service 파일은 삭제하지 않는다.

```bash
for unit in $HOST_UNITS; do
  systemctl --user stop "$unit"
  systemctl --user disable "$unit"
done
running_host_gateways="$(systemctl --user --type=service --state=running --no-legend | grep -E 'hermes-gateway-' || true)"
test -z "$running_host_gateways"
```

Expected: Host hermes-gateway-* running process 0개, service file은 남아 있고 disabled다.

## I. Single-owner 검증

```bash
docker compose ps
for c in $GATEWAY_CONTAINERS; do
  docker inspect -f '{{.Name}} status={{.State.Status}} restart={{.HostConfig.RestartPolicy.Name}}' "$c"
  docker top "$c" -eo pid,args
  count="$(docker top "$c" -eo pid,args | awk 'NR > 1 && /gateway[[:space:]]+run/ {n++} END {print n + 0}')"
  test "$count" -eq 1
done
running_host_gateways="$(systemctl --user --type=service --state=running --no-legend | grep -E 'hermes-gateway-' || true)"
test -z "$running_host_gateways"
```

Expected: Docker Gateway 8개, 각 container gateway process 1개, Host gateway 0개다. dispatcher, supervisor, BFF, workers, Redis는 기존 구조대로 running이어야 한다.

## J. EC2 reboot

```bash
sudo reboot
```

## K. Reboot acceptance

SSH 재접속 후 어떠한 hermes gateway run 또는 systemctl --user start도 실행하지 않는다.

```bash
cd ~/hgfinance
docker compose ps
systemctl --user --type=service --state=running --no-legend | grep -E 'hermes-gateway-' || true
docker compose logs --since=5m ceo-hermes workforce-hermes research-hermes quant-hermes accounting-hermes trading-hermes risk-hermes qa-hermes | tail -200
```

Expected: Docker daemon이 8개 Gateway를 자동 복구하고 Host Gateway는 실행되지 않는다. Discord 8개 bot이 online이며 CEO canary 한 번에 응답 하나만 온다.

## L. Rollback

어느 canary라도 실패하면 profile/auth/DB/state를 건드리지 않고 Docker Gateway를 중지한 뒤 Host unit을 복구한다.

```bash
docker compose stop ceo-hermes workforce-hermes research-hermes quant-hermes accounting-hermes trading-hermes risk-hermes qa-hermes
for unit in $HOST_UNITS; do
  systemctl --user enable --now "$unit"
done
systemctl --user --type=service --state=running --no-legend | grep -E 'hermes-gateway-'
```

Rollback 후 Host gateway 8개가 running인지 확인하고 Docker gateway는 다시 기동하지 않는다. Host unit 파일은 그대로 남아 있으므로 별도 데이터 복구가 필요 없다.

## Local acceptance

```bash
docker compose config --quiet
docker compose -f docker-compose.yml -f docker-compose.discord-idempotency.yml config --quiet
python3 -m unittest tests.contracts.test_discord_gateway_wiring -v
python3 -m unittest tests.contracts.test_department_compose_wiring -v
python3 -m unittest tests.orchestration.test_discord_gateway_patch tests.orchestration.test_discord_idempotency -v
bash -n scripts/sync_hermes_profiles.sh
git diff --check
```

Docker daemon 접근이 불가능한 로컬에서는 docker compose config 같은 정적 검증만 수행하고, docker top, Discord response count, reboot acceptance는 AWS canary에서 실행한다.
