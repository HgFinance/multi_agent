"""Conditional PAPER event consumer using existing delivery/projection owners.

This is the missing adapter between ``hf:conditional-rule-events:v1`` and the
already-owned Trading status, Discord, Kanban, and Notion components.  It owns
no bot, order state, fill interpretation, or second notification database.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import redis

from apps.api.conditional_rule_status import build_conditional_execution_status
from apps.api.user_order_workflow import (
    directive_execution_event_payload,
    user_order_repository,
)
from apps.api.user_orders import (
    _workflow_state_from_directive,
    read_paper_directive_status_for_admitted_authority,
)
from orchestration.adapters.ceo_notion_projection import CeoNotionProjection
from orchestration.adapters.ceo_supervisor import HermesKanbanClient
from orchestration.adapters.department_notion_projection import (
    DepartmentNotionProjection,
)
from orchestration.adapters.terminal_projection_utils import action, workflow_role
from orchestration.canonical_profiles import canonical_profile_for_department
from orchestration.conditional_rules.worker_store import PostgresRuleWorkerStore
from orchestration.discord_delivery import DiscordFinalDelivery, correlation_from_task
from orchestration.discord_idempotency import DiscordIdempotencyStore
from orchestration.semantic_qa import SemanticQaResult, evaluate_answer

LOG = logging.getLogger("conditional-rule-notification-consumer")
DEFAULT_STREAM = "hf:conditional-rule-events:v1"
DEFAULT_GROUP = "conditional-paper-reporting-v1"
DEFAULT_PROJECTION_GROUP = "conditional-paper-projection-v1"
SUPPORTED_EVENT = "DIRECTIVE_SUBMITTED"
CONSUMER_MODES = frozenset({"all", "delivery", "projection"})


class ConditionalNotificationError(RuntimeError):
    """A retryable reporting dependency did not complete."""


def _publish_answer_qa(
    *,
    root_task: Mapping[str, Any],
    record: Any,
    snapshot: Any,
    qa: SemanticQaResult,
) -> bool:
    """Publish one redacted QA evaluation without reopening a closed root run."""

    try:
        from langsmith import RunTree

        from orchestration.ceo_workflow_scope import (
            langsmith_trace_context_from_body,
        )
        from orchestration.langsmith_feedback import (
            EvaluationResult,
            publish_evaluation,
        )
        from orchestration.llm_observability import (
            _safe_langsmith_client,
            langsmith_project,
        )

        body = str(root_task.get("body") or "")
        context = langsmith_trace_context_from_body(body)
        source_trace_id = ""
        if context:
            run = RunTree.from_headers(
                {"langsmith-trace": str(context)},
                project_name=langsmith_project("workflow"),
                ls_client=_safe_langsmith_client(),
            )
            if run is not None:
                source_trace_id = str(run.id)
        root_id = _task_id(root_task)
        source_id = (
            f"{source_trace_id or record.client_request_id}:"
            f"conditional:{snapshot.directive_id}"
        )[:128]
        metadata = {
            "schema_version": "hgfinance.observability.feedback.v1",
            "source_run_id": source_id,
            "source_project": langsmith_project("workflow"),
            "source": "conditional-execution-consumer",
            "trace_id": source_trace_id or None,
            "request_id": str(record.client_request_id),
            "root_id": root_id or None,
            "task_id": root_id or None,
            "workflow_mode": "binding",
            "workflow_role": "conditional-execution-verification",
            "department": "trading",
            "status": "completed",
            "raw_payloads_sent": False,
            **qa.as_metadata(),
        }
        result = EvaluationResult(
            source_run_id=source_id,
            department="trading",
            workflow_role="conditional-execution-verification",
            decision=("OBSERVED_PASS" if qa.verdict == "PASS" else "REVIEW_REQUIRED"),
            score=qa.score,
            finding_codes=qa.finding_codes,
            summaries=("authoritative conditional execution answer QA evaluated",),
            metadata=metadata,
        )
        return bool(
            publish_evaluation(
                result,
                str(os.getenv("LANGSMITH_EVALS_PROJECT") or "HgFinance-Evals"),
            )
        )
    except Exception:
        LOG.warning("conditional answer LangSmith QA publication failed")
        return False


def _task_id(task: Mapping[str, Any]) -> str:
    return str(task.get("id") or task.get("task_id") or "").strip()


def _comments_contain(task: Mapping[str, Any], marker: str) -> bool:
    comments = task.get("comments") or ()
    if isinstance(comments, Sequence) and not isinstance(comments, (str, bytes)):
        return any(
            marker in str(item.get("body") if isinstance(item, Mapping) else item)
            for item in comments
        )
    return marker in str(task.get("comment") or "")


class ConditionalRuleNotificationConsumer:
    def __init__(
        self,
        *,
        rule_store: Any,
        order_store: Any,
        status_reader: Callable[..., Any],
        kanban_client: Any,
        discord_delivery: Any,
        discord_store: Any,
        ceo_projection: Any,
        department_projection: Any,
        mode: str = "all",
    ) -> None:
        if mode not in CONSUMER_MODES:
            raise ValueError(f"unsupported conditional notification mode: {mode}")
        self.rule_store = rule_store
        self.order_store = order_store
        self.status_reader = status_reader
        self.kanban_client = kanban_client
        self.discord_delivery = discord_delivery
        self.discord_store = discord_store
        self.ceo_projection = ceo_projection
        self.department_projection = department_projection
        self.mode = mode

    def _context(self, event: Mapping[str, Any]) -> dict[str, Any]:
        payload = event.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, Mapping):
            raise ConditionalNotificationError("conditional event payload is invalid")
        context = dict(payload)
        rule_id = str(event.get("aggregate_id") or context.get("rule_id") or "").strip()
        directive_id = str(context.get("directive_id") or "").strip()
        if not rule_id or not directive_id:
            raise ConditionalNotificationError(
                "conditional event correlation is incomplete"
            )
        # Redis is a delivery boundary, not an authority boundary.  Always
        # replace its correlation metadata with the durable rule/request link.
        # This also resolves multi-condition rules, whose per-rule request key
        # intentionally differs from the parent admitted request key.
        resolved = self.rule_store.notification_context(
            rule_id=rule_id, directive_id=directive_id
        )
        context.update(resolved.__dict__)
        context["rule_id"] = rule_id
        context["directive_id"] = directive_id
        return context

    def _related_workflows(
        self, context: Mapping[str, Any]
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        roots: list[str] = []
        original_root = str(context.get("ceo_root_task_id") or "").strip()
        if original_root:
            roots.append(original_root)
        try:
            original = self.kanban_client.show(original_root) if original_root else {}
        except Exception:
            original = {}
        original_correlation = correlation_from_task(original)
        target_thread = original_correlation.thread_id
        if target_thread:
            try:
                for task in self.kanban_client.list_tasks():
                    tid = _task_id(task)
                    if not tid or tid in roots:
                        continue
                    if workflow_role(task) != "root":
                        continue
                    if correlation_from_task(task).thread_id == target_thread:
                        roots.append(tid)
            except Exception:
                LOG.warning("related Discord-thread workflow discovery failed")

        workflows: list[tuple[str, list[dict[str, Any]]]] = []
        for root_id in roots:
            try:
                resolved_root, children = self.kanban_client.workflow(root_id)
                root_task = self.kanban_client.show(resolved_root)
            except Exception:
                LOG.warning(
                    "conditional workflow projection unavailable root=%s", root_id
                )
                continue
            workflows.append((resolved_root, [root_task, *children]))
        return workflows

    def _comment_once(self, task: Mapping[str, Any], marker: str, text: str) -> None:
        tid = _task_id(task)
        if not tid or _comments_contain(task, marker):
            return
        self.kanban_client.comment_task(tid, f"{marker}\n{text}")

    def handle_event(self, event: Mapping[str, Any]) -> bool:
        """Project one event. Return true only at the accounting terminal state."""

        if str(event.get("event_type") or "") != SUPPORTED_EVENT:
            return True
        event_id = str(event.get("event_id") or "").strip()
        if not event_id:
            raise ConditionalNotificationError("conditional event_id is missing")
        context = self._context(event)
        if not context.get("order_request_id"):
            # Rules admitted outside the user-order workflow have no Discord,
            # Kanban, or Notion target.  Acknowledge them instead of retaining a
            # permanently poisonous Redis entry.
            LOG.info(
                "conditional report has no admitted order event_id=%s rule=%s",
                event_id,
                context.get("rule_id"),
            )
            return True
        record = self.order_store.get(str(context["order_request_id"]))
        if record is None:
            raise ConditionalNotificationError("linked order request was not found")
        for key in ("user_id", "fund_id", "book_id", "client_request_id"):
            if str(getattr(record, key)) != str(context[key]):
                raise ConditionalNotificationError(
                    f"conditional event {key} does not match admitted authority"
                )

        directive = self.status_reader(
            user_id=record.user_id,
            fund_id=record.fund_id,
            book_id=record.book_id,
            directive_id=str(context["directive_id"]),
        )
        workflow_state = _workflow_state_from_directive(directive)
        correlation = directive_execution_event_payload(record, directive)
        # Only the immediate lane updates the existing user-order projection.
        # The durable projection lane reads the same authority independently,
        # but must not create a duplicate BROKER_EXECUTION_SNAPSHOT audit row.
        if self.mode in {"all", "delivery"}:
            self.order_store.mark_outcome(
                record.order_request_id,
                state=workflow_state,
                directive_id=str(context["directive_id"]),
                error_code=getattr(directive, "error_code", None)
                if not isinstance(directive, Mapping)
                else directive.get("error_code"),
                event_type="BROKER_EXECUTION_SNAPSHOT",
                event_payload=correlation,
            )
        snapshot = build_conditional_execution_status(
            rule_id=str(context["rule_id"]),
            rule_execution_id=str(context.get("rule_execution_id") or "") or None,
            directive=directive,
            expected_directive_id=str(context["directive_id"]),
            workflow_state=workflow_state,
        )
        terminal = snapshot.workflow_state in {"COMPLETED", "FAILED", "UNKNOWN"}
        if not terminal:
            # The Redis entry intentionally remains pending so the authoritative
            # directive can be read again.  Do not project an intermediate fill
            # to Discord, Kanban, or Notion: doing so creates a second user-facing
            # message when accounting later advances the same event to COMPLETED.
            return False
        content = snapshot.final_answer
        qa = evaluate_answer(content, status="completed")
        if qa.verdict != "PASS":
            raise ConditionalNotificationError(
                "conditional execution answer failed deterministic QA"
            )
        content += f"\nQA 검증 : {qa.verdict} ({qa.version})"
        marker = (
            "hgfinance.conditional-execution-correction.v2 "
            f"event_id={event_id} state={snapshot.workflow_state}"
        )
        original_root: Mapping[str, Any] = {
            "id": str(context.get("ceo_root_task_id") or ""),
            "body": f"discord_request_id={record.client_request_id}",
        }
        original_trading: Mapping[str, Any] = {
            "id": str(context.get("trading_task_id") or ""),
            "body": f"discord_request_id={record.client_request_id}",
        }

        # The user-facing path ends here.  It needs only the authoritative DB
        # snapshot, deterministic local QA, and the existing idempotent Discord
        # sender.  Hermes CLI, Notion, and external LangSmith are deliberately
        # excluded so their latency or outage cannot delay the fill report.
        if self.mode in {"all", "delivery"}:
            discord_status = self.discord_delivery.deliver_to_existing_thread(
                root_task_id=str(
                    context.get("ceo_root_task_id") or context["rule_id"]
                ),
                source_task=original_trading,
                root_task=original_root,
                content=content,
                title="💹 조건주문 권위 상태",
                store=self.discord_store,
                profile=canonical_profile_for_department("ceo"),
                response_key_suffix=(
                    f"conditional-execution-qa-v2:{event_id}:"
                    f"{snapshot.workflow_state}"
                ),
            )
            if discord_status not in {"sent", "deduped"}:
                if discord_status == "missing_thread":
                    LOG.warning(
                        "conditional report has no Discord thread event_id=%s root=%s",
                        event_id,
                        context.get("ceo_root_task_id"),
                    )
                    return True
                raise ConditionalNotificationError(
                    f"Discord conditional report failed: {discord_status}"
                )
        if self.mode == "delivery":
            return True

        workflows = self._related_workflows(context)
        for root_id, tasks in workflows:
            root_task = next(
                (task for task in tasks if _task_id(task) == root_id), tasks[0]
            )
            self._comment_once(root_task, marker, content)
            trading_tasks = [
                task
                for task in tasks
                if str(task.get("assignee") or "")
                == canonical_profile_for_department("trading")
            ]
            for task in trading_tasks:
                result = self.department_projection.project(
                    root_task_id=root_id,
                    task=task,
                    workflow_tasks=tasks,
                    event={"force_upsert": True, "correction": content},
                )
                if getattr(result, "status", "failed") == "failed":
                    raise ConditionalNotificationError(
                        "Trading Notion correction failed"
                    )
                self._comment_once(task, marker, content)
            synthesis_tasks = [
                task
                for task in tasks
                if workflow_role(task) == "synthesis" and action(task) == "SYNTHESIZE"
            ]
            for task in synthesis_tasks:
                result = self.ceo_projection.project(
                    root_task_id=root_id,
                    task=task,
                    workflow_tasks=tasks,
                    event={"force_upsert": True, "correction": content},
                )
                if result.get("status") == "failed":
                    raise ConditionalNotificationError("CEO Notion correction failed")
                self._comment_once(task, marker, content)
            if root_id == str(context.get("ceo_root_task_id") or ""):
                original_root = root_task
                if trading_tasks:
                    original_trading = trading_tasks[0]

        qa_marker = (
            "hgfinance.conditional-semantic-qa.v2 "
            f"event_id={event_id} verdict={qa.verdict}"
        )
        if not _comments_contain(original_root, qa_marker):
            if _publish_answer_qa(
                root_task=original_root,
                record=record,
                snapshot=snapshot,
                qa=qa,
            ):
                self._comment_once(
                    original_root,
                    qa_marker,
                    "권위 상태 답변의 로컬 QA 결과가 redacted LangSmith metadata로 기록되었습니다.",
                )

        return True


def _ensure_consumer_group(
    client: Any, *, stream: str, group: str, start_id: str
) -> None:
    """Create one durable group without initializing a business consumer."""

    try:
        client.xgroup_create(stream, group, id=start_id, mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


class RedisConditionalNotificationRunner:
    def __init__(
        self,
        client: Any,
        consumer: ConditionalRuleNotificationConsumer,
        *,
        stream: str = DEFAULT_STREAM,
        group: str = DEFAULT_GROUP,
        consumer_name: str | None = None,
        min_idle_ms: int = 2000,
        group_start_id: str = "0",
    ) -> None:
        self.client = client
        self.consumer = consumer
        self.stream = stream
        self.group = group
        self.consumer_name = consumer_name or f"{socket.gethostname()}-{os.getpid()}"
        self.min_idle_ms = max(1000, int(min_idle_ms))
        self.group_start_id = group_start_id

    def prepare(self) -> None:
        _ensure_consumer_group(
            self.client,
            stream=self.stream,
            group=self.group,
            start_id=self.group_start_id,
        )

    @staticmethod
    def _event(fields: Mapping[str, Any]) -> dict[str, Any]:
        event = dict(fields)
        payload = event.get("payload")
        if isinstance(payload, str):
            event["payload"] = json.loads(payload)
        return event

    def _process(self, message_id: str, fields: Mapping[str, Any]) -> int:
        terminal = self.consumer.handle_event(self._event(fields))
        if terminal:
            self.client.xack(self.stream, self.group, message_id)
            return 1
        return 0

    def _process_safely(self, message_id: str, fields: Mapping[str, Any]) -> int:
        try:
            return self._process(message_id, fields)
        except Exception:
            LOG.exception(
                "conditional reporting event failed message_id=%s", message_id
            )
            return 0

    def poll_once(self, *, block_ms: int = 1000) -> int:
        self.prepare()
        acknowledged = 0
        claimed = self.client.xautoclaim(
            self.stream,
            self.group,
            self.consumer_name,
            min_idle_time=self.min_idle_ms,
            start_id="0-0",
            count=10,
        )
        claimed_rows = (
            claimed[1]
            if isinstance(claimed, (tuple, list)) and len(claimed) > 1
            else ()
        )
        for message_id, fields in claimed_rows:
            acknowledged += self._process_safely(str(message_id), fields)
        rows = self.client.xreadgroup(
            self.group,
            self.consumer_name,
            {self.stream: ">"},
            count=10,
            block=max(1, int(block_ms)),
        )
        for _stream, messages in rows:
            for message_id, fields in messages:
                acknowledged += self._process_safely(str(message_id), fields)
        return acknowledged


def _discord_store(environment: Mapping[str, str]) -> DiscordIdempotencyStore:
    home = Path(environment.get("HERMES_HOME", "/opt/data"))
    profile_home = home / "profiles" / canonical_profile_for_department("ceo")
    return DiscordIdempotencyStore(profile_home if profile_home.is_dir() else home)


def build_runner(
    environment: Mapping[str, str] | None = None,
    *,
    mode: str = "all",
    group: str | None = None,
    group_start_id: str = "0",
) -> RedisConditionalNotificationRunner:
    env = dict(environment or os.environ)
    dsn = str(env.get("CONDITIONAL_RULE_DATABASE_URL") or "").strip()
    if not dsn:
        raise RuntimeError("CONDITIONAL_RULE_DATABASE_URL is required")
    redis_url = str(
        env.get("CONDITIONAL_RULE_EVENT_REDIS_URL") or env.get("REDIS_URL") or ""
    ).strip()
    if not redis_url:
        raise RuntimeError("REDIS_URL is required")
    kanban = HermesKanbanClient(environment=env) if mode != "delivery" else None
    rule_store = PostgresRuleWorkerStore(
        dsn,
        role=str(
            env.get("CONDITIONAL_RULE_WORKER_DATABASE_ROLE")
            or "svc_conditional_rule_worker"
        ),
    )
    consumer = ConditionalRuleNotificationConsumer(
        rule_store=rule_store,
        order_store=user_order_repository(),
        status_reader=read_paper_directive_status_for_admitted_authority,
        kanban_client=kanban,
        discord_delivery=(
            DiscordFinalDelivery(environment=env) if mode != "projection" else None
        ),
        discord_store=_discord_store(env) if mode != "projection" else None,
        ceo_projection=(
            CeoNotionProjection(env=env, kanban_client=kanban)
            if mode != "delivery"
            else None
        ),
        department_projection=(
            DepartmentNotionProjection(env=env) if mode != "delivery" else None
        ),
        mode=mode,
    )
    client = redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
        health_check_interval=30,
    )
    return RedisConditionalNotificationRunner(
        client,
        consumer,
        stream=str(env.get("CONDITIONAL_RULE_EVENT_STREAM") or DEFAULT_STREAM),
        group=(
            group
            or str(env.get("CONDITIONAL_RULE_NOTIFICATION_GROUP") or DEFAULT_GROUP)
        ),
        consumer_name=f"{socket.gethostname()}-{os.getpid()}-{mode}",
        min_idle_ms=int(env.get("CONDITIONAL_RULE_NOTIFICATION_RETRY_MS") or "2000"),
        group_start_id=group_start_id,
    )


def _run_forever(runner: RedisConditionalNotificationRunner) -> None:
    while True:
        try:
            runner.poll_once(block_ms=1000)
        except Exception:
            LOG.exception(
                "conditional %s cycle failed", runner.consumer.mode
            )
            time.sleep(1)


def _run_projection_lane(environment: Mapping[str, str], group: str) -> None:
    """Retry projection initialization without ever stopping Discord delivery."""

    while True:
        try:
            runner = build_runner(
                environment,
                mode="projection",
                group=group,
                group_start_id="$",
            )
            runner.prepare()
            _run_forever(runner)
        except Exception:
            LOG.exception("conditional projection lane initialization failed")
            time.sleep(2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    env = dict(os.environ)
    if args.healthcheck:
        from apps.api.conditional_rule_notification_health import main as health_main

        return health_main()

    delivery_runner = build_runner(env, mode="delivery")
    delivery_runner.prepare()
    projection_group = str(
        env.get("CONDITIONAL_RULE_PROJECTION_GROUP") or DEFAULT_PROJECTION_GROUP
    )
    # Reserve the durable projection cursor before accepting a new event. The
    # actual Notion/Kanban/LangSmith clients initialize independently below.
    _ensure_consumer_group(
        delivery_runner.client,
        stream=delivery_runner.stream,
        group=projection_group,
        start_id="$",
    )
    if args.once:
        try:
            projection_runner = build_runner(
                env,
                mode="projection",
                group=projection_group,
                group_start_id="$",
            )
            delivery_runner.poll_once(block_ms=1)
            projection_runner.poll_once(block_ms=1)
            return 0
        except Exception:
            LOG.exception("conditional reporting one-shot cycle failed")
            return 1

    projection_thread = threading.Thread(
        target=_run_projection_lane,
        args=(env, projection_group),
        name="conditional-projection",
        daemon=True,
    )
    projection_thread.start()
    _run_forever(delivery_runner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ConditionalNotificationError",
    "ConditionalRuleNotificationConsumer",
    "DEFAULT_PROJECTION_GROUP",
    "RedisConditionalNotificationRunner",
    "build_runner",
    "main",
]
