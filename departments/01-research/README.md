# 리서치본부 (Research)

## Mission

데이터 수집, RAG Evidence와 Research Packet 생성을 담당한다. Universe/Technical/Microstructure/News
Analyst를 소집해 종목별 근거, 촉매, 무효화 조건을 갖춘 Research Packet을 만든다.

## Owner

재일님 — [TEAM_JAEIL_RESEARCH_QUANT_GUIDE](../../docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md)

## 입력·출력 계약

- 입력: 실시간 가격·베타·변동성 데이터, `collectors/news.py`(Tavily)로 조회한 뉴스·공시
- 출력: Research Packet (근거, 촉매, 무효화 조건) → `workflow` step 2 트레이딩본부로 전달

## 실행법

```bash
research-department chat -q 'Build a Research Packet for AAPL'
python3 departments/01-research/collectors/news.py 'AAPL Apple stock'
```

## 테스트

없음 — prompt-only Profile 단계. `collectors/news.py`는 자체 점검 스크립트가 없다(외부 API 호출).

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- `collectors/news.py` — Tavily 뉴스 조회. 구 경로 `fetch_news.py`와 임시 호환 Wrapper는 2026-07-30에
  삭제됐다 — 이 경로가 유일한 실행 경로다
- `references/` 이전 여부는 미결정 — [REPOSITORY_DEPARTMENT_STRUCTURE.md](../../docs/02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md) 7절 참고
