"""Canonical QA workflow contract.

The historical ``qa_required`` flag mixed three different facts: whether QA
was enabled, whether it blocked the response, and whether a QA task happened
to exist. QA is now a post-response governance audit: it may be enabled or
disabled, but it can never block CEO response synthesis. Durable materialization
remains a fact derived from task roles.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

QA_PROFILE = "qa-department"
QA_PROFILE_ALIASES = frozenset(
    {"qa-department", "qa", "quality-assurance", "quality assurance"}
)


@dataclass(frozen=True)
class QaContract:
    """Workflow intent plus the two read-only materialization observations."""

    qa_enabled: bool
    qa_blocks_response: bool
    qa_materialized: bool = False
    qa_legacy_primary_present: bool = False
    source: str = "default"


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return None


def _marker(body: str, key: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(key)}=(\S+)\s*$", str(body or ""))
    return match.group(1) if match else None


def _metadata_values(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return _metadata_values(decoded)
    if isinstance(value, Mapping):
        merged = dict(value)
        for nested_key in (
            "metadata",
            "workflow_metadata",
            "run_metadata",
            "task_run_metadata",
            "task_run",
        ):
            nested = value.get(nested_key)
            if nested is not None:
                merged.update(_metadata_values(nested))
        return merged
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        merged: dict[str, Any] = {}
        for item in value:
            merged.update(_metadata_values(item))
        return merged
    return {}


def canonical_qa_contract(
    *,
    workflow_mode: str,
    body: str = "",
    metadata: Mapping[str, Any] | None = None,
    legacy_qa_required: Any = None,
    paper_order: bool = False,
    default_qa_enabled: bool = True,
    planner_qa_requested: bool = False,
) -> QaContract:
    """Resolve canonical QA intent without using child existence as intent.

    Explicit canonical markers win. ``qa_blocks_response`` is accepted for
    read compatibility but is normalized to ``False``: CEO response delivery
    is the response-plane boundary, and QA is scheduled afterwards in the
    asynchronous governance plane. A bare legacy ``qa_required=false`` still
    explicitly disables the audit for old roots.
    """

    if paper_order:
        return QaContract(True, False, source="paper-order-post-response-audit")

    metadata_values = _metadata_values(metadata or {})
    body_enabled = parse_bool(_marker(body, "qa_enabled"))
    body_blocks = parse_bool(_marker(body, "qa_blocks_response"))
    enabled = body_enabled
    # Legacy callers may still provide this marker. It must not restore the
    # old QA -> CEO blocking topology.
    blocks = False
    source = "canonical-body" if body_enabled is not None or body_blocks is not None else ""

    if enabled is None:
        enabled = parse_bool(metadata_values.get("qa_enabled"))
        if enabled is not None:
            source = "canonical-metadata"
    if body_blocks is not None or metadata_values.get("qa_blocks_response") is not None:
        source = source or "canonical-post-response"

    legacy = parse_bool(legacy_qa_required)
    if legacy is None:
        legacy = parse_bool(_marker(body, "qa_required"))
    if legacy is None:
        legacy = parse_bool(metadata_values.get("qa_required"))

    mode = str(workflow_mode or "analysis").casefold()
    if (
        mode == "analysis"
        and planner_qa_requested
        and body_enabled is None
        and parse_bool(metadata_values.get("qa_enabled")) is None
    ):
        # A planner's QA selection is governance intent, not an analysis
        # primary.  A new canonical false marker still wins explicitly.
        enabled = True
        source = source or "planner-qa-intent"
    if mode == "binding":
        if enabled is None:
            enabled = True
        source = source or ("legacy-binding" if legacy is not None else "binding-default")
    else:
        if enabled is None:
            if legacy is None:
                enabled = default_qa_enabled
                source = source or "analysis-default"
            elif legacy:
                enabled = True
                source = source or "legacy-enabled"
            else:
                # Existing direct roots explicitly advertised an async QA
                # lane through these markers. Preserve that legacy behavior;
                # a bare false is the explicit exclusion form.
                lowered = str(body or "").casefold()
                legacy_async = (
                    _marker(body, "governance_plane") == "async_qa"
                    or _marker(body, "qa_is_not_synthesis_prerequisite") == "true"
                    or "async_post_hoc_audit" in lowered
                )
                enabled = legacy_async
                source = source or ("legacy-async" if legacy_async else "legacy-disabled")
        blocks = False

    enabled = bool(enabled)
    # Deliberately unconditional. This invariant prevents a future legacy
    # marker or workflow mode from recreating QA -> CEO.
    return QaContract(enabled, False, source=source or "default")


def split_planner_selection(values: Sequence[Any]) -> tuple[tuple[str, ...], bool]:
    """Separate QA governance intent from analysis primary profiles."""

    primary: list[str] = []
    qa_requested = False
    for value in values:
        normalized = str(value).strip().strip(" .,:;()[]{}").casefold()
        if normalized in QA_PROFILE_ALIASES:
            qa_requested = True
            continue
        if normalized and normalized not in primary:
            primary.append(normalized)
    return tuple(primary), qa_requested


def with_materialization(
    contract: QaContract,
    *,
    roles: Sequence[str],
    profiles: Sequence[str] = (),
) -> QaContract:
    """Attach read-only runtime facts to a resolved intent contract."""

    normalized_roles = tuple(str(role).strip().casefold() for role in roles)
    normalized_profiles = tuple(str(profile).strip().casefold() for profile in profiles)
    canonical = "qa" in normalized_roles
    legacy_primary = QA_PROFILE in normalized_profiles and "primary" in normalized_roles
    return QaContract(
        contract.qa_enabled,
        contract.qa_blocks_response,
        qa_materialized=canonical,
        qa_legacy_primary_present=legacy_primary,
        source=contract.source,
    )


__all__ = [
    "QA_PROFILE",
    "QA_PROFILE_ALIASES",
    "QaContract",
    "canonical_qa_contract",
    "parse_bool",
    "split_planner_selection",
    "with_materialization",
]
