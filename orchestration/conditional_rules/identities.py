"""Stable identities shared by replay, worker, database, and Trading."""

from __future__ import annotations

import hashlib


def _digest(*parts: object) -> str:
    canonical = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluation_id(rule_id: str, rule_version: int, evaluation_key: str) -> str:
    return "eval_" + _digest(rule_id, rule_version, evaluation_key)[:48]


def trigger_id(
    rule_id: str,
    rule_version: int,
    evaluation_key: str,
    condition_sha256: str,
) -> str:
    return "trg_" + _digest(
        rule_id, rule_version, evaluation_key, condition_sha256
    )[:48]


def execution_idempotency_key(
    rule_id: str, rule_version: int, trigger_identity: str
) -> str:
    suffix = _digest(rule_id, rule_version, trigger_identity)[:32]
    return f"rule:{rule_id}:v{rule_version}:trigger:{suffix}"


__all__ = ["evaluation_id", "execution_idempotency_key", "trigger_id"]
