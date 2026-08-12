#!/usr/bin/env python3
"""Break Triage — 대사 Break 의 원인 후보 검색과 Aging/SLA (exception-investigation-worker).

소유: 도현 (회계/포트폴리오본부)
근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.5, 8.4, 11(DoD 7번)
      docs/HEDGE_FUND_MASTER_PLAN.md 11.2, 12.4
      CLAUDE.local.md 원칙 5(회계 수치를 LLM 문장에서 확정하지 않는다)

**판정은 reconciliation.py 가 유지한다.** 이 파일에는 Break 를 만들거나 Severity 를
매기는 경로가 없다 — 이미 만들어진 Break 를 입력으로 받아 "왜 났을 법한가"의 후보와
과거 해소 사례를 붙일 뿐이다. 그래서 여기서 나온 것은 전부 서술 재료이고 판정이 아니다.

셋으로 나뉜다:

  1. 사례 검색(RAG) — `similar_cases()` 가 과거 해소된 Break 에서 같은 kind·유사 상황을
     찾는다. 코퍼스는 `accounting.breaks` 의 실제 해소 이력이다(status RESOLVED/CLOSED/
     WAIVED + resolution 텍스트). 해소 이력이 없는 초기에는 accounting_ops.yaml 의
     원인 분류표가 Cold Start 근거이며, **이력이 쌓이면 이력이 분류표를 이긴다.**
  2. 인용 검증 — `verify_citations()` 가 색인 밖 case_id / cause_id 를 날조로 잡는다.
     서술은 LLM 이 하되 근거 id 는 여기서 확인한다(skills/agentic-rag 와 같은 원칙).
  3. Aging/SLA — `check_aging()` 은 LLM 을 아예 부르지 않는다. Severity 별 기한을
     넘긴 Break 를 결정론으로 OVERDUE 로 만든다. **Break 는 시간이 지난다고 사라지지
     않는다** — 조용히 늙는 Break 가 가장 위험하다(DoD 7번 "임의로 숨겨지지 않는다").

**임베딩을 쓰지 않는다.** 코퍼스가 우리 해소 이력이라 규모가 작고, 검색의 정답은
"같은 kind 에서 실제로 이렇게 풀었다"이지 의미가 비슷한 아무 사례가 아니다.
인터페이스는 `skills/agentic-rag` 의 `search()` 와 같은 모양(질의 in, 점수 붙은 조각 out)
이라 이력이 커져 pgvector 가 필요해지면 `similar_cases()` 만 바꾸면 된다.

자체 점검: python departments/05-accounting-portfolio/reconciliation/break_triage.py
  DATABASE_URL 이 있으면 실 DB 해소 이력 조회(읽기 전용)까지 본다.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

_HERE = Path(__file__).resolve().parent
_DEPT = _HERE.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_DEPT / "ledger"))

import yaml

from reconciliation import Break, Severity

OPS_PATH = _DEPT / "accounting_ops.yaml"

# 해소가 끝난 Break 만 사례가 된다. OPEN/INVESTIGATING 은 아직 답이 아니다.
RESOLVED_STATUSES = ("RESOLVED", "CLOSED", "WAIVED")


class BreakTriageError(Exception):
    """원인 후보를 만들 수 없는 경우. 빈 근거로 서술을 열지 않는다."""


@dataclass(frozen=True)
class ResolvedCase:
    """과거에 실제로 해소된 Break 한 건. 검색 코퍼스의 단위다."""

    case_id: str          # "case:<break_id 앞 12자>"
    kind: str
    severity: str
    recon_type: str
    detail: str
    resolution: str
    resolved_at: str | None

    @property
    def text(self) -> str:
        return f"[{self.kind}/{self.severity}] {self.detail} -> {self.resolution}"


@dataclass(frozen=True)
class ScoredCase:
    case: ResolvedCase
    score: float


@dataclass(frozen=True)
class AgingSettings:
    sla: Mapping[str, timedelta]
    due_soon_ratio: float


def load_ops(path: Path = OPS_PATH) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BreakTriageError(f"운영 튜닝 파일을 읽을 수 없습니다: {path}") from exc
    if not doc or "break_triage" not in doc:
        raise BreakTriageError(f"{path} 에 break_triage 블록이 없습니다. 튜닝값을 코드에 두지 않습니다")
    return doc["break_triage"]


def aging_settings(ops: Mapping[str, Any] | None = None) -> AgingSettings:
    ops = ops if ops is not None else load_ops()
    return AgingSettings(
        sla={k: timedelta(hours=float(v)) for k, v in ops["sla_hours"].items()},
        due_soon_ratio=float(ops["due_soon_ratio"]),
    )


# ── 1. 사례 검색 (결정론) ──────────────────────────────────────────────────
def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^0-9A-Za-z가-힣]+", (text or "").lower()) if len(t) > 1}


def similar_cases(brk: Break, corpus: Sequence[ResolvedCase], *,
                  recon_type: str | None = None,
                  ops: Mapping[str, Any] | None = None) -> list[ScoredCase]:
    """같은 kind 의 과거 해소 사례를 점수순으로. 같은 kind 가 항상 유사어를 이긴다."""
    ops = ops if ops is not None else load_ops()
    query = _tokens(f"{brk.kind} {brk.detail}")
    scored: list[ScoredCase] = []
    for case in corpus:
        score = 0.0
        if case.kind == brk.kind:
            score += 10.0                      # 같은 종류의 Break — 이게 제일 강한 신호다
        if recon_type and case.recon_type == recon_type:
            score += 2.0
        if case.severity == str(brk.severity):
            score += 1.0
        score += len(query & _tokens(case.text))
        if score >= float(ops["min_similarity"]):
            scored.append(ScoredCase(case=case, score=score))
    scored.sort(key=lambda s: (-s.score, s.case.case_id))
    return scored[: int(ops["max_cases"])]


def cause_candidates(brk: Break, *, ops: Mapping[str, Any] | None = None) -> list[dict[str, str]]:
    """분류표의 원인 후보. 사례가 없을 때의 Cold Start 근거다."""
    ops = ops if ops is not None else load_ops()
    return [dict(entry) for entry in (ops.get("cause_taxonomy") or {}).get(brk.kind, [])]


def triage(brk: Break, corpus: Sequence[ResolvedCase] = (), *,
           recon_type: str | None = None,
           ops: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Break 하나의 원인 후보 묶음. **판정이 아니다.**

    Severity 도 status 도 그대로 실어 나르기만 한다 — 이 함수에 그 둘을 바꾸는 경로가 없다.
    """
    ops = ops if ops is not None else load_ops()
    cases = similar_cases(brk, corpus, recon_type=recon_type, ops=ops)
    causes = cause_candidates(brk, ops=ops)
    enough = len(corpus) >= int(ops["min_corpus_for_case_based"])
    return {
        "break_id": str(brk.break_id),
        "kind": brk.kind,
        # 아래 둘은 reconciliation.py 가 정한 값이며 여기서 재계산하지 않는다.
        "severity": str(brk.severity),
        "escalates": brk.escalates,
        "similar_cases": [
            {"case_id": s.case.case_id, "score": s.score, "kind": s.case.kind,
             "detail": s.case.detail, "resolution": s.case.resolution,
             "resolved_at": s.case.resolved_at}
            for s in cases
        ],
        "cause_candidates": causes,
        # 근거가 이력인지 분류표인지 소비자가 구분할 수 있어야 한다.
        "evidence_basis": "resolved_cases" if (cases and enough) else "taxonomy_only",
        "corpus_size": len(corpus),
        "citable_ids": sorted({s.case.case_id for s in cases} | {c["id"] for c in causes}),
        # 계약 — 이 결과로 Break 를 닫거나 등급을 바꾸지 않는다.
        "decided_by": "deterministic_reconciliation",
        "authoritative": False,
        "changes_severity": False,
    }


