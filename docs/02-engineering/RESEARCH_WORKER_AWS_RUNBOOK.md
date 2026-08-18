# 리서치본부 Worker Runtime + Model Plane AWS 런북

> **Scope/status:** 이 런북은 tracked FP8 model-plane 절차의 historical/design
> baseline이다. 현재 AWS가 AWQ로 전환되었다는 외부 runtime 상태는 이 저장소에서
> 검증되지 않았으며, 최신 구현 상태는
> [CURRENT_PROJECT_ARCHITECTURE.md](../CURRENT_PROJECT_ARCHITECTURE.md)를 따른다.

소유: 재일 · 작성 2026-08-13 · 상태: Phase 2/3 첫 수직 슬라이스 (Research)

이 문서는 **이미 떠 있는 Control Plane**(portfolio-bff → CEO → Kanban → dispatcher →
research-hermes → research-mcp) 위에 다음을 얹는 절차다.

```
research-hermes (본부장, MCP 도구 호출)
      ↓ run_research_workers / get_worker_job / worker_model_health   ← 신규 MCP 도구
research-mcp (LangGraph runner - employee_worker_runtime)
      ↓ Worker Model Gateway (departments/worker_model_gateway.py)   ← 신규
vLLM (hedgefund-vllm, compose 오버레이)                               ← 신규
      ↓
Qwen2.5-14B-Instruct FP8 (EBS /opt/hgfinance/models, S3 가 정본)
```

부서 내부만 구현한다 - 부서 간 통신은 이 런북 범위 밖이다.
Worker 산출은 언제나 비구속 worker-context 다(binding=false).

## 0. 전제 확인 (GPU)

```bash
# 인스턴스 타입 (목표: g6.xlarge L4 24GB - FINAL_RUNTIME_ARCHITECTURE §3.1)
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
curl -sH "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/instance-type; echo

nvidia-smi                       # 드라이버가 보여야 한다
docker info 2>/dev/null | grep -i nvidia   # nvidia container runtime 확인
```

진단을 가른다 - 순서대로:

1. **인스턴스 타입이 g6 계열이 아니면**: GPU 인스턴스가 아니다. Model Plane 은
   g6 계열로 이전/리사이즈 후 진행한다.
2. **g6 인데 `nvidia-smi` 가 없으면**: 인스턴스는 맞고 **드라이버가 없는 것**이다.
   순정 Ubuntu 24.04 AMI 는 NVIDIA 드라이버가 사전 설치돼 있지 않다:

```bash
sudo apt-get update && sudo apt-get install -y ubuntu-drivers-common
sudo ubuntu-drivers install --gpgpu     # 또는: sudo apt-get install -y nvidia-driver-570-server
sudo reboot   # 재부팅 후 nvidia-smi 재확인
```

