#!/usr/bin/env python3
"""F19 ImprovementCandidate를 Notion HR DB(NOTION_HR_DB)에 올리는 Reporter Node의 업로드 로직.

담당: 영주. departments/03-risk/notion_reporter.py, departments/06-ai-qa-audit/notion_reporter.py와
같은 패턴 - 속성명·Select 값은 코드 출력을 그대로 쓴다.

2026-08-03 실측: NOTION_HR_DB에 실제로 연결해보니 이미 만들어진 DB가
docs/06-integrations/notion/NOTION_DEPARTMENT_DB_DESIGN.md 5절 "채용 후보" 스키마
그대로였다(속성: 후보 role_code, CEO 승인/IAM 생성/QA 독립검증 체크박스 3개,
hiring_priority_tier, 담당자, 서술, 원본 리포트, 생성 시각). Workforce Scorecard로
대체하려던 이전 버전은 이 실제 스키마와 맞지 않아 업로드가 400으로 거부됐다 - 이제
improvements/candidate.py의 ImprovementCandidate를 이 스키마에 맞춰 올린다.

매핑이 완전하지 않다는 걸 숨기지 않는다:
  - "QA 독립검증": 전이 Event에 qa_eval_run_id가 있으면 True. 우리 workflow.py의 승인
    게이트가 실제로 요구하는 근거이므로 정확하다.
  - "CEO 승인": candidate.status가 APPROVED 이상까지 갔으면 True로 본다. 다만 우리
    workflow.py는 "독립 승인자"만 요구하고 그 승인자가 실제로 CEO인지 구분하지 않는다
    (승인 게이트 1개가 QA 근거 + 독립 승인자 확인을 동시에 한다) - 그래서 이 체크박스는
    "누군가 독립적으로 승인했다"는 근사치다. 실제 CEO 승인 여부를 구분하려면
    workflow.py에 승인자 역할 필드를 추가해야 한다(지금 범위 밖).
  - "IAM 생성": 항상 False다. F19(자기 개선)는 Skill/Profile/Workflow/Agent Version을
    바꾸는 것이지 Identity를 만드는 게 아니다 - 그건 lifecycle/access.py(Y4)의 provision()
    소관이라 여기서 값을 지어내지 않는다.
  - "담당자"(people)는 보내지 않는다. Notion people 속성은 실제 Notion 계정 ID가
    필요한데 우리 author/actor 문자열("qa-department-hermes" 등)을 그 ID로 변환할
    방법이 없다 - 틀린 타입을 억지로 채우지 않는다.
  - "hiring_priority_tier"도 보내지 않는다. ImprovementCandidate에는 대응하는 값이
    없다(그건 워크포스 채용 우선순위 개념이고, F19는 기존 Agent 개선 후보라 다른 개념).

자격증명은 root .env가 아니라 ai-office/.dev.vars에서 읽는다(Risk/QA와 동일 근거).

Notion은 Projection일 뿐이다 - 이 모듈이 실패해도(미설정, 네트워크 오류 등) Candidate
상태 머신 판정은 절대 바뀌지 않는다. 모든 실패를 흡수하고 {"ok": False, ...}로만 기록한다.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from reporting import notion_rich_text_chunks

_IMPROVEMENTS_DIR = Path(__file__).resolve().parent / "improvements"
if str(_IMPROVEMENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_IMPROVEMENTS_DIR))

if TYPE_CHECKING:
    from candidate import ImprovementCandidate
    from workflow import CandidateEvent

_DEV_VARS = Path(__file__).resolve().parent.parent.parent / "ai-office" / ".dev.vars"
_NOTION_VERSION = "2022-06-28"

# candidate.py의 CandidateStatus 값 그대로 - APPROVED 이상까지 간 상태 (KEPT/ROLLED_BACK/
# RETIRED 도 한 번은 APPROVED를 거쳤을 것이므로 포함한다).
_APPROVED_OR_LATER = frozenset({
    "APPROVED", "DEPLOYED", "OBSERVING", "KEPT", "ROLLED_BACK", "RETIRED",
})


def _load_dev_vars() -> dict:
    if not _DEV_VARS.exists():
        return {}
    env = {}
    for line in _DEV_VARS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _post(path: str, body: dict, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"https://api.notion.com/v1/{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Notion-Version": _NOTION_VERSION,
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _rich_text(s) -> dict:
    return {"rich_text": notion_rich_text_chunks(s)}


def upload_candidate(
    candidate: "ImprovementCandidate", events: list["CandidateEvent"],
    *, report_md: str = "", env: dict | None = None,
) -> dict:
    """candidate(+전이 Event 이력)를 Notion HR DB에 1건 업로드한다. 절대 예외를 던지지 않는다."""
    env = env if env is not None else _load_dev_vars()
    token, db_id = env.get("NOTION_TOKEN"), env.get("NOTION_HR_DB")
    if not token or not db_id:
        return {"ok": False, "reason": "NOTION_TOKEN/NOTION_HR_DB 미설정 - 업로드 생략"}

    qa_verified = any(e.qa_eval_run_id for e in events)
    ceo_approved = candidate.status.value in _APPROVED_OR_LATER

    props = {
        "후보 role_code": {"title": [{"text": {"content": candidate.target_ref}}]},
        "QA 독립검증": {"checkbox": qa_verified},
        "CEO 승인": {"checkbox": ceo_approved},
        "IAM 생성": {"checkbox": False},  # F19는 Identity를 만들지 않는다 - lifecycle/access.py 소관
        "서술": _rich_text(candidate.expected_effect),
        "원본 리포트": _rich_text(report_md),
        "생성 시각": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
    }

    try:
        status, body = _post("pages", {"parent": {"database_id": db_id}, "properties": props}, token)
    except Exception as e:  # 네트워크 오류 등 - 절대 파이프라인을 죽이지 않는다
        return {"ok": False, "reason": f"업로드 예외: {e}"}
    if status == 200:
        return {"ok": True, "url": body.get("url")}
    return {"ok": False, "status": status, "error": body.get("message", body)}


# ── 자체 점검 (네트워크 없음) ──────────────────────────────────────────────
def _check_missing_config_skips_without_network():
    from candidate import ImprovementCandidate

    def _boom(*a, **k):
        raise AssertionError("설정 없는데 네트워크 호출을 시도했다")
    orig = _post
    globals()["_post"] = _boom
    try:
        c = ImprovementCandidate(
            candidate_id="ic-1", author="qa-department-hermes", target_type="PROFILE",
            target_ref="agent-citation-checker", target_current_version=3,
            evidence_ids=["finding-101"], expected_effect="인용 누락 오탐 감소",
            risk_class="MEDIUM", rollback_target_version=3,
        )
        result = upload_candidate(c, [], env={})
        assert result == {"ok": False, "reason": "NOTION_TOKEN/NOTION_HR_DB 미설정 - 업로드 생략"}
    finally:
        globals()["_post"] = orig
    print("  미설정 시 네트워크 미호출   OK")


def _check_payload_shape():
    from candidate import ImprovementCandidate
    from workflow import CandidateEvent, CandidateStatus

    captured = {}

    def _fake_post(path, body, token):
        captured["path"], captured["body"], captured["token"] = path, body, token
        return 200, {"url": "https://notion.so/fake"}

    orig = _post
    globals()["_post"] = _fake_post
    try:
        c = ImprovementCandidate(
            candidate_id="ic-1", author="qa-department-hermes", target_type="PROFILE",
            target_ref="agent-citation-checker", target_current_version=3,
            evidence_ids=["finding-101"], expected_effect="인용 누락 오탐 감소",
            risk_class="MEDIUM", rollback_target_version=3, status=CandidateStatus.APPROVED,
        )
        now = datetime(2026, 8, 3, tzinfo=timezone.utc)
        events = [CandidateEvent(candidate_id="ic-1", sequence=1, from_status=CandidateStatus.PENDING_APPROVAL,
                                  to_status=CandidateStatus.APPROVED, actor="ceo-office-hermes", reason="ok",
                                  occurred_at=now, qa_eval_run_id="eval-1")]
        result = upload_candidate(c, events, env={"NOTION_TOKEN": "tok", "NOTION_HR_DB": "db1"})
        assert result == {"ok": True, "url": "https://notion.so/fake"}
        props = captured["body"]["properties"]
        assert captured["body"]["parent"]["database_id"] == "db1"
        assert props["후보 role_code"]["title"][0]["text"]["content"] == "agent-citation-checker"
        assert props["QA 독립검증"]["checkbox"] is True
        assert props["CEO 승인"]["checkbox"] is True
        assert props["IAM 생성"]["checkbox"] is False
        assert "담당자" not in props and "hiring_priority_tier" not in props
    finally:
        globals()["_post"] = orig
    print("  업로드 Payload 구성        OK")


def _check_qa_and_ceo_flags_reflect_evidence():
    from candidate import ImprovementCandidate
    from workflow import CandidateStatus

    captured = {}

    def _fake_post(path, body, token):
        captured["body"] = body
        return 200, {"url": "https://notion.so/fake"}

    orig = _post
    globals()["_post"] = _fake_post
    try:
        c = ImprovementCandidate(
            candidate_id="ic-2", author="qa-department-hermes", target_type="PROFILE",
            target_ref="agent-x", target_current_version=1, evidence_ids=["f1"],
            expected_effect="e", risk_class="LOW", rollback_target_version=1,
            status=CandidateStatus.EVALUATING,
        )
        upload_candidate(c, [], env={"NOTION_TOKEN": "tok", "NOTION_HR_DB": "db1"})
        props = captured["body"]["properties"]
        assert props["QA 독립검증"]["checkbox"] is False
        assert props["CEO 승인"]["checkbox"] is False, "EVALUATING 인데 승인됐다고 표시됐다"
    finally:
        globals()["_post"] = orig
    print("  진행 중 후보는 체크박스 미표기  OK")


if __name__ == "__main__":
    print("agent-workforce notion_reporter 자체 점검 (네트워크 없음)")
    _check_missing_config_skips_without_network()
    _check_payload_shape()
    _check_qa_and_ceo_flags_reflect_evidence()
    print("notion_reporter 3개 영역 통과")
