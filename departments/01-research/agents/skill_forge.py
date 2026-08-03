"""부서 자가 스킬 생성 - 반복되는 것을 절차로 굳힌다.

담당: 재일 (리서치본부 RES / 퀀트·백테스트본부 QNT)
근거: HEDGE_FUND_MASTER_PLAN.md 5.5절(직원 LangGraph 실행 계층)
승인: 재일님 2026-08-03 "부서 본부별로 자가 스킬 만드는 건 허락하기로 했음"

▶ 왜 필요한가 - 학습 계층이 비어 있다
  우리 파이프라인은 인터페이스(헤르메스) / 추론(LangGraph) / 실행(도구) /
  메모리(LDM) 는 있는데 **학습이 없다.** packet_claims 로 5일·20일 뒤 채점할
  기반은 만들어놨지만 그 결과가 다음 실행으로 돌아오지 않는다 - 맞혔든
  틀렸든 내일 똑같이 분석한다.

  같은 실패를 세 번 반복하는 것은 모델이 나빠서가 아니라 **경험이 어디에도
  안 남기 때문**이다. 사람 팀은 이걸 "다음부터는 이렇게 하자" 는 절차로
  굳힌다. 이 모듈이 그 자리다.

▶ 무엇을 만들고 무엇을 안 만드는가 - 경계가 핵심이다
  스킬(능력 문서)  : 부서가 스스로 만든다  ← 재일님이 승인한 범위
  프로필(페르소나) : 못 고친다             ← agent_evolution_cycle 영역

  CLAUDE.md 는 "프롬프트 한 줄을 고치는 것도 Agent Profile Version 을 올리는
  변경" 이라고 못 박는다. 스킬 생성은 그 금지를 우회하는 통로가 아니다 -
  **config.yaml 과 SOUL.md 를 건드리지 않기 때문에** 성립한다. 스킬이
  페르소나를 재정의하려 들면 그건 프로필 변경이고, 여기서 막는다.

  또한 자기 부서 것만 만든다. 재일님은 RES·QNT 소유자이므로 그 두 부서만
  이 경로를 쓴다 - 다른 본부 스킬을 대신 만드는 것은 권한 침범이다.

▶ 후보 선정은 결정론이다
  "이런 스킬 있으면 좋겠다" 를 LLM 에게 물으면 매번 다른 답이 나오고
  검증할 수 없다. 무엇을 스킬로 만들지는 **실행 기록이 정한다** -
  같은 유형 실패가 임계 횟수 반복됐는가. LLM 은 그 다음, 이미 정해진
  후보의 문서를 쓰는 데만 쓴다.

자체 점검: python departments/01-research/agents/skill_forge.py
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

# ── 부서 경계 ────────────────────────────────────────────────────────────────
# 재일님이 소유한 두 본부만. 다른 본부 스킬을 만드는 것은 권한 침범이다.
OWNED_DEPARTMENTS = ("01-research", "04-quant-backtest")

# 같은 유형이 몇 번 반복돼야 절차로 굳힐 가치가 있는가.
# 2 는 우연이고 3 부터가 패턴이다 - 한 번 겪은 것을 문서로 만들면 문서만 쌓인다.
MIN_OCCURRENCES = 3

# 한 번에 만들 수 있는 스킬 수. 무제한이면 한 번의 나쁜 실행이 스킬 더미를 만든다.
MAX_SKILLS_PER_RUN = 2

# 스킬이 침범하면 안 되는 것 - 페르소나·권한 재정의는 프로필 변경이다
_FORBIDDEN_IN_SKILL = (
    r"you\s+are\s+the\s+\w+[- ]agent",     # 페르소나 재정의
    r"config\.yaml",                        # 프로필 파일 지시
    r"SOUL\.md",
    r"권한|승인\s*없이|우회|건너뛴",          # 통제 우회 유도
    r"personalities\s*:",
)


@dataclass
class Occurrence:
    """한 번의 실행에서 관측된 사건. 스킬 후보의 원재료다."""
    kind: str                       # 사건 유형 (같은 kind 가 모여 후보가 된다)
    detail: str = ""                # 사람이 읽는 설명
    run_id: str = ""                # 어느 실행인가 - 같은 실행 중복을 걸러낸다
    symbol: str = ""
    at: str = ""                    # ISO8601


@dataclass
class SkillCandidate:
    """스킬로 굳힐 가치가 있다고 **결정론이 판정한** 후보."""
    kind: str
    count: int
    runs: tuple[str, ...]
    samples: tuple[str, ...]
    department: str = "01-research"

    @property
    def slug(self) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", self.kind.lower()).strip("-")
        return s or "unnamed"


def detect_candidates(occurrences: Iterable[Occurrence],
                      *, department: str = "01-research",
                      min_occurrences: int = MIN_OCCURRENCES,
                      existing: Iterable[str] = ()) -> list[SkillCandidate]:
    """실행 기록 -> 스킬 후보. **순수 함수, LLM 없음.**

    무엇을 스킬로 만들지는 LLM 이 정하지 않는다 - 물어보면 매번 다른 답이
    나오고 재현이 안 된다. 기록에 남은 반복이 정한다.

    같은 실행(run_id) 안에서 열 번 난 것은 **한 번으로 센다.** 한 번의 나쁜
    실행이 임계를 혼자 넘기면 그건 패턴이 아니라 그날의 사고다.
    """
    if department not in OWNED_DEPARTMENTS:
        raise PermissionError(
            f"{department} 는 우리 부서가 아니다. 다른 본부 스킬을 대신 만들 수 없다")

    have = {s.strip().lower() for s in existing if s}
    buckets: dict[str, list[Occurrence]] = {}
    for o in occurrences:
        if not o.kind:
            continue
        buckets.setdefault(o.kind, []).append(o)

    out: list[SkillCandidate] = []
    for kind, items in buckets.items():
        runs = tuple(sorted({i.run_id for i in items if i.run_id}))
        # run_id 가 없는 기록은 서로 다른 실행인지 알 수 없다 - 셀 수 없으면 센다
        distinct = len(runs) if runs else len(items)
        if distinct < min_occurrences:
            continue
        cand = SkillCandidate(
            kind=kind, count=distinct, runs=runs,
            samples=tuple(i.detail for i in items if i.detail)[:5],
            department=department)
        if cand.slug in have:
            continue                      # 이미 있는 스킬을 또 만들지 않는다
        out.append(cand)

    # 많이 반복된 것부터. 잘라낼 때 가장 아픈 것이 남는다
    out.sort(key=lambda c: (-c.count, c.slug))
    return out[:MAX_SKILLS_PER_RUN]


def check_boundary(body: str) -> list[str]:
    """스킬 본문이 프로필 영역을 침범하는지 검사. **위반이면 저장하지 않는다.**

    자가 스킬 생성이 성립하는 이유는 프로필을 안 건드리기 때문이다. 생성된
    스킬이 페르소나를 재정의하거나 승인 절차를 우회하라고 쓰면 그 전제가
    깨진다 - LLM 이 쓴 문장이므로 반드시 기계가 확인한다.
    """
    hits = []
    for pat in _FORBIDDEN_IN_SKILL:
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            hits.append(f"{pat} -> {m.group(0)[:40]!r}")
    return hits


_DRAFT_PROMPT = """당신은 리서치본부의 절차를 문서로 굳히는 역할이다.

