#!/usr/bin/env python3
"""트레이딩 직원별 RAG 경로 정책. **검색도 LLM 호출도 하지 않는다 - 경로만 정한다.**

소유: 도현 (트레이딩본부)
형식 근거: departments/03-risk/skills/rag_router.py — 리스크본부가 먼저 세운 형식을
      그대로 따른다(RAGRoute / RAGPlan / WorkerRAGPolicy). 전사 감사 기준이 부서마다
      다르면 "이 직원이 무엇을 검색할 수 있나"를 부서별로 다시 읽어야 한다.
내용 근거: docs/HEDGE_FUND_MASTER_PLAN.md 5.9(결정론 계층), CLAUDE.md 권한 분리

**정책을 코드에 박는 이유.** 검색 경로를 payload 로 받으면 hot path 가
`{"rag_route": "HYPERGRAPH"}` 한 줄로 아무 직원에게나 전방위 검색을 열 수 있다.
그러면 "이 직원은 무엇을 볼 수 있는가"가 실행마다 달라져 감사가 불가능해진다.
그래서 **LLM 직원 2명은 전원 `forced_route`** 다 — `choose_rag_route()` 가 payload 를
읽는 분기에 애초에 도달하지 못한다.

**이 표는 LLM 직원 표다.** 2026-08-06 tool 강등으로 조건부 직원 5명이 사라지고 그
결정론 근거는 `desk-runner-worker` 하나로 합쳐졌다. 러너는 LLM 이 아니라서 검색
결과가 서술로 새어나갈 통로가 없고, 무엇을 보는지가 provider 목록으로 코드에
고정돼 있다 — 정책이 보호할 대상이 없다. 그래서 정책표에 넣지 않고
`rag_policy_for_worker("desk-runner-worker")` 는 **의도적으로 예외를 낸다**:
누가 러너를 LLM 직원처럼 라우팅하려 하면 조용히 기본 경로를 얻는 대신 막힌다.
러너가 실제로 쓰는 플랜은 `desk_runner_plans()` 가 payload 없이 고정한다.

라우터가 장식이 아닌 지점: `evidence.py` 의 provider 가 `plan.methods` 를 실제로
검사한다. `"lexical" not in plan.methods` 면 broker_rules 검색을 **부르지 않고**
`route_denied` 를 낸다. 라우터가 정책을 말하고 provider 가 복종하는 구조다.

자체 점검: python departments/02-trading/skills/rag_router.py
"""
from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping

# 리스크본부와 같은 리터럴을 쓴다. 트레이딩은 HYPERGRAPH 를 쓰는 직원이 없지만
# 타입을 좁히면 전사 계약이 부서마다 갈라진다.
RAGRoute = Literal["NO_RAG", "HYBRID", "GRAPH", "HYPERGRAPH"]


@dataclass(frozen=True)
class RAGPlan:
    route: RAGRoute
    methods: tuple[str, ...]
    max_chunks: int
    max_hops: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkerRAGPolicy:
    """직원 하나의 검색 경계. 결정론이고 감사 가능하다."""

    allowed_routes: frozenset[RAGRoute]
    forced_route: RAGRoute | None = None
    reason: str = ""


# ── LLM 직원 2명 정책표 ────────────────────────────────────────────────────
# **이 표에는 LLM 을 쓰는 직원만 올린다.** 라우터의 목적은 "이 직원의 *서술*이 어떤
# 문서를 근거로 삼을 수 있나"를 고정하는 것이라, 서술을 만들지 않는 직원에게는
# 규율할 대상이 없다. 2026-08-06 에 조건부 직원 5명을 결정론 `desk-runner` 하나로
# 합치면서 그 항목들을 지웠다 - 남겨두면 "정책이 있다 = LLM 직원이다"라는 이 표의
# 뜻이 흐려진다.
#
# 전원 forced_route 다. 하나라도 비워두면 그 직원만 입력으로 경로를 바꿀 수 있게 된다.
WORKER_RAG_POLICIES: dict[str, WorkerRAGPolicy] = {
    # Bull/Bear 는 Claim 색인이 이미 주입된 근거다. 직원이 따로 검색하면 상대가 보지
    # 못한 문서가 몰래 결론에 들어오고, 그 순간 debate_merge 의 독립성·인용 검증이
    # 우회된다 - 확증편향을 막으려고 나눈 두 직원이 서로 다른 근거를 갖게 된다.
    "bull-thesis-worker": WorkerRAGPolicy(
        frozenset({"NO_RAG"}), "NO_RAG",
        "Claim 색인 외 검색은 상대가 못 본 근거를 들여와 독립성 검증을 우회한다"),
    "bear-thesis-worker": WorkerRAGPolicy(
        frozenset({"NO_RAG"}), "NO_RAG",
        "Claim 색인 외 검색은 상대가 못 본 근거를 들여와 독립성 검증을 우회한다"),
}

