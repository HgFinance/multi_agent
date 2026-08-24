"""MemoHarness-lite D5 experience memory.

The experience bank is deliberately a small, fail-open adapter around the
private Postgres/Supabase-compatible database.  It stores structured workflow
outcomes only; user prompts, worker payloads, credentials, and model output are
never persisted here.

Modes:

* ``off``    - no database access and no planner hint.
* ``shadow`` - read/write the bank and emit timing, but do not alter D4.
* ``active`` - read/write the bank and pass a bounded advisory hint to D4.

The adapter has no LLM dependency.  Provider quota/auth failures are retained
as observed failure codes but are intentionally excluded from routing hints.
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable, Mapping, Sequence

from orchestration.experience_retention_policy import (
    D5_WRITE_STOP_RELATION_BYTES,
    OPERATIONAL_FAILURE_CODES,
)


LOGGER = logging.getLogger(__name__)
TABLE_NAME = "experience.workflow_experiences"
MODES = frozenset({"off", "shadow", "active"})
_CODE_RE = re.compile(r"[^A-Z0-9_.:-]+")
_OPERATIONAL_FAILURE_CODES = OPERATIONAL_FAILURE_CODES


@dataclass(frozen=True)
class ExperienceRecord:
    """Safe, structured outcome written to the durable experience bank."""

    case_type: str
    binding: bool
    primary_departments: tuple[str, ...]
    orchestration_policy: str
    success: bool
    failure_codes: tuple[str, ...]
    latency_ms: int | None
    qa_enabled: bool
    qa_blocks_response: bool
    lesson: str
    source_run_id: str | None = None
    experience_identity: str | None = None


@dataclass(frozen=True)
class ExperienceLookup:
    mode: str
    available: bool
    elapsed_ms: int
    matched_count: int
    planner_hint: dict[str, Any] | None = None
    error_code: str | None = None
    lookup_ms: int = 0
    hint_build_ms: int = 0


@dataclass(frozen=True)
class ExperienceWrite:
    mode: str
    available: bool
    elapsed_ms: int
    written: bool
    error_code: str | None = None


def configured_mode(value: str | None = None) -> str:
    candidate = str(value if value is not None else os.getenv("MEMOHARNESS_D5_MODE", "off"))
    mode = candidate.strip().lower()
    return mode if mode in MODES else "off"


def _bounded_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _safe_code(value: Any) -> str:
    code = _CODE_RE.sub("_", str(value or "").upper()).strip("_:-")
    return code[:64]


def _safe_codes(values: Sequence[Any] | None) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    result: list[str] = []
    for value in values[:12]:
        code = _safe_code(value)
        if code and code not in result:
            result.append(code)
    return tuple(result)


def _safe_departments(values: Sequence[Any] | None) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    result: list[str] = []
    for value in values[:12]:
        department = _bounded_text(value, 48).lower()
        if department and department not in result:
            result.append(department)
    return tuple(result)


def bounded_planner_hint(
    experience_hint: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the small, payload-free hint allowed to cross into D4."""

    if not isinstance(experience_hint, Mapping):
        return None
    bounded: dict[str, Any] = {}
    for key in (
        "source",
        "matched_runs",
        "successful_runs",
        "success_rate",
        "operational_failures_are_non_routing",
    ):
        if key in experience_hint:
            bounded[key] = experience_hint[key]
    for key in ("successful_policies", "successful_department_sets", "lessons"):
        value = experience_hint.get(key)
        if not isinstance(value, list):
            continue
        bounded[key] = [
            item for item in value[:3] if isinstance(item, (str, Mapping))
        ]
    current = experience_hint.get("current_departments")
    if isinstance(current, list):
        bounded["current_departments"] = [
            str(item)[:48] for item in current[:12]
        ]
    return bounded or None


def canonical_experience_identity(
    *,
    root_id: Any = None,
    source_run_id: Any = None,
    namespace: str = "portfolio",
) -> str | None:
    """Build one non-null DB identity for a workflow outcome."""

    value = _bounded_text(root_id if root_id is not None else source_run_id, 128)
    if not value:
        return None
    prefix = "kanban" if root_id is not None else namespace.strip().lower() or "run"
    return _bounded_text(f"{prefix}:{value}", 160)


