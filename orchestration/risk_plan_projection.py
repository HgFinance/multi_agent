"""Deterministic user-facing projection of a canonical Position Risk Plan."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def format_position_risk_plan(plan: Mapping[str, Any]) -> str:
    """Render exact plan fields without recalculating or changing any number."""

    action = str(plan.get("action") or "UNKNOWN")
    state = str(plan.get("state") or "PROPOSED")
    lines = [
        "🛡️ **동적 포지션 Risk Plan**",
        f"- 계획/상태: `{plan.get('risk_plan_id') or '미생성'}` · `{state}` / `{action}`",
        f"- 멘데이트 버전: `{plan.get('mandate_version_id') or '미확인'}`",
        f"- 시장 레짐·기준시각: `{plan.get('regime') or '미확인'}` · `{plan.get('as_of') or '미확인'}`",
    ]
    mandate_limits = plan.get("mandate_limits") or {}
    portfolio_usage = plan.get("portfolio_usage") or {}
    if isinstance(mandate_limits, Mapping) and isinstance(portfolio_usage, Mapping):
        lines.append(
            "- 멘데이트/실사용: 종목 비중 "
            f"`{portfolio_usage.get('current_instrument_weight', '미확인')}` / "
            f"`{mandate_limits.get('max_instrument_weight', '미확인')}`; Gross "
            f"`{portfolio_usage.get('current_gross_exposure', '미확인')}`"
        )
    if action == "PROPOSE":
        lines.extend(
            [
                f"- 허용 수량 상한: `{plan.get('quantity_cap')}`주 (현재 보유 `{plan.get('current_quantity', 0)}`주)",
                f"- 기준가 / 손절가 / 익절가: `{plan.get('entry_reference')}` / `{plan.get('stop_price')}` / `{plan.get('take_profit_price')}`",
                f"- 트레일링 활성가 / 거리: `{plan.get('trailing_activation_price')}` / `{plan.get('trailing_distance')}`",
                f"- 손실예산 / 손익비: `{plan.get('position_risk_amount')}` / `{plan.get('reward_risk_ratio')}`",
            ]
        )
    else:
        lines.append("- 손절가·익절가·수량: 데이터/제약 조건 미충족으로 생성하지 않음")
    reasons = plan.get("reason_codes") or []
    triggers = plan.get("review_triggers") or []
    lines.extend(
        [
            f"- 계산근거: `{plan.get('calculation_version') or '미확인'}` · `{', '.join(map(str, reasons)) or '없음'}`",
            f"- 만료시각: `{plan.get('expires_at') or '미확인'}`",
            f"- 재검토 조건: `{', '.join(map(str, triggers)) or '미확인'}`",
            f"- 데이터 품질: `{plan.get('data_quality') or '미확인'}`",
            "- 실행 상태: Risk 제안이며 주문 승인이 아닙니다. ACTIVE 전환 후에도 Trading의 PAPER 조건주문 변환과 Risk Engine 재검증이 필요합니다.",
        ]
    )
    return "\n".join(lines)


__all__ = ["format_position_risk_plan"]