# desk-runner(결정론 잡무) 전용 플랜. **정책표 밖이다.**
#
# 흡수한 근거원 셋이 요구하는 수단의 합집합이다 - 전이표 조회(graph_context),
# 브로커 규칙 정확일치(lexical), 집행 기억 정형 필터(structured_filter). 경로를 넓게
# 여는 것처럼 보이지만 위험이 다르다: 라우터가 막으려는 것은 *검색 결과가 LLM 서술을
# 몰래 편향시키는 것*인데 이 직원에는 LLM 이 없고, 산출물은 모듈 판정을 그대로 옮긴
# 값이다. 대신 `choose_rag_route(worker_id="desk-runner")` 는 정책이 없어 ValueError 를
# 낸다 - 누군가 이 직원을 LLM 경로에 물리면 조용히 열리는 대신 즉시 죽는다.
DETERMINISTIC_PLAN = RAGPlan(
    route="HYBRID",
    methods=("lexical", "structured_filter", "graph_context"),
    max_chunks=16, max_hops=2,
    reason="desk-runner 는 LLM 이 없다 — 결정론 모듈 출력을 그대로 옮긴다",
)


def rag_policy_for_worker(worker_id: str) -> WorkerRAGPolicy:
    try:
        return WORKER_RAG_POLICIES[worker_id]
    except KeyError as exc:
        # 모르는 직원에게 기본 경로를 주지 않는다. 정책 없는 직원은 돌지 못한다.
        raise ValueError(f"RAG 정책이 없는 트레이딩 Worker 입니다: {worker_id}") from exc


def _plan(route: RAGRoute, reason: str) -> RAGPlan:
    if route == "NO_RAG":
        return RAGPlan(route, (), 0, 0, reason)
    if route == "HYBRID":
        # 벡터가 없다. 트레이딩 근거원(TR 규칙표, 집행 기억)은 정확 일치와 정형
        # 필터가 정답이고 근사 이웃은 개선이 아니라 결함이다(broker_rules 주석 참고).
        return RAGPlan(route, ("lexical", "structured_filter", "rerank"), 12, 0, reason)
    if route == "GRAPH":
        return RAGPlan(route, ("entity_link", "graph_context"), 16, 2, reason)
    return RAGPlan(route, ("entity_link", "hyper_extract", "graph_context"), 20, 3, reason)


def choose_rag_route(payload: Mapping[str, Any], *, worker_id: str) -> RAGPlan:
    """직원의 검색 경로를 정한다. payload 는 **forced_route 가 없을 때만** 읽힌다.

    LLM 직원 2명은 전원 forced 라 지금은 아래 payload 분기에 도달하지 않는다 -
    그게 의도다. 자체 점검이 그 사실을 직접 검사한다. 결정론 직원(desk-runner)은
    정책표에 없어서 여기 오면 ValueError 다 - `DETERMINISTIC_PLAN` 을 직접 쓴다.
    """
    policy = rag_policy_for_worker(worker_id)
    if policy.forced_route is not None:
        return _plan(policy.forced_route,
                     policy.reason or f"{worker_id} 정책상 {policy.forced_route} 고정")

    requested = str(payload.get("rag_route") or "").upper()
    if requested and requested in policy.allowed_routes:
        return _plan(requested, f"{worker_id} 허용 경로 내 요청: {requested}")
    fallback: RAGRoute = "NO_RAG" if "NO_RAG" in policy.allowed_routes else next(
        iter(sorted(policy.allowed_routes)))
    return _plan(fallback, f"요청 {requested or '없음'} 은 허용 목록 밖 - 안전 경로로 떨어진다")


def allows(plan: RAGPlan, method: str) -> bool:
    """provider 가 검색 수단을 쓰기 전에 부르는 함수. 라우터를 강제하는 지점이다."""
    return method in plan.methods