def triage_context(triaged: Mapping[str, Any]) -> str:
    """Worker 프롬프트에 넣는 근거 블록. 이 목록 밖의 id 는 인용할 수 없다."""
    lines = [
        "아래 근거만 인용할 수 있습니다. 목록에 없는 id 를 만들어 쓰지 마십시오.",
        "각 주장에 case_id 또는 cause id 를 붙이십시오.",
        "**금액·수량·NAV 를 문장에서 새로 만들지 마십시오** — 수치는 원장에서만 나옵니다.",
        "Break 의 severity 와 종결 여부는 이미 정해졌습니다. 바꾸지 마십시오.",
        "",
        f"대상 Break: {triaged['break_id']} kind={triaged['kind']} "
        f"severity={triaged['severity']} (확정값)",
        "",
    ]
    if triaged["similar_cases"]:
        lines.append("과거 해소 사례:")
        lines += [f"- {c['case_id']} | {c['detail']} -> {c['resolution']}"
                  for c in triaged["similar_cases"]]
    else:
        lines.append("과거 해소 사례: 없음 (이력이 쌓이기 전이다 - 사례가 있다고 말하지 마십시오)")
    lines.append("")
    if triaged["cause_candidates"]:
        lines.append("원인 분류표 후보:")
        lines += [f"- {c['id']} | {c['cause']} | 확인방법: {c['check']}"
                  for c in triaged["cause_candidates"]]
    else:
        lines.append("원인 분류표 후보: 없음")
    return "\n".join(lines)


