# 담당자: 영주 (CEO Office)
# 근거: HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F01(사용자 Mandate),
#       TEAM_YOUNGJU_CEO_HR_GUIDE.md 5.1(사용자 Mandate 변경), 10.1(Version/Effective Time)
#
# F01 의 Version/Effective Time 저장과 장중 변경 방향 판정.
#   - 기존 Version 을 덮어쓰지 않고 새 Version 을 만든다 (10.1, DDL 의 unique(mandate_id, version)).
#   - content_hash 로 무의미한 중복 Version 을 막는다 (DDL 의 unique(mandate_id, content_hash)).
#   - 장중 변경을 TIGHTEN(완화=더 안전) / LOOSEN(확대=더 위험) / NEUTRAL 로 분류한다:
#       * TIGHTEN  -> 즉시 적용        ("장중 Risk 완화는 즉시 적용")
#       * LOOSEN   -> 사용자 재승인 필요 ("장중 Risk 확대는 사용자 재승인")
#       * 혼합/모호 -> LOOSEN 취급 (CLAUDE.md 개발 원칙 9: 위험은 확대가 아니라 차단 방향)
#   - Mandate 통화가 Fund 기준 통화(accounting.funds.base_currency)와 일치하는지 저장 시점에
#     검증한다 (GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC 2.1 기준 자본 계약, 결정 4-A).
#     Fund 통화를 확인할 수 없으면 저장하지 않는다.
#
# 이 모듈은 DB 에 직접 접근하지 않는다. Repository 는 인터페이스로만 두고, 값 매핑
# (to_version_row)은 governance.mandate_versions 컬럼과 1:1로 맞춘다. asyncpg 연결은
# 이후 Y1 에서 이 인터페이스 구현체로 붙인다.

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from policy import MandatePolicy

# ---------------------------------------------------------------------------
# 변경 방향
# ---------------------------------------------------------------------------


class ChangeDirection(str, Enum):
    NEUTRAL = "NEUTRAL"      # Risk 수준 동일 (설명/비Risk 필드만 변경)
    TIGHTEN = "TIGHTEN"      # 더 안전 -> 즉시 적용 가능
    LOOSEN = "LOOSEN"        # 더 위험 -> 사용자 재승인 필요


def _num(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def classify_change(current: MandatePolicy, proposed: MandatePolicy) -> ChangeDirection:
    """current 대비 proposed 가 Risk 를 확대(LOOSEN)/완화(TIGHTEN)하는지 판정.

    하나라도 확대 방향이면 LOOSEN. 확대는 없고 완화만 있으면 TIGHTEN.
    Risk 관련 변화가 전혀 없으면 NEUTRAL.
    """
    loosen = False
    tighten = False

    c, p = current.risk_bounds, proposed.risk_bounds
    # 값이 커질수록 더 위험한 한도들.
    for field in (
        "max_instrument_weight",
        "max_sector_weight",
        "max_gross_exposure",
        "max_daily_loss",
    ):
        cv, pv = _num(getattr(c, field)), _num(getattr(p, field))
        if pv > cv:
            loosen = True
        elif pv < cv:
            tighten = True
    if p.max_concurrent_positions > c.max_concurrent_positions:
        loosen = True
    elif p.max_concurrent_positions < c.max_concurrent_positions:
        tighten = True

    # 허용 자산 추가 = 확대, 제거 = 완화.
    ca, pa = set(current.allowed_assets), set(proposed.allowed_assets)
    if pa - ca:
        loosen = True
    if ca - pa:
        tighten = True
    # 금지 자산 제거 = 확대, 추가 = 완화.
    cf, pf = set(current.forbidden_assets), set(proposed.forbidden_assets)
    if cf - pf:
        loosen = True
    if pf - cf:
        tighten = True

    # 자동 주문 전환: USER_APPROVAL -> AUTO = 확대, 반대 = 완화.
    cm = current.approval_rules.paper_order_mode
    pm = proposed.approval_rules.paper_order_mode
    if cm != pm:
        if pm.value == "AUTO":
            loosen = True
        else:
            tighten = True

    if loosen:
        return ChangeDirection.LOOSEN
    if tighten:
        return ChangeDirection.TIGHTEN
    return ChangeDirection.NEUTRAL


def requires_user_reapproval(direction: ChangeDirection) -> bool:
    """장중 적용 시 사용자 재승인이 필요한가. LOOSEN(확대)만 필요."""
    return direction == ChangeDirection.LOOSEN


# ---------------------------------------------------------------------------
# content_hash 와 Version Row 매핑
# ---------------------------------------------------------------------------


def _canonical_json(obj) -> str:
    """정렬·구분자 고정 JSON. Decimal 은 문자열로 직렬화해 재현성을 확보한다."""

    def default(o):
        if isinstance(o, Decimal):
            return format(o, "f")
        raise TypeError(f"직렬화 불가 타입: {type(o)}")

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=default)