def route_denied(worker_id: str, plan: RAGPlan, method: str) -> dict[str, Any]:
    """경로가 막혔을 때 evidence 에 남길 사실. 조용히 빈 근거로 넘어가지 않는다."""
    return {"checked": False, "reason": f"route_denied:{plan.route}",
            "worker_id": worker_id, "required_method": method,
            "allowed_methods": list(plan.methods)}


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    LLM_WORKERS = ("bull-thesis-worker", "bear-thesis-worker")
    DETERMINISTIC_WORKER = "desk-runner-worker"

    # 1. LLM 직원 2명만 정책이 있다. 모르는 직원은 기본값을 못 얻는다
    assert set(WORKER_RAG_POLICIES) == set(LLM_WORKERS), sorted(WORKER_RAG_POLICIES)
    for worker_id in LLM_WORKERS:
        assert rag_policy_for_worker(worker_id) is not None
    try:
        rag_policy_for_worker("nonexistent-worker")
        raise AssertionError("정책 없는 직원이 경로를 얻었다")
    except ValueError:
        pass
    print("  LLM 직원 2명 + 미등록 차단   OK")

    # 2. **전원 forced_route** - 이게 이 파일의 요지다
    unforced = [w for w, p in WORKER_RAG_POLICIES.items() if p.forced_route is None]
    assert unforced == [], f"forced 가 아닌 직원이 있다: {unforced}"
    assert all(p.reason for p in WORKER_RAG_POLICIES.values()), "사유 없는 정책이 있다"
    print("  전원 forced_route            OK")

    # 3. **입력 플래그로 경로를 못 바꾼다** - hot path 가 검색을 열 수 없다
    attack = {"rag_route": "HYPERGRAPH", "hypergraph": True, "allowed_scopes": ["*"]}
    for worker_id in LLM_WORKERS:
        forced = WORKER_RAG_POLICIES[worker_id].forced_route
        got = choose_rag_route(attack, worker_id=worker_id)
        assert got.route == forced, f"{worker_id}: {got.route} != {forced}"
    print("  입력으로 경로 변경 불가      OK")

    # 4. **결정론 직원은 LLM 경로에 못 물린다** - 조용히 열리는 대신 죽는다
    for caller in (lambda: rag_policy_for_worker(DETERMINISTIC_WORKER),
                   lambda: choose_rag_route(attack, worker_id=DETERMINISTIC_WORKER)):
        try:
            caller()
            raise AssertionError(f"{DETERMINISTIC_WORKER} 가 LLM 라우터를 통과했다")
        except ValueError:
            pass
    # 흡수한 근거원 셋이 요구하는 수단이 전부 열려 있어야 러너가 사실을 다 모은다
    for method in ("lexical", "structured_filter", "graph_context"):
        assert allows(DETERMINISTIC_PLAN, method), method
    assert DETERMINISTIC_PLAN.max_hops == 2 and "LLM 이 없다" in DETERMINISTIC_PLAN.reason
    print("  결정론 직원 정책 밖 + 플랜   OK")

    # 5. 경로별 플랜 모양
    no_rag = choose_rag_route({}, worker_id="bull-thesis-worker")
    assert no_rag.methods == () and no_rag.max_chunks == 0 and no_rag.max_hops == 0
    hybrid = _plan("HYBRID", "형태 검사")
    assert "lexical" in hybrid.methods and "vector" not in hybrid.methods, hybrid.methods
    assert hybrid.max_hops == 0, "정형 검색에 홉이 붙었다"
    assert _plan("GRAPH", "형태 검사").max_hops == 2
    assert set(no_rag.as_dict()) == {"route", "methods", "max_chunks", "max_hops", "reason"}
    print("  경로별 플랜 모양             OK")

    # 6. provider 강제 헬퍼 - 라우터를 장식이 아니게 만드는 지점
    assert allows(hybrid, "lexical") is True
    assert allows(no_rag, "lexical") is False
    denied = route_denied("bull-thesis-worker", no_rag, "lexical")
    assert denied["checked"] is False and denied["reason"] == "route_denied:NO_RAG"
    assert denied["required_method"] == "lexical" and denied["allowed_methods"] == []
    print("  provider 강제 헬퍼           OK")

    # 7. Bull 과 Bear 는 같은 정책이어야 한다 - 한쪽만 검색을 얻으면 그게 편향이다
    bull = WORKER_RAG_POLICIES["bull-thesis-worker"]
    bear = WORKER_RAG_POLICIES["bear-thesis-worker"]
    assert bull.forced_route == bear.forced_route == "NO_RAG"
    assert bull.allowed_routes == bear.allowed_routes
    print("  Bull/Bear 정책 대칭          OK")

    print("ok - 트레이딩 RAG 라우터 7개 영역 점검 통과 "
          f"(LLM 직원 {len(LLM_WORKERS)}명 전원 forced, desk-runner 는 정책표 밖)")
