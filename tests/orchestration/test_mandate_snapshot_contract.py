"""Mandate 스냅샷을 부서가 실제로 읽게 하는 규약 테스트.

스냅샷을 root body에 싣는 것(`build_mandate_snapshot_block`)까지는
`tests/api/test_ceo_hermes_boundary.py`가 이미 지킨다. 여기가 지키는 건 그 다음
구간이다 - **쓰기만 하고 읽는 쪽이 없으면 스냅샷은 죽은 문자열이다.**

세 겹으로 본다:
1. 자식 body에 참조 지시문이 실리는가 (`build_scoped_task_body`)
2. Supervisor가 root를 보고 그 판단을 자식에게 전달하는가
3. 부서 Profile이 그 지시문을 읽으라고 적고 있는가 (SOUL.md)

3번이 없으면 1·2번은 부서 LLM이 무시해도 되는 잡음이므로 함께 고정한다.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from orchestration.adapters.ceo_supervisor import CeoSupervisorService
from orchestration.ceo_workflow_scope import (
    CEO_MANDATE_SNAPSHOT_MARKER,
    build_root_body,
    build_scoped_task_body,
    mandate_snapshot_present,
)
from tests.orchestration.test_ceo_supervisor import FakeClient

REPO_ROOT = Path(__file__).resolve().parents[2]

MANDATE = {
    "mandate_id": "m-1",
    "current_version": 3,
    "content_hash": "sha256:abc",
    "fund_id": "fund-1",
    "policy": {
        "risk_bounds": {
            "max_drawdown_pct": "0.15",
            "max_instrument_weight": "0.10",
            "currency": "KRW",
        }
    },
}

# 부서 SOUL.md에 이 규약이 있어야 하는 곳. CEO는 지시문을 **쓰는** 쪽이고
# 나머지 넷은 **읽는** 쪽이다. QA/quant/HR은 아직 Mandate를 소비하지 않으므로
# 넣지 않는다 - 안 쓰는 부서에 규약을 뿌리면 지켜지는지 확인할 수 없다.
MANDATE_READER_PROFILES = (
    "departments/01-research/hermes/SOUL.md",
    "departments/02-trading/hermes/SOUL.md",
    "departments/03-risk/hermes/SOUL.md",
    "departments/05-accounting-portfolio/hermes/SOUL.md",
)


class MandateSnapshotPresenceTest(unittest.TestCase):
    def test_presence_follows_the_real_root_body(self) -> None:
        """판별을 별도 플래그가 아니라 root body 자체에서 읽는다.

        플래그를 따로 들고 다니면 body에는 스냅샷이 없는데 플래그만 참인 상태가
        생긴다. 그러면 부서는 `kanban show`로 빈 카드를 읽고 한도를 추론한다.
        """

        with_mandate = build_root_body("q", "req-1", mandate=MANDATE)
        without = build_root_body("q", "req-1")

        self.assertTrue(mandate_snapshot_present(with_mandate))
        self.assertFalse(mandate_snapshot_present(without))
        self.assertFalse(mandate_snapshot_present(""))
        self.assertFalse(mandate_snapshot_present(None))  # type: ignore[arg-type]

    def test_mandate_without_active_version_is_not_present(self) -> None:
        """껍데기 Mandate(활성 Version 0)는 "한도 있음"이 아니다."""

        draft = {"mandate_id": "m-1", "current_version": 0, "policy": {}}
        self.assertFalse(mandate_snapshot_present(build_root_body("q", "r", mandate=draft)))


class ScopedTaskBodyMandateReferenceTest(unittest.TestCase):
    def test_reference_line_points_at_the_root_card(self) -> None:
        body = build_scoped_task_body(
            "research", "t_root123", role="primary", has_mandate=True
        )

        self.assertIn("mandate_snapshot=see_root_task_body", body)
        self.assertIn("root_task_id=t_root123", body)
        self.assertIn("kanban show t_root123", body)
        self.assertIn(CEO_MANDATE_SNAPSHOT_MARKER, body)

    def test_limit_values_are_never_copied_into_the_child(self) -> None:
        """자식은 위치만 받는다. 값 복사가 시작되면 단일 원본이 깨진다.

        요약·누락된 복사본으로 Research와 Risk가 다른 한도를 쓰는 상황이 이
        기능이 막으려는 바로 그 실패다.
        """

        root = build_root_body("q", "req-1", mandate=MANDATE)
        child = build_scoped_task_body("research", "t_root123", role="primary", has_mandate=True)

        self.assertIn("risk.max_drawdown_pct=0.15", root)
        for copied in ("risk.max_drawdown_pct", "0.15", "0.10", "sha256:abc", "mandate_version="):
            self.assertNotIn(copied, child)

    def test_absent_mandate_emits_no_line_at_all(self) -> None:
        """한도가 없으면 "root를 봐라"라고도 하지 않는다.

        빈 카드를 가리키면 부서 LLM이 헛읽고 없는 한도를 채울 여지가 생긴다.
        줄이 없는 상태가 "사용자 Mandate 없음"이라는 정확한 사실이다.
        """

        default_body = build_scoped_task_body("research", "t_root123", role="primary")
        explicit = build_scoped_task_body(
            "research", "t_root123", role="primary", has_mandate=False
        )

        for body in (default_body, explicit):
            self.assertNotIn("mandate_snapshot", body)
            self.assertNotIn(CEO_MANDATE_SNAPSHOT_MARKER, body)

    def test_reference_line_does_not_disturb_existing_scope_metadata(self) -> None:
        body = build_scoped_task_body(
            "research", "t_root123", role="qa", request_id="req-9",
            workflow_mode="binding", has_mandate=True,
        )

        self.assertIn("workflow_root_task_id=t_root123", body)
        self.assertIn("workflow_role=qa", body)
        self.assertIn("workflow_mode=binding", body)
        self.assertIn("request_id=req-9", body)
        self.assertTrue(body.rstrip().endswith("research"))


class SupervisorPropagatesMandateReferenceTest(unittest.TestCase):
    """Supervisor는 root를 이미 읽고 있다. 그 판단을 자식에게 넘기는지 본다."""

    def test_created_children_carry_the_reference_when_root_has_a_snapshot(self) -> None:
        client = FakeClient()
        client.root_body = build_root_body("q", "req-1", mandate=MANDATE)

        CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "e1", "task_id": "r", "kind": "completed"}
        )

        self.assertTrue(client.created)
        for created in client.created:
            self.assertIn("mandate_snapshot=see_root_task_body", created["body"])
            # 값이 아니라 위치만. root ID는 supervisor가 아는 실제 root여야 한다.
            self.assertNotIn("0.15", created["body"])

    def test_created_children_carry_nothing_when_root_has_no_snapshot(self) -> None:
        client = FakeClient()
        client.root_body = build_root_body("q", "req-1")

        CeoSupervisorService(client).handle_terminal_event(
            {"event_id": "e1", "task_id": "r", "kind": "completed"}
        )

        self.assertTrue(client.created)
        for created in client.created:
            self.assertNotIn("mandate_snapshot", created["body"])


class DepartmentProfileReadContractTest(unittest.TestCase):
    """지시문을 읽으라고 적힌 Profile이 없으면 배선만으론 아무 일도 안 일어난다."""

    def _soul(self, relative: str) -> str:
        path = REPO_ROOT / relative
        self.assertTrue(path.is_file(), f"missing profile: {relative}")
        return path.read_text(encoding="utf-8")

    def test_reader_departments_declare_how_to_find_the_snapshot(self) -> None:
        for relative in MANDATE_READER_PROFILES:
            with self.subTest(profile=relative):
                text = self._soul(relative)
                self.assertIn("mandate_snapshot=see_root_task_body", text)
                self.assertIn(CEO_MANDATE_SNAPSHOT_MARKER, text)
                self.assertIn("kanban show", text)

    def test_reader_departments_forbid_refetch_and_invented_defaults(self) -> None:
        """PIT(개발 원칙 5)와 "지어내지 않는다"(개발 원칙 9)를 문서에 고정한다."""

        for relative in MANDATE_READER_PROFILES:
            with self.subTest(profile=relative):
                text = self._soul(relative).casefold()
                self.assertIn("do not re-fetch a newer mandate", text)
                self.assertIn("default", text)

    def test_ceo_profile_writes_the_pointer_and_forbids_copying(self) -> None:
        text = self._soul("departments/00-ceo-office/hermes/SOUL.md")

        self.assertIn("mandate_snapshot=see_root_task_body", text)
        self.assertIn(CEO_MANDATE_SNAPSHOT_MARKER, text)
        # 마크다운 강조(`Do **not** copy`)를 사이에 두고도 잡히게 한다.
        self.assertRegex(text, re.compile(r"not\W{0,4}\s+copy the limit values", re.IGNORECASE))
        self.assertIn("do not re-fetch a newer mandate", text.casefold())


if __name__ == "__main__":
    unittest.main()