def verify_citations(refs: Iterable[str], triaged: Mapping[str, Any]) -> dict[str, Any]:
    """색인 밖 case_id / cause id 는 날조다. LLM 이 아니라 여기서 잡는다."""
    allowed = set(triaged.get("citable_ids") or [])
    refs = [str(r) for r in refs]
    unknown = sorted({r for r in refs if r not in allowed})
    return {"refs": refs, "unknown_refs": unknown, "uncited": not refs,
            "grounded": bool(refs) and not unknown}


# ── 2. Aging / SLA (결정론 - LLM 없음) ─────────────────────────────────────
@dataclass(frozen=True)
class BreakAge:
    break_id: str
    severity: str
    status: str
    kind: str | None
    age: timedelta
    sla: timedelta
    aging_status: str      # WITHIN_SLA | DUE_SOON | OVERDUE | UNKNOWN_SLA
    due_at: datetime
    overdue_by: timedelta


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        stamp = value
    else:
        try:
            stamp = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise BreakTriageError(f"created_at 을 읽을 수 없습니다: {value!r}") from exc
    if stamp.tzinfo is None:
        # naive 를 UTC 로 가정하면 KST 와 9시간 어긋난다. 추측하지 않는다.
        raise BreakTriageError(f"created_at 에 timezone 이 없습니다: {value!r}")
    return stamp