def experience_case_type(profile: Mapping[str, Any]) -> str:
    explicit = _bounded_text(profile.get("case_type"), 64).lower()
    if explicit:
        return explicit
    category = _bounded_text(profile.get("category"), 64).lower()
    return category or "portfolio_recommendation"


def _binding(profile: Mapping[str, Any], result: Mapping[str, Any] | None = None) -> bool:
    return bool(profile.get("binding", False) or (result or {}).get("binding", False))


def build_experience_record(
    profile: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    latency_ms: int | None = None,
) -> ExperienceRecord:
    """Derive a safe record without copying the query or worker output."""

    plan = result.get("task_plan")
    requested = plan.get("requested_departments", ()) if isinstance(plan, Mapping) else ()
    primary = tuple(
        department
        for department in _safe_departments(requested)
        if department not in {"qa", "ceo"}
    )
    binding = _binding(profile, result)
    policy = "binding_qa_gate" if binding else "analysis_parallel"
    success = str(result.get("pipeline_status", "")).upper() == "COMPLETED"

    failures: list[str] = []
    if not success:
        failures.append("PIPELINE_DEGRADED")
    for department in result.get("degraded_departments", ()):
        code = _safe_code(f"{department}_DEGRADED")
        if code:
            failures.append(code)
    data_context = result.get("data_context")
    if isinstance(data_context, Mapping) and data_context.get("quality_status") not in {None, "PASS", "TEST"}:
        failures.append("DATA_QUALITY")
    for report in result.get("worker_reports", ()):
        if not isinstance(report, Mapping):
            continue
        # Read only structured classifier fields.  Never inspect or persist
        # free-form exception text, which may contain provider payloads.
        for key in ("failure_category", "failure_code", "error_code"):
            value = report.get(key)
            if value:
                code = _safe_code(value)
                if code:
                    failures.append(code)

    failure_codes = tuple(dict.fromkeys(failures))
    department_text = "+".join(primary) or "no-primary"
    outcome = "succeeded" if success else "completed with a safe hold"
    lesson = _bounded_text(f"{department_text} {policy} {outcome}", 240)
    source_run_id = _bounded_text(
        result.get("case_id") or result.get("trace_id") or profile.get("case_id"),
        160,
    ) or None
    return ExperienceRecord(
        case_type=experience_case_type(profile),
        binding=binding,
        primary_departments=primary,
        orchestration_policy=policy,
        success=success,
        failure_codes=_safe_codes(failure_codes),
        latency_ms=max(0, int(latency_ms)) if latency_ms is not None else None,
        qa_enabled=bool(profile.get("qa_enabled", True)),
        qa_blocks_response=bool(profile.get("qa_blocks_response", binding)),
        lesson=lesson,
        source_run_id=source_run_id,
        experience_identity=canonical_experience_identity(
            source_run_id=source_run_id,
        ),
    )


def _body_marker(body: Any, key: str) -> str:
    match = re.search(
        rf"(?m)^{re.escape(key)}=([^\n\r]+)\s*$",
        str(body or ""),
    )
    return _bounded_text(match.group(1), 128) if match else ""


def _task_role(payload: Mapping[str, Any]) -> str:
    body = str(payload.get("body") or "")
    return _body_marker(body, "workflow_role").casefold()


