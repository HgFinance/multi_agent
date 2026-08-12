"""Safety-first command intake contract.

The endpoint records an auditable, idempotent request and deliberately does not
change OMS, Risk Engine, broker, or ledger state. Binding execution belongs to
an authenticated approval service that is not part of this BFF prototype.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class TradingStateTarget(BaseModel):
    fund_id: str = Field(min_length=1, max_length=128)


class TradingStateCommand(BaseModel):
    command: Literal["SET_TRADING_STATE"]
    target: TradingStateTarget
    requested_state: Literal["NORMAL", "ENTRY_BLOCKED", "REDUCE_ONLY", "HALTED"]
    reason: str = Field(min_length=1, max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=128)
    expected_version: int = Field(ge=0)


class CommandVersionConflict(ValueError):
    """The client based a command on a stale projected state."""


class IdempotencyConflict(ValueError):
    """An idempotency key was reused with a different command body."""


class TradingStateCommandService:
    def __init__(self) -> None:
        self._lock = RLock()
        self._version_by_fund: dict[str, int] = {}
        self._commands_by_key: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self._audit_events: list[dict[str, Any]] = []

    @staticmethod
    def _fingerprint(command: TradingStateCommand) -> str:
        payload = command.model_dump_json(exclude_none=True, by_alias=True)
        return sha256(payload.encode("utf-8")).hexdigest()

    def submit(self, command: TradingStateCommand) -> dict[str, Any]:
        fund_id = command.target.fund_id
        fingerprint = self._fingerprint(command)
        with self._lock:
            existing = self._commands_by_key.get(command.idempotency_key)
            if existing:
                _, existing_fingerprint, response = existing
                if existing_fingerprint != fingerprint:
                    raise IdempotencyConflict("idempotency_key_reused_with_different_payload")
                return {**response, "replayed": True}

            current_version = self._version_by_fund.get(fund_id, 0)
            if command.expected_version != current_version:
                raise CommandVersionConflict(
                    f"expected_version={command.expected_version} current_version={current_version}"
                )

            command_id = f"cmd-{uuid4().hex}"
            audit_event = {
                "event_type": "trading.state.command.requested.v1",
                "audit_event_id": f"audit-{uuid4().hex}",
                "command_id": command_id,
                "idempotency_key": command.idempotency_key,
                "fund_id": fund_id,
                "requested_state": command.requested_state,
                "expected_version": command.expected_version,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "execution_status": "NOT_EXECUTED",
                "binding_executed": False,
            }
            response = {
                "schema_version": "operator-command.v1",
                "command_id": command_id,
                "command": command.command,
                "status": "PENDING_APPROVAL",
                "execution_status": "NOT_EXECUTED",
                "binding_executed": False,
                "current_version": current_version,
                "audit_event": audit_event,
                "replayed": False,
            }
            self._commands_by_key[command.idempotency_key] = (command_id, fingerprint, response)
            self._audit_events.append(audit_event)
            return response

    def audit_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._audit_events]


COMMAND_SERVICE = TradingStateCommandService()


__all__ = [
    "COMMAND_SERVICE",
    "CommandVersionConflict",
    "IdempotencyConflict",
    "TradingStateCommand",
    "TradingStateCommandService",
]
