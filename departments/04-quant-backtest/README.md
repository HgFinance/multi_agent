# 퀀트/백테스트본부 (Quant / Backtest)

## Mission

전략 가설, Dataset, Backtest와 Release Candidate를 담당한다. Point-in-Time Backtest, Walk-Forward,
Champion/Challenger 비교를 수행하고 검증된 불변 Strategy Bundle만 Shadow/Paper 배포 후보로 제출한다.

`quant-backtest-department`는 Production 승격을 직접 하지 않는다. CEO·Risk·QA 승인이 필요하다
(`CLAUDE.md` "절대 깨면 안 되는 권한 분리" 참고). 실시간 신호 파이프라인과 분리된
`strategy_research_cycle`로만 동작하며, 실시간 운용 중 전략 코드를 직접 수정하지 않는다.

## Owner

재일님 — [TEAM_JAEIL_RESEARCH_QUANT_GUIDE](../../docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md)

## 입력·출력 계약

- 입력: 시장 시계열 데이터(`timescaledb/migrations/`), 전략 가설
- 출력: 검증된 Strategy Bundle → `strategy_research_cycle` step 2 QA본부로 전달

## 실행법

```bash
quant-backtest-department chat -q 'Backtest [전략 가설]'
```

## 테스트

없음 — prompt-only Profile 단계.

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- Dataset, Experiment, Backtest, Registry 모듈은 아직 미구현 — 코드가 생기면
  `datasets/`, `experiments/`, `backtests/`, `registry/`에 배치