def build_discord_experience_record(
    *,
    root_id: str,
    root_payload: Mapping[str, Any],
    task_payloads: Sequence[Mapping[str, Any]],
    terminal_status: str,
) -> ExperienceRecord:
    """Build one safe aggregate from a finalized Discord/Kanban root."""

    root_body = str(root_payload.get("body") or "")
    binding = _body_marker(root_body, "workflow_mode").casefold() == "binding"
    primary: list[str] = []
    failure_codes: list[str] = []
    for payload in task_payloads:
        if _task_role(payload) != "primary":
            continue
        profile = _bounded_text(
            payload.get("assignee") or payload.get("profile"),
            48,
        ).lower()
        if profile and profile not in primary:
            primary.append(profile)
        status = _bounded_text(
            payload.get("status") or payload.get("outcome"),
            32,
        ).casefold()
        if status in {"blocked", "failed", "crashed", "gave_up", "timed_out"}:
            failure_codes.append(_safe_code(f"{profile or 'primary'}_{status}"))
        for key in ("failure_category", "failure_code", "error_code"):
            value = payload.get(key)
            if value:
                code = _safe_code(value)
                if code:
                    failure_codes.append(code)

    normalized_status = _bounded_text(terminal_status, 32).casefold()
    success = normalized_status not in {
        "blocked",
        "failed",
        "crashed",
        "gave_up",
        "timed_out",
    }
    if not success:
        failure_codes.append(_safe_code(f"ROOT_{normalized_status or 'FAILED'}"))
    primary_text = "+".join(_safe_departments(primary)) or "no-primary"
    policy = "binding_qa_gate" if binding else "analysis_parallel"
    outcome = "succeeded" if success else "completed with a safe hold"
    return ExperienceRecord(
        case_type="discord_ceo",
        binding=binding,
        primary_departments=tuple(_safe_departments(primary)),
        orchestration_policy=policy,
        success=success,
        failure_codes=tuple(dict.fromkeys(code for code in failure_codes if code)),
        latency_ms=None,
        qa_enabled=_body_marker(root_body, "qa_enabled").casefold() != "false",
        qa_blocks_response=_body_marker(root_body, "qa_blocks_response").casefold()
        == "true",
        lesson=_bounded_text(f"{primary_text} {policy} {outcome}", 240),
        source_run_id=_bounded_text(root_id, 128) or None,
        experience_identity=canonical_experience_identity(root_id=root_id),
    )