def compute_content_hash(policy: MandatePolicy) -> str:
    """정책 내용의 안정적 해시. 같은 내용 -> 같은 hash (중복 Version 방지용)."""
    payload = policy.model_dump(mode="json")
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MandateVersionRow:
    """governance.mandate_versions 한 행. 컬럼명과 1:1."""

    mandate_id: str
    version: int
    objective_text: str
    objective: dict
    allowed_assets: list
    forbidden_assets: list
    universe_policy: dict
    risk_bounds: dict
    approval_rules: dict
    execution_rules: dict
    effective_from: datetime
    effective_to: datetime | None
    content_hash: str
    created_by: str | None


@dataclass(frozen=True)
class MandateDecisionRow:
    """governance.mandate_decisions 한 행. 컬럼명과 1:1.

    SQL 구현은 (mandate_id, version)을 mandate_versions 의 mandate_version_id FK 로
    해석한다 (in-memory 는 UUID 를 만들지 않고 자연키로 참조).
    """

    mandate_id: str
    version: int
    decision: str  # APPROVE | REJECT | SUSPEND | RETIRE (DDL check)
    conditions: dict
    reason: str | None
    approved_by: str | None
    trace_id: str
    decided_at: datetime


def to_version_row(
    *,
    mandate_id: str,
    version: int,
    policy: MandatePolicy,
    objective_text: str,
    objective: dict,
    effective_from: datetime,
    effective_to: datetime | None = None,
    execution_rules: dict | None = None,
    created_by: str | None = None,
) -> MandateVersionRow:
    """MandatePolicy 를 mandate_versions insert 용 Row 로 변환.

    DDL 제약과 동일하게 effective_to 는 effective_from 이후여야 한다.
    """
    if effective_to is not None and effective_to <= effective_from:
        raise ValueError("effective_to 는 effective_from 이후여야 한다")
    if version <= 0:
        raise ValueError("version 은 1 이상이어야 한다 (DDL: version > 0)")

    return MandateVersionRow(
        mandate_id=mandate_id,
        version=version,
        objective_text=objective_text,
        objective=objective,
        allowed_assets=list(policy.allowed_assets),
        forbidden_assets=list(policy.forbidden_assets),
        universe_policy=policy.universe_policy.model_dump(mode="json"),
        risk_bounds=policy.risk_bounds.model_dump(mode="json"),
        approval_rules=policy.approval_rules.model_dump(mode="json"),
        execution_rules=execution_rules or {},
        effective_from=effective_from,
        effective_to=effective_to,
        content_hash=compute_content_hash(policy),
        created_by=created_by,
    )


# ---------------------------------------------------------------------------
# Repository 인터페이스 + In-Memory 구현 (asyncpg 구현은 Y1 에서 대체)
# ---------------------------------------------------------------------------