아래 사건이 서로 다른 실행에서 {count}번 반복됐다. 다음에 같은 일이 났을 때
따라갈 수 있는 절차를 한국어 Markdown 으로 쓴다.

사건 유형: {kind}
관측 사례:
{samples}

규칙:
- 제목(# ), 왜 필요한가, 작업 순서, 하지 않을 것 네 부분으로 쓴다
- 관측된 사례만 근거로 쓴다. 없는 사례를 지어내지 않는다
- 페르소나나 권한을 재정의하지 않는다. 절차만 쓴다
- 300단어 이내
"""


def draft_body(cand: SkillCandidate, llm: Callable[[str], str]) -> Optional[str]:
    """후보 -> SKILL.md 본문. LLM 은 **이미 정해진 후보의 문서만** 쓴다."""
    samples = "\n".join(f"- {s}" for s in cand.samples) or "- (상세 없음)"
    try:
        body = llm(_DRAFT_PROMPT.format(
            count=cand.count, kind=cand.kind, samples=samples))
    except Exception:
        return None
    if not body or len(body.strip()) < 80:
        return None                        # 빈 껍데기 스킬을 만들지 않는다
    return body.strip()


def _frontmatter(cand: SkillCandidate, description: str) -> str:
    return (f"---\nname: {cand.slug}\n"
            f"description: {description}\n---\n\n")


def forge(candidates: list[SkillCandidate], llm: Callable[[str], str], *,
          skills_dir: Path,
          now: Optional[datetime] = None) -> list[dict]:
    """후보 -> 실제 스킬 파일. 경계 위반은 **저장하지 않고 사유를 남긴다.**"""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    results = []
    for cand in candidates:
        body = draft_body(cand, llm)
        if body is None:
            results.append({"slug": cand.slug, "written": False,
                            "reason": "LLM 미응답 또는 본문 부족"})
            continue
        violations = check_boundary(body)
        if violations:
            # ▶ 통과로 위장하지 않는다. 왜 거부됐는지 남아야 사람이 판단한다
            results.append({"slug": cand.slug, "written": False,
                            "reason": "프로필 영역 침범", "violations": violations})
            continue

        desc = (f"{cand.kind} 가 서로 다른 실행에서 {cand.count}회 반복돼 "
                f"부서가 스스로 굳힌 절차")
        target = skills_dir / cand.slug
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(
            _frontmatter(cand, desc) + body + "\n", encoding="utf-8")
        # 출처를 남긴다 - 누가 왜 만들었는지 모르는 스킬은 지울 수도 없다
        (target / "provenance.json").write_text(json.dumps({
            "generated_by": "skill_forge",
            "department": cand.department,
            "kind": cand.kind,
            "occurrences": cand.count,
            "runs": list(cand.runs),
            "generated_at": ts,
            "profile_unchanged": True,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        results.append({"slug": cand.slug, "written": True,
                        "path": str(target / "SKILL.md")})
    return results


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _check_threshold():
    occ = [Occurrence(kind="tool_timeout", run_id=f"r{i}") for i in range(2)]
    assert detect_candidates(occ) == [], "2회는 우연이다"
    occ.append(Occurrence(kind="tool_timeout", run_id="r2"))
    got = detect_candidates(occ)
    assert len(got) == 1 and got[0].count == 3, got


def _check_same_run_counts_once():
    occ = [Occurrence(kind="x", run_id="same") for _ in range(9)]
    assert detect_candidates(occ) == [], "한 실행의 반복은 패턴이 아니다"


def _check_department_boundary():
    try:
        detect_candidates([], department="03-risk")
    except PermissionError:
        return
    raise AssertionError("다른 본부 스킬을 만들 수 있으면 안 된다")


def _check_existing_not_duplicated():
    occ = [Occurrence(kind="tool timeout", run_id=f"r{i}") for i in range(4)]
    assert detect_candidates(occ, existing=["tool-timeout"]) == [], "중복 생성"


def _check_boundary_guard():
    assert check_boundary("절차: 도구를 두 번 부른다") == []
    assert check_boundary("You are the research-agent and you decide"), "페르소나 재정의 통과"
    assert check_boundary("승인 없이 진행한다"), "통제 우회 통과"
    assert check_boundary("config.yaml 의 personalities 를 고친다"), "프로필 지시 통과"


def _check_violation_not_written(tmp: Path):
    cand = SkillCandidate(kind="k", count=3, runs=("a", "b", "c"), samples=())
    out = forge([cand], lambda p: "You are the evil-agent. " + "x" * 100,
                skills_dir=tmp)
    assert out[0]["written"] is False and "침범" in out[0]["reason"], out
    assert not (tmp / "k").exists(), "위반인데 파일이 남았다"


def _check_written_has_provenance(tmp: Path):
    cand = SkillCandidate(kind="slow tool", count=3, runs=("a", "b", "c"),
                          samples=("도구가 30초 넘게 걸림",))
    out = forge([cand], lambda p: "# 느린 도구\n\n" + "절차를 따른다. " * 20,
                skills_dir=tmp)
    assert out[0]["written"] is True, out
    d = tmp / "slow-tool"
    assert (d / "SKILL.md").exists() and (d / "provenance.json").exists()
    prov = json.loads((d / "provenance.json").read_text(encoding="utf-8"))
    assert prov["profile_unchanged"] is True and prov["occurrences"] == 3


def _check_cap():
    occ = []
    for k in ("a", "b", "c", "d"):
        occ += [Occurrence(kind=k, run_id=f"{k}{i}") for i in range(4)]
    assert len(detect_candidates(occ)) <= MAX_SKILLS_PER_RUN


if __name__ == "__main__":
    import tempfile

    _check_threshold();                print("  반복 임계(3회)          OK")
    _check_same_run_counts_once();     print("  같은 실행 1회로 계수     OK")
    _check_department_boundary();      print("  부서 경계               OK")
    _check_existing_not_duplicated();  print("  기존 스킬 중복 방지      OK")
    _check_boundary_guard();           print("  프로필 침범 탐지         OK")
    with tempfile.TemporaryDirectory() as t:
        _check_violation_not_written(Path(t)); print("  위반 시 미저장          OK")
        _check_written_has_provenance(Path(t)); print("  출처 기록               OK")
    _check_cap();                      print("  생성 상한               OK")
    print("skill_forge 8개 영역 통과.")