3. **드라이버는 있는데 docker 에 nvidia 가 없으면** nvidia-container-toolkit 설치:

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
```

## 1. 코드 반영

변경/신규 파일 (모두 이 커밋에 있다):

| 파일 | 역할 |
|---|---|
| `departments/worker_model_gateway.py` | worker_id → 모델/adapter 해석 + OpenAI 호환 호출 (stdlib) |
| `departments/01-research/config/worker_model_registry.json` | worker → adapter 정본 (지금은 전원 base) |
| `departments/01-research/api/mcp_server.py` | `run_research_workers`/`get_worker_job`/`worker_model_health` 도구 |
| `docker-compose.model.yml` | vllm 서비스 + research-mcp 모델 배선 오버레이 |
| `scripts/model_plane/*` | 모델 다운로드·FP8 양자화·manifest |

```bash
cd ~/hgfinance
git fetch origin
git pull            # 배포 브랜치에 위 커밋이 합쳐져 있어야 한다
# mcp_server.py 는 이미지에 COPY 되므로 재빌드가 필요하다
docker compose build research-mcp
```

## 2. 모델 준비 (EBS ← HF, S3 정본화)

```bash
cd ~/hgfinance
chmod +x scripts/model_plane/*.sh

# 2-1. 14B FP8 사전 양자화 체크포인트 (~15GB, 최초 1회)
scripts/model_plane/fetch_base_model.sh
# → /opt/hgfinance/models/Qwen2.5-14B-Instruct-FP8-dynamic + manifest.json

# 2-2. (요구사항: 양자화를 AWS 에서 직접 테스트) 1.5B 로 양자화 파이프라인 검증
#      BF16 로드 → FP8_DYNAMIC → 저장 → quantization_record.json → manifest
scripts/model_plane/run_quantize_fp8.sh
# 14B 직접 양자화는 RAM 64GB+ 인스턴스에서만 (quantize_fp8.py 머리말 참고)

# 2-3. S3 를 정본으로 (버킷 이름 정한 뒤 1회)
export HGF_MODEL_BUCKET=<버킷이름>
aws s3 sync /opt/hgfinance/models/Qwen2.5-14B-Instruct-FP8-dynamic \
  "s3://$HGF_MODEL_BUCKET/models/Qwen2.5-14B-Instruct-FP8-dynamic" --exclude 'hf-cache/*'
```

EBS 를 새로 만들 때는 HF 가 아니라 S3 에서 받고 manifest 로 검증한다:

```bash
aws s3 sync "s3://$HGF_MODEL_BUCKET/models/<이름>" /opt/hgfinance/models/<이름>
python3 scripts/model_plane/model_manifest.py --model-dir /opt/hgfinance/models/<이름> --verify
```

## 3. vLLM 기동

```bash
cd ~/hgfinance
docker compose -f docker-compose.yml -f docker-compose.model.yml up -d vllm
docker logs -f hedgefund-vllm     # "Application startup complete" 까지 수 분
```

확인 (호스트 127.0.0.1 에만 게시된다):

```bash
curl -s http://127.0.0.1:8000/v1/models | python3 -m json.tool
curl -s http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "qwen2.5-14b-instruct-fp8",
  "messages": [{"role":"user","content":"한 문장으로 자기소개"}],
  "max_tokens": 64}' | python3 -m json.tool
```

OOM 이 나면 `.env` 에 `VLLM_MAX_MODEL_LEN=8192` 를 넣고 다시 올린다.
(L4 24GB 기본값은 16384. g6e 48GB 면 32768 로 올려도 된다.)

## 4. research-mcp 재기동 (모델 배선 주입)

```bash
docker compose -f docker-compose.yml -f docker-compose.model.yml up -d research-mcp
docker logs --tail 5 hedgefund-research-mcp   # "research-mcp-v1: 0.0.0.0:8037/mcp"
```

⚠ 이후 research-mcp / vllm 을 만질 때는 **항상 두 -f 를 함께** 쓴다.
오버레이 없이 `docker compose up -d research-mcp` 를 돌리면 모델 env 와
런타임 파일 마운트가 빠진 채 재생성돼, 이후 모든 run_research_workers job 이
`ModuleNotFoundError: No module named 'worker_model_gateway'` 로 FAILED 가 된다
(§7 첫 행).

⚠ **compose 파일 목록 규칙** — `-f` 나 `COMPOSE_FILE` 을 쓰는 순간
`docker-compose.override.yml`(§4.5 에서 만드는 EC2 호스트 적응)은 **자동
적용에서 빠진다.** 그래서 매번 -f 를 나열하는 대신 `.env` 에 넣으려면
override 를 반드시 포함해야 한다 (§4.5 를 먼저 실행한 뒤, 이 EC2 의 다른
작업자와 합의 후):

```
COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.model.yml
```

이걸 빠뜨리면 timescaledb 가 재생성되는 날 cpuset 거부가 되살아난다(§7).

## 4.5 부서 읽기면 기동 (Evidence First 의 데이터 원천)

`run_research_workers` 는 symbol 이 오면 Worker 호출 전에 research-api(뉴스·공시,
Supabase)와 market-api(가격, TSDB)에서 근거를 모은다(§11 Evidence First).
소스별 독립 시도라 market-api 가 없어도 뉴스·공시는 산다.

⚠ **EC2 최초 1회**: 기본 compose 의 timescaledb 는 로컬 24코어 PC 용
`cpuset: "22,23"` 이 박혀 있어 EC2(vCPU 8)에서 생성이 거부된다. compose 주석
(160행)이 "서버로 옮길 때는 cpuset 을 지운다" 라고 정한 그대로, **호스트 전용
override** 로 푼다. `docker-compose.override.yml` 은 gitignore 라 pull 과
충돌하지 않는다. 단 자동 적용은 **-f 도 COMPOSE_FILE 도 없이 부르는 명령에만**
된다 - §3/§4 처럼 -f 를 쓰는 명령이나 COMPOSE_FILE 사용 시에는 목록에
override 를 직접 포함해야 한다(§4 의 규칙 참고).

```bash
# 기존 override 가 있으면 덮어쓰지 말고 내용을 합칠 것
[ -f docker-compose.override.yml ] && { echo "override 가 이미 있다 - 아래 내용을 수동으로 합쳐라"; cat docker-compose.override.yml; } || cat > docker-compose.override.yml <<'EOF'
# EC2 호스트 적응 (gitignored). timescaledb 의 로컬 PC 용 코어 핀을 해제한다.
services:
  timescaledb:
    cpuset: ""
EOF

# 검증: 아무것도 안 나와야 정상 (cpuset 이 병합에서 제거됨)
docker compose config timescaledb 2>/dev/null | grep cpuset

docker compose up -d research-api                  # Supabase 만 필요, 의존 없음
docker compose up -d timescaledb market-api        # TSDB 는 비어 있어도 뜬다
docker compose ps research-api market-api timescaledb
curl -s http://127.0.0.1:8035/health | python3 -m json.tool   # research-api 확인
curl -s http://127.0.0.1:8036/health                          # market-api 확인
```

참고: AWS `.env` 는 `TOOL_GATEWAY_ENFORCE=true` 라 research-api 는 persona
헤더 없는 호출을 403 으로 막는다 - 러너는 `X-Agent-Persona:
holdings-analyst-worker` 명의로 부르므로 통과한다(해당 persona 가
news/disclosures/snapshot/bars 스코프를 전부 보유).

## 5. Worker 직접 스모크 (Hermes 우회 - 러너·게이트웨이·vLLM 검증)

```bash
docker exec -i hedgefund-research-mcp python - <<'PY'
import sys
sys.path.insert(0, "/app"); sys.path.insert(0, "/app/departments")
import worker_model_gateway as gw, employee_workers

# 프로덕션(run_research_workers)과 같은 경로 - 워커별 binding 해석
bindings = {}
def llm_factory(worker_id):
    llm, b = gw.llm_for_worker(worker_id)
    bindings[worker_id] = b.as_metadata()
    return llm

r = employee_workers.run_employee_workers(
    {"holding_question": "이 워커 배선이 동작하는지 한 단락으로 확인 응답하라"},
    llm_factory=llm_factory)
print("bindings:", bindings)
print("executed:", r["executed"], "degraded:", r["degraded"])
print(r["workers"][0]["output"])
PY
```

기대: `executed: ['holdings-analyst-worker'] degraded: False`, output 에
`summary`/`confidence`/`evidence_refs`/`escalate`/`schema_valid: True`.

## 6. E2E - 리서치 헤르메스에게 질문 → Worker → 아웃풋

```bash
# 6-1. 헤르메스가 새 도구를 보는지 (research-mcp 재시작 후 도구 목록이 안 늘면
#      research-hermes 도 재시작한다: docker compose restart research-hermes)
docker exec -u hermes -i hedgefund-research-hermes hermes chat -Q \
  -q 'worker_model_health 도구를 호출하고 결과 JSON 을 그대로 보여줘'

# 6-2. 본질 E2E: 질문 → Evidence First(실데이터) → Worker → 산출 보고
docker exec -u hermes -i hedgefund-research-hermes hermes chat -Q \
  -q 'run_research_workers 도구를 symbol="005930", holding_question="삼성전자를 보유 중이다. 러너가 모아준 최근 뉴스·공시·가격 근거만으로, 확인된 사실과 미확인 사항을 나눠 정리하고 각 주장에 근거 ref(n1, d1 형식)를 달아라" 로 실행해라. job_id 를 받으면 get_worker_job 을 완료될 때까지 반복 조회하고, 완료되면 evidence.sources 상태와 워커 산출의 summary, confidence, evidence_refs, escalate 값을 요약하지 말고 그대로 보고하라. degraded 나 FAILED 소스가 있으면 그 사실도 보고하라.'
```

기대: `evidence.sources.news = OK(n건)` (Supabase 에 로컬 수집기가 쌓아온
실데이터), TSDB 가 비어 있으면 `price_context = UNAVAILABLE` 로 정직하게
표시되고 Worker 는 뉴스·공시 근거만으로 답한다.

턴이 job RUNNING 상태에서 끝나면 이어서 물으면 된다:

```bash
docker exec -u hermes -i hedgefund-research-hermes hermes chat -Q \
  -q 'get_worker_job job_id=<위에서 받은 id> 결과를 그대로 보고하라'
```

## 7. 문제 해결

| 증상 | 원인/조치 |
|---|---|
| job FAILED `ModuleNotFoundError: No module named 'worker_model_gateway'` | 오버레이 없이 research-mcp 를 올렸다(마운트 누락) → §4 명령으로 재기동 |
| job FAILED `URLError`/`timeout` | vLLM 다운 또는 로딩 중 → `worker_model_health`, `docker logs hedgefund-vllm` |
| `worker_model_health` ok:false, served_models 에 이름 없음 | `--served-model-name` 과 `WORKER_MODEL_NAME` 불일치 → .env 확인 |
| worker DEGRADED `worker_output_schema_invalid` | 모델이 계약(JSON) 이탈 → 재시도 3회 후 fail-closed. 반복되면 모델·프롬프트 문제, WORKER_MODEL_MATRIX 절차로 벤치마크 |
| vLLM OOM / KV 부족 | `VLLM_MAX_MODEL_LEN` 축소, `VLLM_GPU_MEMORY_UTILIZATION=0.85` |
| Hermes 가 새 도구를 모른다 | research-hermes 재시작 (MCP 도구 목록 캐시) |
| evidence.sources.news FAILED `URLError` | research-api 미기동 → §4.5 |
| evidence.sources.news FAILED `ApiForbidden` | TOOL_GATEWAY_ENFORCE 켜짐 + persona 스코프 문제 → research-api 로그의 violation 확인 |
| price_context UNAVAILABLE | market-api 미기동 또는 TSDB 에 해당 종목 일봉 없음 - AWS TSDB 는 원래 비어 있다(정상). 뉴스·공시 근거로는 계속 동작한다 |
| timescaledb 생성 거부 `Requested CPUs are not available` | 로컬 PC 용 cpuset → §4.5 의 override 생성. **override 를 이미 만들었는데 재발하면** 그 명령의 -f/COMPOSE_FILE 목록에 override 가 빠진 것이다(§4 규칙) - `docker compose config timescaledb \| grep cpuset` 이 값을 보여주면 누락 확정 |

## 8. 다음 단계 (오늘 범위 밖)

- 부서별 LoRA: BF16 베이스로 파인튜닝(LLaMA-Factory/peft) → adapter 를
  `/opt/hgfinance/models/loras/<이름>` 에 두고 `/v1/load_lora_adapter` 로 적재 →
  `worker_model_registry.json` 의 해당 worker 에 `adapter_id`/`status: enabled` 기록.
  주의: adapter 에 `modules_to_save`(embed/lm_head)를 넣으면 양자화 베이스 위에서
  못 쓴다. 평가는 FP8+LoRA 조합으로 다시 돌린다.
- 다른 부서 수직 슬라이스: 같은 패턴(부서 MCP 에 worker 실행 도구 + 게이트웨이 주입).
- Worker Registry 를 파일에서 DB 로 승격, envelope(model_version/adapter_version)
  스탬핑을 부서 결과 계약에 연결.