def check_aging(open_break_rows: Iterable[Mapping[str, Any]], *, now: datetime | None = None,
                settings: AgingSettings | None = None) -> dict[str, Any]:
    """미종결 Break 의 경과 시간을 SLA 와 대조한다.

    입력은 `recon_repository.open_breaks()` 가 돌려주는 행 모양 그대로다.
    Severity 가 SLA 표에 없으면 통과시키지 않고 `UNKNOWN_SLA` 로 남긴다 -
    모르는 등급을 기한 없음으로 읽으면 그 Break 만 영원히 안 늙는다(개발 원칙 9).
    """
    settings = settings or aging_settings()
    now = now or datetime.now(timezone.utc)
    aged: list[BreakAge] = []
    for row in open_break_rows:
        severity = str(row.get("severity", "")).lower()
        created = _parse_time(row.get("created_at"))
        age = now - created
        sla = settings.sla.get(severity)
        if sla is None:
            status, sla, due_at = "UNKNOWN_SLA", timedelta(0), created
        else:
            due_at = created + sla
            if age > sla:
                status = "OVERDUE"
            elif age >= sla * settings.due_soon_ratio:
                status = "DUE_SOON"
            else:
                status = "WITHIN_SLA"
        aged.append(BreakAge(
            break_id=str(row.get("break_id")), severity=severity,
            status=str(row.get("status", "")), kind=row.get("kind"),
            age=age, sla=sla, aging_status=status, due_at=due_at,
            overdue_by=max(age - sla, timedelta(0)) if status == "OVERDUE" else timedelta(0),
        ))

    by_status: dict[str, list[str]] = {}
    for item in aged:
        by_status.setdefault(item.aging_status, []).append(item.break_id)
    overdue = [a for a in aged if a.aging_status == "OVERDUE"]
    return {
        "as_of": now.isoformat(),
        "total_open": len(aged),
        "by_aging_status": {k: sorted(v) for k, v in sorted(by_status.items())},
        "overdue": [
            {"break_id": a.break_id, "severity": a.severity, "kind": a.kind,
             "age_hours": round(a.age.total_seconds() / 3600, 2),
             "sla_hours": round(a.sla.total_seconds() / 3600, 2),
             "overdue_hours": round(a.overdue_by.total_seconds() / 3600, 2),
             "due_at": a.due_at.isoformat()}
            for a in sorted(overdue, key=lambda x: -x.overdue_by.total_seconds())
        ],
        "unknown_sla": sorted(a.break_id for a in aged if a.aging_status == "UNKNOWN_SLA"),
        # 기한을 넘긴 Break 가 있으면 마감 서술이 그걸 빼놓지 못하게 한다.
        "sla_breached": bool(overdue),
        "decided_by": "deterministic",
    }


# ── 3. 해소 이력 조회 (읽기 전용) ─────────────────────────────────────────
_CASE_SQL = """
select b.break_id::text, b.severity, b.evidence, b.resolution, b.resolved_at,
       r.reconciliation_type
  from accounting.breaks b
  join accounting.reconciliation_items i
    on i.reconciliation_item_id = b.reconciliation_item_id
  join accounting.reconciliations r
    on r.reconciliation_id = i.reconciliation_id
 where r.fund_id = %(fund_id)s
   and b.status = any(%(statuses)s)
   and b.resolution is not null
 order by b.resolved_at desc nulls last
 limit %(limit)s
"""


def load_corpus(repo, fund_id, *, limit: int = 500) -> list[ResolvedCase]:
    """해소된 Break 이력을 검색 코퍼스로 읽는다. **읽기만 한다.**

    `repo` 는 `ledger.repository.LedgerRepository` 다. 여기서 연결을 새로 만들지 않는
    이유는 마감 파이프라인이 이미 열어둔 연결을 재사용해야 하기 때문이다.
    """
    with repo.cursor() as cur:
        cur.execute(_CASE_SQL, {"fund_id": fund_id, "statuses": list(RESOLVED_STATUSES),
                                "limit": limit})
        rows = cur.fetchall()
    corpus: list[ResolvedCase] = []
    for break_id, severity, evidence, resolution, resolved_at, recon_type in rows:
        evidence = evidence or {}
        corpus.append(ResolvedCase(
            case_id=f"case:{break_id[:12]}", kind=str(evidence.get("kind") or "unknown"),
            severity=str(severity).lower(), recon_type=str(recon_type),
            detail=str(evidence.get("detail") or ""), resolution=str(resolution),
            resolved_at=resolved_at.isoformat() if resolved_at else None,
        ))
    return corpus


