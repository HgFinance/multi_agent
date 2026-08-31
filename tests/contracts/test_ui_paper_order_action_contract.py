"""UI가 손으로 베낀 PAPER 주문 action 목록이 백엔드 계약과 같은지 대조한다.

▶ 왜 이 테스트가 있나 (2026-08-31)
  PAPER 주문 action은 세 곳에 적힌다.
    1. `orchestration/contracts/user_paper_order.py` 의 `DirectiveAction` (정본)
    2. `ai-office/app/lib/ceoClient.ts` 의 `PaperOrderWorkflowStatus.action`
       (화면이 실제로 쓰는 경로)
    3. `ai-office/app/lib/paperOrderClient.ts` 의 `PaperDirectiveAction` 타입과
       `parsePaperDirective` 런타임 허용목록

  2·3 은 1을 손으로 베낀 사본인데 아무도 대조하지 않았다. 그래서 바스켓 주문
  (`PLACE_BASKET`)을 백엔드에 추가한 뒤에도 UI 두 곳은 세 개짜리 목록으로
  남았다. `getJson` 이 `body as T` 캐스팅만 하는 탓에 타입이 어긋나도
  컴파일 타임에도 런타임에도 아무 신호가 없었고, 드리프트는 사람이 소스를
  읽어야만 보이는 상태로 방치됐다.

  문서나 리뷰로는 안 풀린다 - 사본은 또 낡는다. 그래서 목록을 **실행되는
  대조**에 건다. 백엔드에 action을 추가하면 여기서 걸리고, 세 곳을 같이
  고치게 된다.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from orchestration.contracts.user_paper_order import DirectiveAction

ROOT = Path(__file__).resolve().parents[2]
CEO_CLIENT = ROOT / "ai-office" / "app" / "lib" / "ceoClient.ts"
PAPER_ORDER_CLIENT = ROOT / "ai-office" / "app" / "lib" / "paperOrderClient.ts"

_QUOTED = re.compile(r'"([A-Z_]+)"')


def _quoted_tokens(region: str) -> set[str]:
    return set(_QUOTED.findall(region))


def _region(source: str, pattern: str, *, label: str) -> str:
    match = re.search(pattern, source, re.DOTALL)
    if match is None:
        raise AssertionError(
            f"{label}: 계약 대조 지점을 찾지 못했다. 리팩터링으로 형태가 바뀌었다면 "
            "이 테스트의 정규식도 같이 고쳐라 - 대조를 지우지 마라."
        )
    return match.group(1)


class UiPaperOrderActionContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.expected = {action.value for action in DirectiveAction}
        self.ceo_source = CEO_CLIENT.read_text(encoding="utf-8")
        self.paper_source = PAPER_ORDER_CLIENT.read_text(encoding="utf-8")

    def test_backend_enum_is_the_four_known_actions(self) -> None:
        # 목록이 늘면 아래 세 대조가 UI를 같이 고치도록 강제한다.
        self.assertEqual(
            self.expected,
            {
                "PLACE_ORDER",
                "PLACE_BASKET",
                "SELL_ALL",
                "SELL_POSITION",
                "CANCEL_ALL",
            },
            "DirectiveAction이 바뀌었다. ai-office의 사본 세 곳을 같이 고쳐라.",
        )

    def test_live_status_type_matches_backend(self) -> None:
        """화면이 실제로 쓰는 경로(`paperOrderWorkflowStatus`)의 타입."""

        region = _region(
            self.ceo_source,
            r"export type PaperOrderWorkflowStatus = \{(.*?)\n\};",
            label="ceoClient.ts PaperOrderWorkflowStatus",
        )
        action_field = _region(
            region, r"\n  action:(.*?);", label="ceoClient.ts action 필드"
        )
        self.assertEqual(_quoted_tokens(action_field), self.expected)
        self.assertIn(
            "null",
            action_field,
            "요청이 해석 단계면 action이 아직 없다. null을 유지해라.",
        )

    def test_directive_action_type_matches_backend(self) -> None:
        region = _region(
            self.paper_source,
            r"export type PaperDirectiveAction =(.*?);",
            label="paperOrderClient.ts PaperDirectiveAction",
        )
        self.assertEqual(_quoted_tokens(region), self.expected)

    def test_runtime_allowlist_matches_backend(self) -> None:
        """런타임 허용목록. 여기서 빠진 action은 응답을 통째로 예외로 만든다."""

        region = _region(
            self.paper_source,
            r"!\[([^\]]*)\]\.includes\(",
            label="paperOrderClient.ts parsePaperDirective 허용목록",
        )
        self.assertEqual(_quoted_tokens(region), self.expected)


if __name__ == "__main__":
    unittest.main()