class MandateVersionRepository:
    """persist 인터페이스. 실제 구현은 mandate_versions/mandate_decisions/mandates 에 반영한다."""

    def latest_version(self, mandate_id: str) -> int:
        raise NotImplementedError

    def content_hash_exists(self, mandate_id: str, content_hash: str) -> bool:
        raise NotImplementedError

    def insert(self, row: MandateVersionRow) -> None:
        raise NotImplementedError

    # --- 활성화(Effective Time) / 결정 기록 (5.1) ---

    def get_mandate_current(self, mandate_id: str) -> tuple[int, str]:
        """(current_version, status). 아직 활성 Version 이 없으면 (0, 'DRAFT')."""
        raise NotImplementedError

    def set_mandate_current(self, mandate_id: str, version: int, status: str) -> None:
        """mandates.current_version 과 status 갱신."""
        raise NotImplementedError

    def set_effective_to(self, mandate_id: str, version: int, ts: datetime) -> None:
        """이전 활성 Version 의 effective_to 를 닫는다 (덮어쓰기 아님, 종료 시각 부여)."""
        raise NotImplementedError

    def record_decision(self, decision: MandateDecisionRow) -> None:
        """mandate_decisions append (감사 기록, Append-only)."""
        raise NotImplementedError

    def get_fund_base_currency(self, mandate_id: str) -> str | None:
        """mandates.fund_id -> accounting.funds.base_currency.

        Mandate 가 속한 Fund 의 기준 통화. Fund 를 찾을 수 없으면 None.
        SQL 구현: select f.base_currency from accounting.funds f
                  join governance.mandates m on m.fund_id = f.fund_id
                  where m.mandate_id = $1
        """
        raise NotImplementedError


class InMemoryMandateVersionRepository(MandateVersionRepository):
    def __init__(self) -> None:
        self._rows: list[MandateVersionRow] = []
        self._decisions: list[MandateDecisionRow] = []
        self._mandate_state: dict[str, tuple[int, str]] = {}
        self._fund_currency: dict[str, str] = {}

    def set_fund_base_currency(self, mandate_id: str, currency: str) -> None:
        """테스트·개발용 seed. 실 구현에서는 accounting.funds 를 조회한다."""
        self._fund_currency[mandate_id] = currency

    def get_fund_base_currency(self, mandate_id: str) -> str | None:
        return self._fund_currency.get(mandate_id)

    def latest_version(self, mandate_id: str) -> int:
        versions = [r.version for r in self._rows if r.mandate_id == mandate_id]
        return max(versions) if versions else 0

    def content_hash_exists(self, mandate_id: str, content_hash: str) -> bool:
        return any(
            r.mandate_id == mandate_id and r.content_hash == content_hash
            for r in self._rows
        )

    def insert(self, row: MandateVersionRow) -> None:
        # DDL unique 제약을 앱 레벨에서도 방어한다.
        if any(
            r.mandate_id == row.mandate_id and r.version == row.version
            for r in self._rows
        ):
            raise ValueError(f"version 중복: {row.mandate_id} v{row.version}")
        if self.content_hash_exists(row.mandate_id, row.content_hash):
            raise ValueError("동일 content_hash Version 이 이미 존재한다")
        self._rows.append(row)

    def get(self, mandate_id: str, version: int) -> MandateVersionRow | None:
        for r in self._rows:
            if r.mandate_id == mandate_id and r.version == version:
                return r
        return None

    def get_mandate_current(self, mandate_id: str) -> tuple[int, str]:
        return self._mandate_state.get(mandate_id, (0, "DRAFT"))

    def set_mandate_current(self, mandate_id: str, version: int, status: str) -> None:
        self._mandate_state[mandate_id] = (version, status)

    def set_effective_to(self, mandate_id: str, version: int, ts: datetime) -> None:
        for i, r in enumerate(self._rows):
            if r.mandate_id == mandate_id and r.version == version:
                if r.effective_to is not None:
                    raise ValueError(f"이미 종료된 Version: {mandate_id} v{version}")
                if ts <= r.effective_from:
                    raise ValueError("effective_to 는 effective_from 이후여야 한다")
                self._rows[i] = replace(r, effective_to=ts)
                return
        raise ValueError(f"존재하지 않는 Version: {mandate_id} v{version}")

    def record_decision(self, decision: MandateDecisionRow) -> None:
        self._decisions.append(decision)

    def decisions_for(self, mandate_id: str) -> list[MandateDecisionRow]:
        return [d for d in self._decisions if d.mandate_id == mandate_id]


@dataclass(frozen=True)
class VersionResult:
    row: MandateVersionRow
    direction: ChangeDirection
    requires_user_reapproval: bool


class CurrencyMismatchError(ValueError):
    """Mandate 통화가 Fund 기준 통화와 다르다.

    GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC 2.1 기준 자본 계약(2026-07-31 결정 4-A):
    통화 일치는 governance 가 저장 시점에 검증한다. 한도는 전부 비율이고 기준 자본은
    회계의 Fund 통화로 표시되므로, 통화가 어긋나면 한도 금액이 잘못 계산된다.
    """