class ExperienceBank:
    """Fail-open Postgres adapter for D5 retrieval and outcome logging."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        mode: str | None = None,
        top_k: int | None = None,
        connect_factory: Callable[..., Any] | None = None,
        connect_timeout: int = 2,
        statement_timeout_ms: int | None = None,
    ) -> None:
        self.mode = configured_mode(mode)
        self.dsn = (dsn or "").strip()
        try:
            configured_top_k = int(top_k or os.getenv("MEMOHARNESS_D5_TOP_K", "5"))
        except (TypeError, ValueError):
            configured_top_k = 5
        self.top_k = max(1, min(configured_top_k, 20))
        self.connect_factory = connect_factory
        self.connect_timeout = max(1, int(connect_timeout))
        configured_statement_timeout = statement_timeout_ms
        if configured_statement_timeout is None:
            try:
                configured_statement_timeout = int(
                    os.getenv("MEMOHARNESS_D5_STATEMENT_TIMEOUT_MS", "1500")
                )
            except (TypeError, ValueError):
                configured_statement_timeout = 1500
        self.statement_timeout_ms = max(100, min(int(configured_statement_timeout), 10000))

    @classmethod
    def from_env(cls) -> "ExperienceBank":
        dsn = (
            os.getenv("MEMOHARNESS_D5_DATABASE_URL", "").strip()
            or os.getenv("MEMOHARNESS_EXPERIENCE_DATABASE_URL", "").strip()
            or os.getenv("CONTROL_DATABASE_URL", "").strip()
            or os.getenv("DATABASE_URL", "").strip()
        )
        return cls(dsn, mode=os.getenv("MEMOHARNESS_D5_MODE", "off"))

    @property
    def enabled(self) -> bool:
        return self.mode in {"shadow", "active"}

    def _connect(self) -> Any:
        if self.connect_factory is not None:
            return self.connect_factory(self.dsn, connect_timeout=self.connect_timeout)
        import psycopg2

        return psycopg2.connect(self.dsn, connect_timeout=self.connect_timeout)

    @staticmethod
    def _close(connection: Any) -> None:
        close = getattr(connection, "close", None)
        if callable(close):
            close()

    def lookup(
        self,
        *,
        case_type: str,
        binding: bool,
        primary_departments: Sequence[str] = (),
        correlation_id: str | None = None,
    ) -> ExperienceLookup:
        started = monotonic()
        if not self.enabled:
            return ExperienceLookup(self.mode, False, 0, 0)
        if not self.dsn:
            return self._lookup_failure(
                started,
                "D5_DATABASE_URL_MISSING",
                correlation_id=correlation_id,
            )

        connection = None
        try:
            connection = self._connect()
            cursor = connection.cursor()
            try:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    "SET LOCAL statement_timeout = %s",
                    (self.statement_timeout_ms,),
                )
                cursor.execute(
                    f"""
                    SELECT case_type, binding, primary_departments,
                           orchestration_policy, success, failure_codes,
                           latency_ms, qa_enabled, qa_blocks_response, lesson
                      FROM {TABLE_NAME}
                     WHERE case_type = %s AND binding = %s
                     ORDER BY created_at DESC
                     LIMIT %s
                    """,
                    (_bounded_text(case_type, 64).lower(), bool(binding), self.top_k),
                )
                rows = cursor.fetchall()
            finally:
                close = getattr(cursor, "close", None)
                if callable(close):
                    close()
            lookup_ms = _elapsed_ms(started)
            records = [self._row_to_record(row) for row in rows]
            hint_started = monotonic()
            hint = self._build_hint(records, primary_departments)
            hint_build_ms = _elapsed_ms(hint_started)
            elapsed = _elapsed_ms(started)
            LOGGER.info(
                "memo_harness_d5_lookup mode=%s available=true matched=%d "
                "lookup_ms=%d hint_build_ms=%d elapsed_ms=%d correlation_id=%s",
                self.mode,
                len(records),
                lookup_ms,
                hint_build_ms,
                elapsed,
                _bounded_text(correlation_id, 128),
            )
            return ExperienceLookup(
                self.mode,
                True,
                elapsed,
                len(records),
                hint,
                None,
                lookup_ms,
                hint_build_ms,
            )
        except Exception as exc:  # D5 is advisory; D4 must continue.
            return self._lookup_failure(
                started,
                type(exc).__name__,
                correlation_id=correlation_id,
            )
        finally:
            if connection is not None:
                self._close(connection)

    def record(self, record: ExperienceRecord) -> ExperienceWrite:
        started = monotonic()
        if not self.enabled:
            return ExperienceWrite(self.mode, False, 0, False)
        if not self.dsn:
            return self._write_failure(started, "D5_DATABASE_URL_MISSING")

        connection = None
        try:
            connection = self._connect()
            cursor = connection.cursor()
            try:
                cursor.execute(
                    "SET LOCAL statement_timeout = %s",
                    (self.statement_timeout_ms,),
                )
                # Capacity protection is scoped to the D5 relation, not the
                # whole control database.  At the write-stop threshold D5
                # fails open so the user workflow is never blocked.
                cursor.execute(
                    "SELECT pg_total_relation_size(%s::regclass)",
                    (TABLE_NAME,),
                )
                relation_size_row = getattr(cursor, "fetchone", lambda: (0,))()
                relation_size = max(0, int((relation_size_row or (0,))[0] or 0))
                if relation_size >= D5_WRITE_STOP_RELATION_BYTES:
                    return self._write_failure(started, "D5_CAPACITY_LIMIT")
                identity = _bounded_text(
                    record.experience_identity or "",
                    160,
                ) or canonical_experience_identity(
                    source_run_id=record.source_run_id,
                )
                if not identity:
                    return self._write_failure(started, "D5_IDENTITY_MISSING")
                cursor.execute(
                    f"""
                    INSERT INTO {TABLE_NAME} (
                      experience_identity, case_type, binding, primary_departments,
                      orchestration_policy, success, failure_codes,
                      latency_ms, qa_enabled, qa_blocks_response,
                      lesson, source_run_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (experience_identity) DO NOTHING
                    """,
                    (
                        identity,
                        _bounded_text(record.case_type, 64).lower(),
                        bool(record.binding),
                        list(_safe_departments(record.primary_departments)),
                        _bounded_text(record.orchestration_policy, 64),
                        bool(record.success),
                        list(_safe_codes(record.failure_codes)),
                        record.latency_ms,
                        bool(record.qa_enabled),
                        bool(record.qa_blocks_response),
                        _bounded_text(record.lesson, 240),
                        _bounded_text(record.source_run_id, 128) or None,
                    ),
                )
                written = getattr(cursor, "rowcount", 1) != 0
            finally:
                close = getattr(cursor, "close", None)
                if callable(close):
                    close()
            connection.commit()
            elapsed = _elapsed_ms(started)
            LOGGER.info(
                "memo_harness_d5_write mode=%s available=true written=%s elapsed_ms=%d",
                self.mode,
                written,
                elapsed,
            )
            return ExperienceWrite(self.mode, True, elapsed, bool(written))
        except Exception as exc:  # D5 write failure must not fail a workflow.
            if connection is not None:
                rollback = getattr(connection, "rollback", None)
                if callable(rollback):
                    rollback()
            return self._write_failure(started, type(exc).__name__)
        finally:
            if connection is not None:
                self._close(connection)

    def _lookup_failure(
        self,
        started: float,
        code: str,
        *,
        correlation_id: str | None = None,
    ) -> ExperienceLookup:
        elapsed = _elapsed_ms(started)
        LOGGER.warning(
            "memo_harness_d5_lookup mode=%s available=false error=%s "
            "lookup_ms=%d hint_build_ms=0 elapsed_ms=%d correlation_id=%s",
            self.mode,
            _safe_code(code),
            elapsed,
            elapsed,
            _bounded_text(correlation_id, 128),
        )
        return ExperienceLookup(
            self.mode,
            False,
            elapsed,
            0,
            error_code=_safe_code(code),
            lookup_ms=elapsed,
        )

    def _write_failure(self, started: float, code: str) -> ExperienceWrite:
        elapsed = _elapsed_ms(started)
        LOGGER.warning(
            "memo_harness_d5_write mode=%s available=false error=%s elapsed_ms=%d",
            self.mode,
            _safe_code(code),
            elapsed,
        )
        return ExperienceWrite(self.mode, False, elapsed, False, _safe_code(code))

    @staticmethod
    def _row_to_record(row: Sequence[Any]) -> ExperienceRecord:
        return ExperienceRecord(
            case_type=_bounded_text(row[0], 64),
            binding=bool(row[1]),
            primary_departments=_safe_departments(row[2]),
            orchestration_policy=_bounded_text(row[3], 64),
            success=bool(row[4]),
            failure_codes=_safe_codes(row[5]),
            latency_ms=int(row[6]) if row[6] is not None else None,
            qa_enabled=bool(row[7]),
            qa_blocks_response=bool(row[8]),
            lesson=_bounded_text(row[9], 240),
        )

    @staticmethod
    def _build_hint(
        records: Sequence[ExperienceRecord],
        primary_departments: Sequence[str],
    ) -> dict[str, Any] | None:
        if not records:
            return None
        requested = set(_safe_departments(primary_departments))
        successes = [
            record
            for record in records
            if record.success
            and not (set(_safe_codes(record.failure_codes)) & _OPERATIONAL_FAILURE_CODES)
        ]
        policies = Counter(record.orchestration_policy for record in successes)
        department_sets = Counter(
            "+".join(record.primary_departments) for record in successes if record.primary_departments
        )
        hint: dict[str, Any] = {
            "source": "memo_harness_d5",
            "matched_runs": len(records),
            "successful_runs": len(successes),
            "success_rate": round(sum(record.success for record in records) / len(records), 3),
            "operational_failures_are_non_routing": True,
        }
        if policies:
            hint["successful_policies"] = [
                {"policy": policy, "count": count}
                for policy, count in policies.most_common(3)
            ]
        if department_sets:
            hint["successful_department_sets"] = [
                {"departments": department_set, "count": count}
                for department_set, count in department_sets.most_common(3)
            ]
        if requested:
            hint["current_departments"] = sorted(requested)
        lessons = [
            record.lesson
            for record in successes
            if record.lesson
        ][:3]
        if lessons:
            hint["lessons"] = lessons
        return hint


def _elapsed_ms(started: float) -> int:
    return max(0, int((monotonic() - started) * 1000))


__all__ = [
    "ExperienceBank",
    "ExperienceLookup",
    "ExperienceRecord",
    "ExperienceWrite",
    "bounded_planner_hint",
    "build_experience_record",
    "build_discord_experience_record",
    "canonical_experience_identity",
    "configured_mode",
    "experience_case_type",
]
