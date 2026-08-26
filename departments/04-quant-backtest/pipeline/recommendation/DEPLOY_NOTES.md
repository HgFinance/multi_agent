# 종목 자문 채팅 배선 (2026-08-25)

`/ui/ceo/ask` 로 종목을 물으면 **가격 근거 + 실시간 뉴스·공시**가 붙은 답이
나온다. 이 문서는 그 배선이 무엇에 의존하는지 적는다 — 하나만 빠져도 조용히
가짜 답이나 차단 메시지로 돌아간다.

## 오버레이 네 개가 전부 필요하다

```
docker compose \
  -f docker-compose.yml \
  -f deploy/aws/docker-compose.paper-order.yml \
  -f docker-compose.model.yml \
  -f docker-compose.research-evidence.yml \
  up -d --no-deps portfolio-bff portfolio-worker
```

| 오버레이 | 없으면 |
|---|---|
| `paper-order.yml` | `DATABASE_URL` 이 **빈 Supabase** 를 본다(reference 0행) |
| `model.yml` | 워커 모델 좌표 누락 |
| `research-evidence.yml` | `PORTFOLIO_WORKER_RUNTIME` 미전달 → **가짜 LLM**(`"TEST async qa context..."`), 뉴스·공시 조회면 주소·토큰 없음 |

떠 있는 조합 확인:

```
docker inspect <container> \
  --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
```

## 코드는 이미지 내장이다 — `docker cp` 는 휘발한다

`portfolio-bff`/`portfolio-worker` 는 `orchestration/` 을 마운트하지 않는다.
소스를 고쳤으면 **반드시 `docker compose build`** 해야 하고, `docker cp` 로
넣은 것은 컨테이너를 재생성하는 순간 사라진다(실제로 한 번 되돌아갔다).

## 데이터 경로

```
portfolio-worker ──HTTP──▶ market-api  GET /levels/{symbol}
                              지지·저항·목표·손절 (일봉에서 결정론 계산, 자격 불필요)

                 ──HTTP──▶ research-mcp GET /evidence/holdings/{symbol}   [Bearer]
                              뉴스·공시 (NAVER·DART 자격은 research-mcp 에만)
```

**자격을 옮기지 않는다.** portfolio-worker 는 NAVER/DART/LS 키가 없고 그게 맞는
경계다 — 결과만 받아 온다. `web_search.py` 의 RES-08 전담 계약도 건드리지 않았다.

성능: research-mcp 첫 호출 87초(DART 기업색인 다운로드), 이후 **3초**.
색인은 24시간 디스크 캐시(`DART_CORP_INDEX_CACHE`)라 컨테이너를 새로 만들면
그 한 번만 다시 문다.

## Worker 는 선언한 입력만 본다

`holdings-analyst-worker.input_fields = (holding_question, portfolio_state, news)`.
payload 최상단에 실으면 **워커에게 영원히 안 보인다.** 그래서

- 가격 근거 → `portfolio_state.price_levels`
- 뉴스·공시 → `news.request_time_evidence`

에 넣는다. 그리고 **한 dict 안에서 같은 키를 두 번 정의하지 않는다** — 뒤가
앞을 덮어 근거가 통째로 사라진 적이 있다(payload 의 news 가 `{"status": ...}`
뿐이었다).

## PIT 게이트

`_live_worker_inputs_ready` 가 두 모드로 갈린다.

- `PORTFOLIO_BUILD`(질의 없음) — 기존 엄격 게이트 그대로. 후보·스냅샷 필수.
- `ADVISORY_BRIEF`(질의 있음) — control DB 연결만 확인. Worker 가 후보·스냅샷을
  읽지 않기 때문이다(2026-08-25 확인).

**좁힌 것은 Worker 실행 조건뿐이다.** 후보가 비면 suitability 는 여전히
`NO_MATCH`, `safe_action` 은 `HOLD`, `manual_review_required` 는 True 다.

## 지금 되는 것 / 안 되는 것

되는 것
- `"삼성전자 지금 어때?"` 같은 **종목 질의** → 종가·지지·저항·목표·손절 + 최신 뉴스
- 종목명 또는 6자리 코드가 질의에 있어야 한다(없으면 `NO_SYMBOL`)

안 되는 것
- `"포트폴리오 추천해줘"` — 종목이 없어 가격 근거가 안 붙는다
- **수급(t1717)** — LS 자격이 ls-mcp 에만 있다. 조회면을 하나 더 내면 붙는다
- **매집 추천** — 배치 산출물이다(`run_ownership.sh`), 채팅과 별개