class FundNotFoundError(ValueError):
    """Mandate 가 속한 Fund 의 기준 통화를 확인할 수 없다.

    확인 불가 시 저장을 막는다 — 개발 원칙 9(위험한 기능은 실패 시 확대가 아니라 차단).
    """


class MandateVersionService:
    """F01 Version 발급 서비스.

    - 첫 Version 은 사용자 승인 전제(외부에서 approve)로 생성한다.
    - 이후 변경은 이전 활성 정책과 비교해 방향을 판정하고, 확대(LOOSEN)면
      사용자 재승인이 필요함을 결과에 표시한다. (실제 재승인 강제는 호출자/Workflow 가
      requires_user_reapproval 를 보고 Interrupt 한다 — 5.1)
    """

    def __init__(self, repo: MandateVersionRepository) -> None:
        self._repo = repo

    def propose_version(
        self,
        *,
        mandate_id: str,
        policy: MandatePolicy,
        objective_text: str,
        objective: dict,
        effective_from: datetime,
        previous_policy: MandatePolicy | None = None,
        effective_to: datetime | None = None,
        execution_rules: dict | None = None,
        created_by: str | None = None,
    ) -> VersionResult:
        # 통화 검증 (결정 4-A). Fund 통화를 확인할 수 없으면 저장하지 않는다.
        fund_currency = self._repo.get_fund_base_currency(mandate_id)
        if fund_currency is None:
            raise FundNotFoundError(
                f"Fund 기준 통화를 확인할 수 없어 Mandate 를 저장하지 않는다: {mandate_id}"
            )
        if policy.risk_bounds.currency != fund_currency:
            raise CurrencyMismatchError(
                "mandate.currency 가 Fund 기준 통화와 다르다 "
                f"(mandate={policy.risk_bounds.currency}, fund={fund_currency})"
            )

        next_version = self._repo.latest_version(mandate_id) + 1

        if previous_policy is None:
            direction = ChangeDirection.NEUTRAL
        else:
            direction = classify_change(previous_policy, policy)

        row = to_version_row(
            mandate_id=mandate_id,
            version=next_version,
            policy=policy,
            objective_text=objective_text,
            objective=objective,
            effective_from=effective_from,
            effective_to=effective_to,
            execution_rules=execution_rules,
            created_by=created_by,
        )

        if self._repo.content_hash_exists(mandate_id, row.content_hash):
            raise ValueError("내용이 동일한 Version 이 이미 있어 새 Version 을 만들 수 없다")

        self._repo.insert(row)
        return VersionResult(
            row=row,
            direction=direction,
            requires_user_reapproval=requires_user_reapproval(direction),
        )


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/mandate/service.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    def _policy(**over):
        risk = {
            "base_capital": "100000000",
            "currency": "KRW",
            "max_instrument_weight": "0.1",
            "max_sector_weight": "0.3",
            "max_gross_exposure": "1.0",
            "max_concurrent_positions": 10,
            "max_daily_loss": "0.03",
        }
        risk.update(over.pop("risk", {}))
        p = {
            "allowed_assets": over.pop("allowed_assets", ["A005930"]),
            "forbidden_assets": over.pop("forbidden_assets", []),
            "risk_bounds": risk,
            "universe_policy": {
                "allowed_markets": ["KRX"], "trading_start": "09:00", "trading_end": "15:30"
            },
            "approval_rules": {
                "paper_order_mode": over.pop("mode", "USER_APPROVAL")
            },
        }
        return MandatePolicy(**p)

    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    base = _policy()

    # 1) 방향 판정.
    assert classify_change(base, _policy()) == ChangeDirection.NEUTRAL
    assert (
        classify_change(base, _policy(risk={"max_gross_exposure": "0.5"}))
        == ChangeDirection.TIGHTEN
    ), "gross 축소는 완화"
    assert (
        classify_change(base, _policy(risk={"max_gross_exposure": "2.0"}))
        == ChangeDirection.LOOSEN
    ), "gross 확대는 확대"
    assert (
        classify_change(base, _policy(mode="AUTO")) == ChangeDirection.LOOSEN
    ), "자동 주문 전환은 확대"
    assert (
        classify_change(base, _policy(forbidden_assets=["A000660"]))
        == ChangeDirection.TIGHTEN
    ), "금지 추가는 완화"
    assert (
        classify_change(base, _policy(allowed_assets=["A005930", "A035720"]))
        == ChangeDirection.LOOSEN
    ), "허용 추가는 확대"
    # 혼합(하나라도 확대) -> LOOSEN.
    assert (
        classify_change(
            base, _policy(risk={"max_gross_exposure": "0.5"}, allowed_assets=["A005930", "A035720"])
        )
        == ChangeDirection.LOOSEN
    )

    # 2) 재승인 요구 매핑.
    assert requires_user_reapproval(ChangeDirection.LOOSEN) is True
    assert requires_user_reapproval(ChangeDirection.TIGHTEN) is False
    assert requires_user_reapproval(ChangeDirection.NEUTRAL) is False

    # 3) content_hash 재현성.
    h1 = compute_content_hash(base)
    h2 = compute_content_hash(_policy())
    assert h1 == h2, "같은 내용은 같은 hash"
    assert h1 != compute_content_hash(_policy(risk={"max_gross_exposure": "2.0"}))

    # 4) Version 발급 서비스.
    repo = InMemoryMandateVersionRepository()
    repo.set_fund_base_currency("m1", "KRW")  # accounting.funds.base_currency
    svc = MandateVersionService(repo)
    r1 = svc.propose_version(
        mandate_id="m1",
        policy=base,
        objective_text="장기 성장",
        objective={"style": "growth"},
        effective_from=now,
    )
    assert r1.row.version == 1 and r1.direction == ChangeDirection.NEUTRAL

    loosened = _policy(risk={"max_gross_exposure": "2.0"})
    r2 = svc.propose_version(
        mandate_id="m1",
        policy=loosened,
        objective_text="장기 성장",
        objective={"style": "growth"},
        effective_from=now,
        previous_policy=base,
    )
    assert r2.row.version == 2
    assert r2.direction == ChangeDirection.LOOSEN
    assert r2.requires_user_reapproval is True

    # 5) 동일 내용 재제출은 거부 (content_hash unique).
    try:
        svc.propose_version(
            mandate_id="m1",
            policy=base,
            objective_text="장기 성장",
            objective={"style": "growth"},
            effective_from=now,
            previous_policy=loosened,
        )
        raise AssertionError("중복 content_hash 인데 통과함")
    except ValueError:
        pass

    # 5-1) 통화 불일치 거부 (결정 4-A). Fund 는 USD 인데 Mandate 가 KRW.
    repo_usd = InMemoryMandateVersionRepository()
    repo_usd.set_fund_base_currency("m2", "USD")
    svc_usd = MandateVersionService(repo_usd)
    try:
        svc_usd.propose_version(
            mandate_id="m2",
            policy=base,  # currency=KRW
            objective_text="x",
            objective={},
            effective_from=now,
        )
        raise AssertionError("통화 불일치인데 통과함")
    except CurrencyMismatchError:
        pass
    assert repo_usd.latest_version("m2") == 0, "거부된 Version 이 저장됐다"

    # 5-2) 통화가 맞으면 통과.
    r_usd = svc_usd.propose_version(
        mandate_id="m2",
        policy=_policy(risk={"currency": "USD"}),
        objective_text="x",
        objective={},
        effective_from=now,
    )
    assert r_usd.row.version == 1

    # 5-3) Fund 통화를 확인할 수 없으면 저장하지 않는다 (차단 방향).
    repo_unknown = InMemoryMandateVersionRepository()
    svc_unknown = MandateVersionService(repo_unknown)
    try:
        svc_unknown.propose_version(
            mandate_id="m-없는펀드",
            policy=base,
            objective_text="x",
            objective={},
            effective_from=now,
        )
        raise AssertionError("Fund 미확인인데 통과함")
    except FundNotFoundError:
        pass

    # 6) Row 가 mandate_versions 컬럼과 맞는지.
    row = r1.row
    assert set(row.risk_bounds.keys()) >= {"base_capital", "max_gross_exposure"}
    assert row.effective_to is None
    assert isinstance(row.content_hash, str) and len(row.content_hash) == 64

    print("service.py 자체 점검 통과")
