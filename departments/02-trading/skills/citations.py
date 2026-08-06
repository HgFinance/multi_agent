#!/usr/bin/env python3
"""직원 인용 검증. 색인 밖 근거 id 를 날조로 잡는다 — 결정론, LLM 없음.

소유: 도현 (트레이딩본부)
근거: skills/agentic-rag 의 인용 검증 원칙(LLM 은 서술, 검증은 Python),
      CLAUDE.md 개발 원칙 9번(실패 시 진입 차단 방향)

직원이 낸 `evidence_refs` 를 실제로 받은 evidence 와 대조한다. 다섯 네임스페이스:

  ls:     ls:CSPAT00701                        브로커 규칙 TR 색인
  tca:    tca:momentum/BUY/mid/ls-live         과거 집행 기억 그룹 (adapter 포함)
  state:  state:INTENT:APPROVED->READY_TO_SUBMIT   OMS 상태 전이
  cert:   cert:FUTURE/risk                     파생 Certification 서명
  claim:  claim:fact:3                         Research Packet Claim 색인

`tca:` 형식에 adapter 를 넣은 것은 의도다 — **인용 자체가 시뮬레이션 출처를 드러낸다.**
`tca:.../paper` 를 근거로 쓴 서술은 읽는 사람이 바로 Paper 근거임을 안다.

**검증 규칙 넷.** 이 규칙이 기존 계약 테스트를 깨느냐 마느냐를 가른다.

  1. **알려진 접두사만 검사한다.** 모르는 접두사(`test:evidence` 등)는 ignored_refs 로
     분류하고 escalate 하지 않는다. 우리가 안 준 근거를 우리 색인으로 판정할 수 없다.
  2. **인용 없음은 escalate 사유가 아니다.** uncited 로 기록만 한다. 근거를 붙일 자리가
     없는 직원도 있고, 무인용을 실패로 치면 모든 직원이 억지 인용을 만든다.
  3. **알려진 접두사의 날조는 escalate.** 하나라도 색인 밖이면 그 직원 보고는
     DEGRADED 로 떨어지고 executed 에서 빠진다. 승인 방향으로 fallback 하지 않는다.
  4. **근거 소스를 못 읽으면 grounded=False.** 검증기가 죽어서 통과가 되지 않는다.

자체 점검: python departments/02-trading/skills/citations.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

_HERE = Path(__file__).resolve().parent
_DEPT = _HERE.parent
for _p in (str(_DEPT / "execution"), str(_DEPT / "contracts"), str(_DEPT / "capability")):
    if _p not in sys.path:
        sys.path.append(_p)

from broker_rules import BrokerRuleError  # noqa: E402
from broker_rules import verify_citations as verify_rule_citations  # noqa: E402
from contracts import BrokerOrderState, IntentState, can_transition  # noqa: E402
from derivatives import CERTIFICATION_REQUIRED, DERIVATIVE_CERTIFIERS  # noqa: E402

# 우리가 근거를 준 네임스페이스. 여기 없는 접두사는 검사 대상이 아니다.
KNOWN_PREFIXES = ("ls:", "tca:", "state:", "cert:", "claim:")

_STATE_MACHINES = {"INTENT": IntentState, "BROKER": BrokerOrderState}


def _namespace(ref: str) -> str | None:
    for prefix in KNOWN_PREFIXES:
        if ref.startswith(prefix):
            return prefix[:-1]
    return None


# ── 네임스페이스별 검증 (전부 결정론) ─────────────────────────────────────
def _check_ls(refs: Sequence[str], _evidence: Mapping[str, Any]) -> list[str]:
    """브로커 규칙 TR 색인. broker_rules 의 기존 검증기를 그대로 쓴다."""
    if not refs:
        return []
    try:
        return list(verify_rule_citations(refs)["unknown_refs"])
    except BrokerRuleError:
        # 규칙 문서를 못 읽으면 통과시키지 않는다 - 전부 미확인 처리한다.
        return sorted(refs)


def _check_tca(refs: Sequence[str], evidence: Mapping[str, Any]) -> list[str]:
    """집행 기억 그룹. evidence 가 실제로 그 그룹을 냈고 표본이 충분해야 한다."""
    groups = ((evidence.get("tca_memory") or {}).get("groups") or {})
    unknown = []
    for ref in refs:
        key = ref[len("tca:"):]
        group = groups.get(key)
        if not isinstance(group, Mapping) or not group.get("sufficient"):
            unknown.append(ref)
    return sorted(unknown)


def _check_state(refs: Sequence[str], _evidence: Mapping[str, Any]) -> list[str]:
    """상태 전이. `contracts.can_transition()` 이 유일한 판정자다."""
    unknown = []
    for ref in refs:
        body = ref[len("state:"):]
        machine, _, arrow = body.partition(":")
        source, sep, target = arrow.partition("->")
        enum = _STATE_MACHINES.get(machine.upper())
        if enum is None or not sep:
            unknown.append(ref)
            continue
        try:
            ok = can_transition(enum(source.strip()), enum(target.strip()))
        except ValueError:
            ok = False   # 존재하지 않는 상태 이름
        if not ok:
            unknown.append(ref)
    return sorted(unknown)


def _check_cert(refs: Sequence[str], evidence: Mapping[str, Any]) -> list[str]:
    """파생 Certification. 상품군과 서명자가 둘 다 실재해야 한다."""
    cert = evidence.get("certification") or {}
    known_signers = set(cert.get("certified_by") or []) | set(cert.get("missing") or [])
    unknown = []
    for ref in refs:
        asset, _, signer = ref[len("cert:"):].partition("/")
        if asset.upper() not in CERTIFICATION_REQUIRED or signer not in DERIVATIVE_CERTIFIERS:
            unknown.append(ref)
            continue
        # evidence 가 서명 현황을 냈다면 그 안에 있어야 한다. 안 냈으면
        # 상품군·서명자 유효성까지만 보고 통과시킨다(없는 것을 만들지는 않았으므로).
        if known_signers and signer not in known_signers:
            unknown.append(ref)
    return sorted(unknown)


def _check_claim(refs: Sequence[str], evidence: Mapping[str, Any]) -> list[str]:
    """Research Packet Claim 색인. 토론이 만든 id 밖은 날조다."""
    claims = set((evidence.get("debate") or {}).get("claims") or [])
    return sorted({r for r in refs if r[len("claim:"):] not in claims})


_CHECKERS = {"ls": _check_ls, "tca": _check_tca, "state": _check_state,
             "cert": _check_cert, "claim": _check_claim}


def verify_refs(refs: Iterable[str], evidence: Mapping[str, Any]) -> dict[str, Any]:
    """직원 인용을 실제 evidence 와 대조한다."""
    refs = [str(r) for r in (refs or [])]
    by_namespace: dict[str, list[str]] = {}
    ignored: list[str] = []
    for ref in refs:
        namespace = _namespace(ref)
        if namespace is None:
            ignored.append(ref)     # 우리가 안 준 근거 - 우리 색인으로 판정하지 않는다
        else:
            by_namespace.setdefault(namespace, []).append(ref)

    unknown: list[str] = []
    for namespace, group in by_namespace.items():
        unknown += _CHECKERS[namespace](group, evidence)

    checked = [r for group in by_namespace.values() for r in group]
    return {
        "refs": refs,
        "by_namespace": {k: sorted(v) for k, v in sorted(by_namespace.items())},
        "checked_refs": sorted(checked),
        "ignored_refs": sorted(ignored),
        "unknown_refs": sorted(set(unknown)),
        # 검사 대상 인용이 하나도 없다 - 실패가 아니라 사실이다(규칙 2번).
        "uncited": not checked,
        "grounded": bool(checked) and not unknown,
        "decided_by": "deterministic",
    }


def apply_citation_checks(result: dict[str, Any], *,
                          evidence_by_worker: Mapping[str, Mapping[str, Any]]
                          ) -> dict[str, Any]:
    """직원 보고에 인용 검증을 적용한다. 날조가 있으면 그 직원만 escalate 한다."""
    for report in result.get("workers", []):
        worker_id = report.get("worker_id")
        evidence = evidence_by_worker.get(worker_id)
        if evidence is None:
            continue      # 근거를 안 받은 직원은 검증 대상이 아니다
        output = report.get("output") or {}
        checked = verify_refs(output.get("evidence_refs") or [], evidence)
        report["evidence_citations"] = checked
        if not checked["unknown_refs"]:
            continue
        # 날조된 근거로 쓴 서술은 채택하지 않는다.
        output["escalate"] = True
        report["status"] = "DEGRADED"
        result["degraded"] = True
        if worker_id not in result.setdefault("failed", []):
            result["failed"].append(worker_id)
        if worker_id in result.get("executed", []):
            result["executed"].remove(worker_id)
    return result


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    EVIDENCE = {
        "debate": {"claims": ["fact:0", "fact:1", "invalid:0"]},
        "tca_memory": {"groups": {
            "momentum/BUY/mid/ls-live": {"samples": 25, "sufficient": True},
            "value/SELL/small/paper": {"samples": 3, "sufficient": False},
        }},
        "certification": {"certified_by": ["broker"], "missing": ["risk", "accounting"]},
    }

    # 1. **모르는 접두사는 무시한다** - 계약 테스트 호환의 생명줄
    #    tests/test_worker_architecture.py 의 _fake_worker_llm 이 test:evidence 를 낸다.
    fake = verify_refs(["test:evidence"], EVIDENCE)
    assert fake["ignored_refs"] == ["test:evidence"], fake
    assert fake["unknown_refs"] == [] and fake["uncited"] is True
    assert fake["grounded"] is False   # 검사한 인용이 없으니 grounded 는 아니다
    print("  모르는 접두사 무시           OK")

    # 2. **인용 없음은 escalate 사유가 아니다**
    none = verify_refs([], EVIDENCE)
    assert none["uncited"] is True and none["unknown_refs"] == []
    result = {"workers": [{"worker_id": "w", "status": "COMPLETED",
                           "output": {"summary": "s", "evidence_refs": [], "escalate": False}}],
              "executed": ["w"], "failed": [], "degraded": False}
    out = apply_citation_checks(result, evidence_by_worker={"w": EVIDENCE})
    assert out["degraded"] is False and out["executed"] == ["w"]
    assert out["workers"][0]["output"]["escalate"] is False
    print("  무인용 통과                  OK")

    # 3. ls: 브로커 규칙 (기존 검증기 재사용)
    ok_ls = verify_refs(["ls:CSPAT00701", "ls:t0424"], EVIDENCE)
    assert ok_ls["unknown_refs"] == [] and ok_ls["grounded"] is True
    bad_ls = verify_refs(["ls:CSPAT99999"], EVIDENCE)
    assert bad_ls["unknown_refs"] == ["ls:CSPAT99999"] and bad_ls["grounded"] is False
    print("  ls: 브로커 규칙              OK")

    # 4. state: 상태 전이 - can_transition 이 유일한 판정자다
    assert verify_refs(["state:INTENT:APPROVED->READY_TO_SUBMIT"], EVIDENCE)["grounded"] is True
    assert verify_refs(["state:BROKER:SUBMITTED->ACKNOWLEDGED"], EVIDENCE)["grounded"] is True
    for bad in ("state:INTENT:REJECTED->READY_TO_SUBMIT",   # 종단에서 나가는 전이
                "state:BROKER:CREATED->FILLED",             # 존재하지 않는 간선
                "state:INTENT:APPROVED->ACKNOWLEDGED",      # 머신을 섞었다
                "state:GHOST:A->B",                         # 없는 머신
                "state:INTENT:NOPE->EXPIRED",               # 없는 상태
                "state:INTENT:APPROVED"):                   # 화살표 없음
        assert verify_refs([bad], EVIDENCE)["unknown_refs"] == [bad], bad
    print("  state: 전이표 검증           OK")

    # 5. tca: 집행 기억 - 표본이 부족한 그룹은 근거가 아니다
    assert verify_refs(["tca:momentum/BUY/mid/ls-live"], EVIDENCE)["grounded"] is True
    thin = verify_refs(["tca:value/SELL/small/paper"], EVIDENCE)
    assert thin["unknown_refs"] == ["tca:value/SELL/small/paper"], thin
    assert verify_refs(["tca:없는/그룹/x/y"], EVIDENCE)["unknown_refs"] != []
    # evidence 가 tca 를 아예 안 냈으면 어떤 tca 인용도 통과 못 한다
    assert verify_refs(["tca:momentum/BUY/mid/ls-live"], {})["unknown_refs"] != []
    print("  tca: 집행 기억 그룹          OK")

    # 6. cert: 상품군과 서명자가 둘 다 실재해야 한다
    assert verify_refs(["cert:FUTURE/risk"], EVIDENCE)["grounded"] is True
    assert verify_refs(["cert:EQUITY/risk"], EVIDENCE)["unknown_refs"] == ["cert:EQUITY/risk"]
    assert verify_refs(["cert:FUTURE/ceo"], EVIDENCE)["unknown_refs"] == ["cert:FUTURE/ceo"]
    print("  cert: Certification          OK")

    # 7. claim: 토론 Claim 색인 밖은 날조
    assert verify_refs(["claim:fact:0"], EVIDENCE)["grounded"] is True
    assert verify_refs(["claim:fact:99"], EVIDENCE)["unknown_refs"] == ["claim:fact:99"]
    assert verify_refs(["claim:fact:0"], {})["unknown_refs"] == ["claim:fact:0"]
    print("  claim: Claim 색인            OK")

    # 8. 날조가 있으면 그 직원만 escalate 된다
    dirty = {"workers": [
        {"worker_id": "bad", "status": "COMPLETED",
         "output": {"summary": "s", "evidence_refs": ["ls:CSPAT99999", "test:x"],
                    "escalate": False}},
        {"worker_id": "good", "status": "COMPLETED",
         "output": {"summary": "s", "evidence_refs": ["claim:fact:1"], "escalate": False}},
    ], "executed": ["bad", "good"], "failed": [], "degraded": False}
    checked = apply_citation_checks(dirty, evidence_by_worker={"bad": EVIDENCE, "good": EVIDENCE})
    assert checked["failed"] == ["bad"] and checked["executed"] == ["good"]
    assert checked["degraded"] is True
    assert checked["workers"][0]["output"]["escalate"] is True
    assert checked["workers"][1]["output"]["escalate"] is False, "멀쩡한 직원까지 escalate 됐다"
    # 모르는 접두사는 unknown 에 안 들어간다
    assert checked["workers"][0]["evidence_citations"]["ignored_refs"] == ["test:x"]
    print("  날조 -> 해당 직원만 escalate OK")

    # 9. 근거를 안 받은 직원은 검증 대상이 아니다
    untouched = {"workers": [{"worker_id": "plain", "status": "COMPLETED",
                              "output": {"summary": "s", "evidence_refs": ["ls:CSPAT99999"],
                                         "escalate": False}}],
                 "executed": ["plain"], "failed": [], "degraded": False}
    same = apply_citation_checks(untouched, evidence_by_worker={})
    assert same["degraded"] is False and same["executed"] == ["plain"]
    assert "evidence_citations" not in same["workers"][0]
    print("  근거 미수령 직원 제외        OK")

    print("ok - 직원 인용 검증 9개 영역 점검 통과 "
          f"(네임스페이스 {len(KNOWN_PREFIXES)}개, 모르는 접두사는 무시)")
