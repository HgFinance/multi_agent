"""Current authority and canonical PAPER portfolio context for rule evaluation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg2
from psycopg2.extras import register_uuid

from orchestration.conditional_rules import ConditionalRuleSpec, RuleState, rule_fingerprint

from .admission import ConditionalRuleAdmissionError


register_uuid()


@dataclass(frozen=True)
class ActiveRuleContext:
    rule_id: UUID
    rule_version: int
    spec_sha256: str
    spec: ConditionalRuleSpec
    rule_state: RuleState
    membership_active: bool
    fund_active: bool
    book_active: bool


class PostgresConditionalRuleContextRepository:
    def __init__(self, dsn: str, *, role: str = "svc_trading_api") -> None:
        if not dsn.strip() or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role):
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_DB_UNAVAILABLE",
                "conditional rule context database is not configured",
                503,
            )
        self.dsn = dsn
        self.role = role

    def load(self, rule_id: UUID) -> ActiveRuleContext:
        try:
            with psycopg2.connect(self.dsn, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f'SET LOCAL ROLE "{self.role}"')
                    cursor.execute(
                        """
                        select rule.rule_id,rule.current_version,version.spec_sha256,
                               version.spec,rule.state,
                               exists (
                                 select 1
                                   from governance.fund_memberships membership
                                   join governance.user_profiles profile
                                     on profile.user_id=membership.user_id
                                    and profile.status='ACTIVE'
                                  where membership.user_id=rule.user_id
                                    and membership.fund_id=rule.fund_id
                                    and membership.status='ACTIVE'
                                    and membership.role in ('OWNER','CIO','TRADER')
                                    and membership.effective_from<=now()
                                    and (membership.effective_to is null
                                         or membership.effective_to>now())
                               ) as membership_active,
                               exists (
                                 select 1 from accounting.funds fund
                                  where fund.fund_id=rule.fund_id
                                    and fund.status='ACTIVE'
                               ) as fund_active,
                               exists (
                                 select 1 from accounting.books book
                                  where book.book_id=rule.book_id
                                    and book.fund_id=rule.fund_id
                                    and book.status='ACTIVE'
                               ) as book_active
                          from execution.conditional_trade_rules rule
                          join execution.conditional_trade_rule_versions version
                            on version.rule_id=rule.rule_id
                           and version.rule_version=rule.current_version
                         where rule.rule_id=%s
                           and rule.state in ('ACTIVE','TRIGGERED','EXECUTION_PENDING')
                           and rule.execution_mode='PAPER'
                           and rule.confirmation_sha256=version.spec_sha256
                           and rule.expires_at>now()
                        """,
                        (rule_id,),
                    )
                    row = cursor.fetchone()
        except psycopg2.Error as exc:
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_DB_UNAVAILABLE",
                "conditional rule context could not be loaded",
                503,
            ) from exc
        if row is None:
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_NOT_ACTIVE",
                "conditional rule is not active",
                409,
            )
        raw_spec = json.loads(row[3]) if isinstance(row[3], str) else row[3]
        try:
            spec = ConditionalRuleSpec.model_validate(raw_spec)
        except Exception as exc:
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_SPEC_INVALID",
                "stored conditional rule spec is invalid",
                500,
            ) from exc
        if rule_fingerprint(spec) != str(row[2]):
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_FINGERPRINT_INVALID",
                "stored conditional rule payload does not match its fingerprint",
                500,
            )
        return ActiveRuleContext(
            rule_id=UUID(str(row[0])),
            rule_version=int(row[1]),
            spec_sha256=str(row[2]),
            spec=spec,
            rule_state=RuleState(str(row[4])),
            membership_active=bool(row[5]),
            fund_active=bool(row[6]),
            book_active=bool(row[7]),
        )


def context_repository_from_env() -> PostgresConditionalRuleContextRepository:
    dsn = os.environ.get("PAPER_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    return PostgresConditionalRuleContextRepository(
        dsn,
        role=os.environ.get("TRADING_DATABASE_ROLE", "svc_trading_api").strip(),
    )


__all__ = [
    "ActiveRuleContext",
    "PostgresConditionalRuleContextRepository",
    "context_repository_from_env",
]