if __name__ == "__main__":
    from uuid import uuid4

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ops = load_ops()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

    def brk(kind="cash_mismatch", severity=Severity.HIGH, detail="현금 불일치: 내부 100 vs 브로커 90"):
        return Break(break_id=uuid4(), kind=kind, severity=severity, detail=detail)

    def case(case_id, kind, resolution, *, severity="high", recon_type="cash", detail="현금 불일치"):
        return ResolvedCase(case_id=case_id, kind=kind, severity=severity,
                            recon_type=recon_type, detail=detail, resolution=resolution,
                            resolved_at="2026-07-30T09:00:00+00:00")

    def raises(fn, why):
        try:
            fn()
        except BreakTriageError:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. 튜닝값은 코드가 아니라 accounting_ops.yaml 에서 온다
    assert ops["max_cases"] == 5 and ops["min_corpus_for_case_based"] == 3
    assert set(ops["sla_hours"]) == {"material", "high", "medium", "low"}
    assert set(ops["cause_taxonomy"]) >= {"external_only_fill", "position_mismatch", "cash_mismatch"}
    print("  튜닝값 YAML 적재           OK")

    # 2. 같은 kind 가 유사어보다 항상 앞선다
    corpus = [
        case("case:aaa", "cash_mismatch", "미posting 수수료 분개를 반영해 해소"),
        case("case:bbb", "price_mismatch", "현금 불일치 평균단가 문제였다", recon_type="fill"),
        case("case:ccc", "cash_mismatch", "입금 미반영을 원장에 posting"),
    ]
    hits = similar_cases(brk(), corpus, recon_type="cash", ops=ops)
    assert [h.case.case_id for h in hits][:2] == ["case:aaa", "case:ccc"], [h.case.case_id for h in hits]
    assert hits[0].score > hits[-1].score
    print("  유사 사례 검색             OK")

    # 3. 이력이 적으면 사례 기반이라고 말하지 않는다 (Cold Start)
    cold = triage(brk(), corpus[:1], recon_type="cash", ops=ops)
    assert cold["evidence_basis"] == "taxonomy_only", cold["evidence_basis"]
    assert cold["cause_candidates"], "분류표 근거조차 없다"
    warm = triage(brk(), corpus, recon_type="cash", ops=ops)
    assert warm["evidence_basis"] == "resolved_cases"
    empty = triage(brk(kind="unknown_kind"), [], ops=ops)
    assert empty["similar_cases"] == [] and empty["cause_candidates"] == []
    assert empty["evidence_basis"] == "taxonomy_only"
    print("  Cold Start 근거 구분       OK")

    # 4. **판정을 바꾸지 않는다** - severity 는 reconciliation.py 값 그대로다
    material = brk(kind="external_only_fill", severity=Severity.MATERIAL,
                   detail="브로커 체결 e9가 내부에 없습니다")
    out = triage(material, corpus, ops=ops)
    assert out["severity"] == "material" and out["escalates"] is True
    assert out["changes_severity"] is False and out["authoritative"] is False
    assert out["decided_by"] == "deterministic_reconciliation"
    print("  판정 불변 (severity 유지)  OK")

    # 5. 근거 블록에 금지 문구가 있고, 색인 밖 인용은 날조로 잡힌다
    ctx = triage_context(warm)
    assert "case:aaa" in ctx and "만들어 쓰지" in ctx
    assert "수치는 원장에서만" in ctx, "회계 수치 금지 문구가 없다"
    assert "severity" in ctx and "바꾸지" in ctx
    ok = verify_citations(["case:aaa", "cause:unposted_fee_or_tax"], warm)
    assert ok["grounded"] is True and ok["unknown_refs"] == []
    bad = verify_citations(["case:aaa", "case:does_not_exist"], warm)
    assert bad["grounded"] is False and bad["unknown_refs"] == ["case:does_not_exist"]
    assert verify_citations([], warm)["uncited"] is True, "무인용이 통과했다"
    # 사례가 없는 Break 에 사례를 인용하면 걸린다
    assert verify_citations(["case:aaa"], empty)["unknown_refs"] == ["case:aaa"]
    print("  인용 검증 (날조 차단)      OK")

    # 6. **Aging/SLA** - material 4시간, high 24시간
    rows = [
        {"break_id": "b-material", "severity": "material", "status": "OPEN",
         "kind": "position_mismatch", "created_at": (now - timedelta(hours=9)).isoformat()},
        {"break_id": "b-high-soon", "severity": "high", "status": "OPEN",
         "kind": "price_mismatch", "created_at": (now - timedelta(hours=20)).isoformat()},
        {"break_id": "b-medium-ok", "severity": "medium", "status": "INVESTIGATING",
         "kind": "cost_mismatch", "created_at": (now - timedelta(hours=2)).isoformat()},
    ]
    aging = check_aging(rows, now=now)
    assert aging["total_open"] == 3 and aging["sla_breached"] is True
    assert aging["by_aging_status"]["OVERDUE"] == ["b-material"]
    assert aging["by_aging_status"]["DUE_SOON"] == ["b-high-soon"]      # 20/24 = 0.83 > 0.75
    assert aging["by_aging_status"]["WITHIN_SLA"] == ["b-medium-ok"]
    assert aging["overdue"][0]["overdue_hours"] == 5.0, aging["overdue"][0]
    assert aging["decided_by"] == "deterministic"
    # 기한 안이면 breached 가 아니다 - 게이트가 항상 켜져 있는 게 아니라는 확인
    calm = check_aging([rows[2]], now=now)
    assert calm["sla_breached"] is False and calm["overdue"] == []
    print("  Break Aging / SLA          OK")

    # 7. 모르는 severity 를 기한 없음으로 읽지 않는다 / naive 시각을 추측하지 않는다
    unknown = check_aging([{"break_id": "b-x", "severity": "critical", "status": "OPEN",
                            "created_at": (now - timedelta(days=30)).isoformat()}], now=now)
    assert unknown["unknown_sla"] == ["b-x"], unknown
    assert unknown["sla_breached"] is False   # 기한을 모르므로 넘겼다고 단정하지도 않는다
    raises(lambda: check_aging([{"break_id": "b", "severity": "high",
                                 "created_at": "2026-08-05T12:00:00"}], now=now), "naive 시각")
    raises(lambda: check_aging([{"break_id": "b", "severity": "high",
                                 "created_at": "어제"}], now=now), "읽을 수 없는 시각")
    raises(lambda: load_ops(_HERE / "없는파일.yaml"), "없는 튜닝 파일")
    print("  SLA 미상 fail-closed       OK")

    # 8. reconciliation.py 가 만든 Break 를 그대로 받는다 (계약 연결 확인)
    from reconciliation import FillRecord, reconcile_fills
    from decimal import Decimal
    from contracts import Side

    real = reconcile_fills([], [FillRecord(instrument_id=uuid4(), side=Side.BUY,
                                           quantity=Decimal("50"), price=Decimal("70000"),
                                           event_time=now, broker_fill_id="bf9", ref="e9")])
    assert real.breaks and real.breaks[0].kind == "external_only_fill"
    triaged = triage(real.breaks[0], corpus, recon_type="fill", ops=ops)
    assert triaged["severity"] == "material" and triaged["escalates"] is True
    assert any(c["id"] == "cause:manual_broker_trade" for c in triaged["cause_candidates"])
    print("  reconciliation 연결        OK")

    # 9. 실 DB 해소 이력 조회 (읽기 전용)
    if os.environ.get("DATABASE_URL"):
        try:
            from dotenv import load_dotenv
            load_dotenv(Path.cwd() / ".env")
        except ModuleNotFoundError:
            pass
        from repository import LedgerRepository

        repo = LedgerRepository.from_env()
        if repo is None:
            print("  실 DB 조회                 skip - 연결 없음")
        else:
            fund_id, _ = repo.bootstrap("ACC01-PAPER", "MAIN")
            db_corpus = load_corpus(repo, fund_id)
            print(f"  실 DB 해소 이력 조회       OK (사례 {len(db_corpus)}건)")
    else:
        print("  실 DB 조회                 skip - DATABASE_URL 없음")

    print("ok - Break Triage 9개 영역 점검 통과 (판정은 reconciliation.py, 여기는 근거·Aging)")
