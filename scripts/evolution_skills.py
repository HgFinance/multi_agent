#!/usr/bin/env python3
"""Operator and daemon entrypoint for governed Evolution Skills."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from departments.worker_model_gateway import (  # noqa: E402
    ModelBinding,
    resolve,
    worker_llm,
)
from orchestration.evolution_skills import (  # noqa: E402
    OWNED_DEPARTMENTS,
    OWNER_TO_DEPARTMENT,
    PRODUCTION_GENERATION_MODEL,
    EvolutionSkillError,
    EvolutionSkillStore,
    Occurrence,
    active_registry_bindings,
    build_resolution_report,
    detect_candidates,
    inventory_skills,
    load_registry,
    promote_proposal,
    retire_skill,
    validate_canonical_registry,
)

DEFAULT_STATE_ROOT = Path(
    os.environ.get(
        "EVOLUTION_SKILLS_HOME", str(Path.home() / ".hermes/evolution-skills")
    )
)
DEFAULT_REGISTRY = ROOT / "skills/evolution-registry.json"


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _store(args: argparse.Namespace) -> EvolutionSkillStore:
    return EvolutionSkillStore(Path(args.state_root))


def _occurrences(store: EvolutionSkillStore, department: str) -> list[Occurrence]:
    return [
        Occurrence(
            kind=str(row.get("kind") or ""),
            detail=str(row.get("detail") or ""),
            run_id=str(row.get("run_id") or ""),
            symbol=str(row.get("symbol") or ""),
            at=str(row.get("at") or ""),
            department=str(row.get("department") or department),
            source_type=str(row.get("source_type") or "legacy"),
            source_artifact_id=str(row.get("source_artifact_id") or ""),
            benchmark_id=str(row.get("benchmark_id") or ""),
            improvement_type=str(row.get("improvement_type") or ""),
        )
        for row in store.load_occurrences(department=department)
    ]


def _proposal_history(
    store: EvolutionSkillStore,
) -> tuple[set[str], dict[str, set[str]]]:
    open_slugs: set[str] = set()
    consumed: dict[str, set[str]] = {}
    if not store.proposals_dir.exists():
        return open_slugs, consumed
    for state_path in store.proposals_dir.glob("*/state.json"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            provenance = json.loads(
                (state_path.parent / "provenance.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        slug = str(state.get("slug") or "")
        if not slug:
            continue
        if state.get("status") in {"PROPOSED", "VALIDATED", "APPROVED"}:
            open_slugs.add(slug)
        # Rejected evidence remains consumed. Replaying the exact same
        # evidence on every daemon cycle would bypass the reviewer decision.
        consumed.setdefault(slug, set()).update(
            str(run) for run in provenance.get("runs") or []
        )
    return open_slugs, consumed


def _binding(base_url: str) -> ModelBinding:
    env = dict(os.environ)
    env.update(
        {
            "WORKER_MODEL_BASE_URL": base_url,
            "WORKER_MODEL_EXECUTION_CONTEXT": "container",
            "WORKER_MODEL_NAME": PRODUCTION_GENERATION_MODEL,
        }
    )
    registry_path = env.get("WORKER_MODEL_REGISTRY_PATH") or str(
        ROOT / "departments/01-research/config/worker_model_registry.json"
    )
    binding = resolve(
        "skill-evolution-proposal-worker", env=env, registry_path=registry_path
    )
    if binding.base_model != PRODUCTION_GENERATION_MODEL:
        raise EvolutionSkillError("Evolution proposal binding is not governed 14B")
    return binding


def _run_proposals(args: argparse.Namespace, *, department: str | None = None) -> dict:
    store = _store(args)
    department = department or args.department
    open_slugs, consumed = _proposal_history(store)
    registry = load_registry(Path(args.registry))
    active_versions = {
        slug: int(entry.get("current_version") or 0)
        for slug, entry in registry["skills"].items()
        if entry.get("status") == "active"
    }
    candidates = detect_candidates(
        _occurrences(store, department),
        department=department,
        active_versions=active_versions,
        consumed_runs=consumed,
    )
    candidates = [candidate for candidate in candidates if candidate.slug not in open_slugs]
    if args.dry_run or not candidates:
        return {
            "department": department,
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "slug": candidate.slug,
                    "version": candidate.version,
                    "runs": list(candidate.runs),
                }
                for candidate in candidates
            ],
            "written": [],
        }
    binding = _binding(args.model_base_url)
    call = worker_llm(binding)
    written = []
    for candidate in candidates:
        state = store.create_proposal(
            candidate,
            lambda prompt: call(
                "Write only the requested Korean Hermes skill Markdown. Do not claim unobserved results.",
                prompt,
            ),
            model_metadata=binding.as_metadata(),
        )
        written.append(state)
    return {
        "department": department,
        "candidate_count": len(candidates),
        "model": binding.as_metadata(),
        "written": written,
    }


def _publish_pending_reviews(store: EvolutionSkillStore) -> dict[str, int]:
    from orchestration.qa_discord_feedback import (
        format_skill_proposal_request,
        post_qa_discord_message,
        qa_feedback_channel_id,
    )

    token = os.environ.get("DISCORD_BOT_TOKEN_QA", "").strip()
    channel_id = qa_feedback_channel_id()
    if not token or not channel_id:
        return {"delivered": 0, "failed": 0, "not_configured": 1}
    delivered = failed = 0
    for state in store.pending_review_proposals():
        proposal_id = str(state["proposal_id"])
        if not store.update_review_delivery(
            proposal_id, expected="PENDING", status="CLAIMED"
        ):
            continue
        content = format_skill_proposal_request(
            proposal_id=proposal_id,
            slug=str(state["slug"]),
            version=int(state["version"]),
            owner_profile=str(state["owner_profile"]),
            content_hash=str(state.get("content_hash") or ""),
            provenance_hash=str(state.get("provenance_hash") or ""),
            diff_hash=str(state.get("diff_hash") or ""),
            source_artifact_ids=state.get("source_artifact_ids") or (),
            benchmark_ids=state.get("benchmark_ids") or (),
            validation=state.get("validation") or {},
        )
        try:
            message_id = post_qa_discord_message(
                content, token=token, channel_id=channel_id
            )
        except Exception as exc:  # noqa: BLE001 - ambiguous delivery is terminal
            store.update_review_delivery(
                proposal_id,
                expected="CLAIMED",
                status="FAILED_FINAL",
                error_code=type(exc).__name__,
            )
            failed += 1
            continue
        store.update_review_delivery(
            proposal_id,
            expected="CLAIMED",
            status="DELIVERED",
            message_id=message_id,
        )
        delivered += 1
    return {"delivered": delivered, "failed": failed, "not_configured": 0}


def _publish_activation_notices(store: EvolutionSkillStore) -> dict[str, int]:
    from orchestration.qa_discord_feedback import (
        format_skill_activation_notice,
        post_qa_discord_message,
        qa_feedback_channel_id,
    )

    token = os.environ.get("DISCORD_BOT_TOKEN_QA", "").strip()
    channel_id = qa_feedback_channel_id()
    if not token or not channel_id:
        return {"delivered": 0, "failed": 0, "not_configured": 1}
    delivered = failed = 0
    for state in store.pending_activation_notices():
        proposal_id = str(state["proposal_id"])
        if not store.update_activation_delivery(
            proposal_id, expected="PENDING", status="CLAIMED"
        ):
            continue
        try:
            message_id = post_qa_discord_message(
                format_skill_activation_notice(
                    build_resolution_report(store, proposal_id)
                ),
                token=token,
                channel_id=channel_id,
            )
        except Exception as exc:  # noqa: BLE001 - ambiguous delivery is terminal
            store.update_activation_delivery(
                proposal_id,
                expected="CLAIMED",
                status="FAILED_FINAL",
                error_code=type(exc).__name__,
            )
            failed += 1
            continue
        store.update_activation_delivery(
            proposal_id,
            expected="CLAIMED",
            status="DELIVERED",
            message_id=message_id,
        )
        delivered += 1
    return {"delivered": delivered, "failed": failed, "not_configured": 0}


def _promote_approved(args: argparse.Namespace) -> dict[str, object]:
    store = _store(args)
    activated: list[str] = []
    failed: list[dict[str, str]] = []
    for state in store.pending_approved_proposals():
        proposal_id = str(state["proposal_id"])
        try:
            promote_proposal(
                store,
                proposal_id,
                repository_root=Path(args.repository_root),
                registry_path=Path(args.registry),
            )
            activated.append(proposal_id)
        except Exception as exc:  # noqa: BLE001 - isolate proposals in control loop
            failed.append(
                {"proposal_id": proposal_id, "error": f"{type(exc).__name__}: {exc}"}
            )
    return {"activated": activated, "failed": failed}


def cmd_ingest(args: argparse.Namespace) -> None:
    store = _store(args)
    rows = []
    for line in Path(args.input).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        rows.append(
            Occurrence(
                kind=str(raw.get("kind") or ""),
                detail=str(raw.get("detail") or ""),
                run_id=str(raw.get("run_id") or ""),
                symbol=str(raw.get("symbol") or ""),
                at=str(raw.get("at") or ""),
                department=str(raw.get("department") or args.department),
                source_type=str(raw.get("source_type") or "legacy"),
                source_artifact_id=str(raw.get("source_artifact_id") or ""),
                benchmark_id=str(raw.get("benchmark_id") or ""),
                improvement_type=str(raw.get("improvement_type") or ""),
            )
        )
    _print({"ingested": store.append_occurrences(rows), "state_root": str(store.root)})


def cmd_propose(args: argparse.Namespace) -> None:
    _print(_run_proposals(args))


def cmd_daemon(args: argparse.Namespace) -> None:
    interval = max(60, int(args.interval_seconds))
    while True:
        if args.feedback_state_path:
            try:
                from orchestration.langsmith_feedback import FeedbackLedger
                from orchestration.qa_feedback_benchmarks import (
                    run_pending_feedback_benchmarks,
                )
                from orchestration.qa_skill_evolution_bridge import (
                    process_qa_skill_feedback,
                )

                ledger = FeedbackLedger(args.feedback_state_path)
                _print(
                    {"qa_benchmarks": run_pending_feedback_benchmarks(ledger)}
                )
                _print(
                    {
                        "qa_feedback": process_qa_skill_feedback(
                            ledger, _store(args)
                        )
                    }
                )
            except Exception as exc:  # noqa: BLE001 - daemon reports and retries
                _print(
                    {
                        "qa_feedback_error": f"{type(exc).__name__}: {exc}",
                        "retry_seconds": interval,
                    }
                )
        for department in args.department:
            try:
                _print(_run_proposals(args, department=department))
            except Exception as exc:  # daemon reports and retries; it never promotes
                _print(
                    {
                        "department": department,
                        "error": f"{type(exc).__name__}: {exc}",
                        "retry_seconds": interval,
                    }
                )
        _print({"proposal_reviews": _publish_pending_reviews(_store(args))})
        time.sleep(interval)


def cmd_control_daemon(args: argparse.Namespace) -> None:
    """Promote only second-approved proposals from the isolated control plane."""

    interval = max(30, int(args.interval_seconds))
    while True:
        _print({"promotion": _promote_approved(args)})
        _print({"activation_notices": _publish_activation_notices(_store(args))})
        if args.once:
            return
        time.sleep(interval)


def cmd_approve(args: argparse.Namespace) -> None:
    _print(
        _store(args).approve(
            args.proposal_id, approved_by=args.approved_by, qa_verdict=args.qa_verdict
        )
    )


def cmd_promote(args: argparse.Namespace) -> None:
    _print(
        promote_proposal(
            _store(args),
            args.proposal_id,
            repository_root=ROOT,
            registry_path=Path(args.registry),
        )
    )


def cmd_feedback(args: argparse.Namespace) -> None:
    registry = load_registry(Path(args.registry))
    entry = registry["skills"].get(args.slug)
    if (
        not entry
        or entry.get("status") != "active"
        or int(entry.get("current_version") or 0) != args.version
    ):
        raise EvolutionSkillError(
            "feedback requires the active registered skill version"
        )
    owners = list(entry.get("owner_profiles") or [])
    if len(owners) != 1 or owners[0] not in OWNER_TO_DEPARTMENT:
        raise EvolutionSkillError("feedback skill owner is unresolved")
    _store(args).record_feedback(
        slug=args.slug,
        version=args.version,
        run_id=args.run_id,
        score=args.score,
        detail=args.detail,
        department=OWNER_TO_DEPARTMENT[owners[0]],
    )
    _print(
        {
            "recorded": True,
            "slug": args.slug,
            "version": args.version,
            "run_id": args.run_id,
        }
    )


def cmd_retire(args: argparse.Namespace) -> None:
    _print(
        retire_skill(
            _store(args),
            args.slug,
            repository_root=ROOT,
            registry_path=Path(args.registry),
            approved_by=args.approved_by,
            owner_profile=args.owner_profile,
            replacement=args.replacement,
            owner_approved_no_replacement=args.owner_approved_no_replacement,
        )
    )


def cmd_status(args: argparse.Namespace) -> None:
    store = _store(args)
    registry = load_registry(Path(args.registry))
    active, owners = active_registry_bindings(Path(args.registry))
    proposals = []
    if store.proposals_dir.exists():
        for path in sorted(store.proposals_dir.glob("*/state.json")):
            proposals.append(json.loads(path.read_text(encoding="utf-8")))
    _print(
        {
            "state_root": str(store.root),
            "occurrence_count": len(store.load_occurrences()),
            "candidate_count": sum(
                1
                for line in (
                    store.candidates_path.read_text(encoding="utf-8").splitlines()
                    if store.candidates_path.is_file()
                    else []
                )
                if line.strip()
            ),
            "proposal_count": len(proposals),
            "proposals": proposals,
            "active_skills": sorted(active),
            "owners": {name: sorted(value) for name, value in owners.items()},
            "registry_version": registry["registry_version"],
        }
    )


def cmd_report(args: argparse.Namespace) -> None:
    _print(build_resolution_report(_store(args), args.proposal_id))


def cmd_inventory(args: argparse.Namespace) -> None:
    roots = [Path(value) for value in (args.root or [])]
    if not roots:
        hermes_root = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        roots = [ROOT / "skills", hermes_root / "skills", hermes_root / "profiles"]
    report = inventory_skills(
        roots,
        repository_root=ROOT,
        registry_path=Path(args.registry),
    )
    output = _store(args).write_inventory(report)
    report["saved_to"] = str(output)
    _print(report)


def cmd_validate(args: argparse.Namespace) -> None:
    result = validate_canonical_registry(ROOT, Path(args.registry))
    _print(result)
    if not result["ok"]:
        raise EvolutionSkillError(
            f"canonical registry validation failed: {result['errors']}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest")
    ingest.add_argument("--department", required=True, choices=tuple(OWNED_DEPARTMENTS))
    ingest.add_argument("--input", required=True)
    ingest.set_defaults(func=cmd_ingest)

    propose = sub.add_parser("propose")
    propose.add_argument(
        "--department", required=True, choices=tuple(OWNED_DEPARTMENTS)
    )
    propose.add_argument(
        "--model-base-url",
        default=os.environ.get(
            "EVOLUTION_SKILL_MODEL_BASE_URL", "http://127.0.0.1:8000/v1"
        ),
    )
    propose.add_argument("--dry-run", action="store_true")
    propose.set_defaults(func=cmd_propose)

    daemon = sub.add_parser("daemon")
    daemon.add_argument(
        "--department",
        required=True,
        action="append",
        choices=tuple(OWNED_DEPARTMENTS),
    )
    daemon.add_argument(
        "--model-base-url",
        default=os.environ.get(
            "EVOLUTION_SKILL_MODEL_BASE_URL", "http://127.0.0.1:8000/v1"
        ),
    )
    daemon.add_argument("--dry-run", action="store_true")
    daemon.add_argument(
        "--feedback-state-path",
        default=os.environ.get("LANGSMITH_FEEDBACK_STATE_PATH", ""),
    )
    daemon.add_argument("--interval-seconds", type=int, default=900)
    daemon.set_defaults(func=cmd_daemon)

    control_daemon = sub.add_parser("control-daemon")
    control_daemon.add_argument("--repository-root", required=True)
    control_daemon.add_argument("--interval-seconds", type=int, default=60)
    control_daemon.add_argument("--once", action="store_true")
    control_daemon.set_defaults(func=cmd_control_daemon)

    approve = sub.add_parser("approve")
    approve.add_argument("proposal_id")
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--qa-verdict", required=True, choices=("PASS", "FAIL"))
    approve.set_defaults(func=cmd_approve)

    promote = sub.add_parser("promote")
    promote.add_argument("proposal_id")
    promote.set_defaults(func=cmd_promote)

    feedback = sub.add_parser("feedback")
    feedback.add_argument("--slug", required=True)
    feedback.add_argument("--version", required=True, type=int)
    feedback.add_argument("--run-id", required=True)
    feedback.add_argument("--score", required=True, type=float)
    feedback.add_argument("--detail", default="")
    feedback.set_defaults(func=cmd_feedback)

    retire = sub.add_parser("retire")
    retire.add_argument("--slug", required=True)
    retire.add_argument("--approved-by", required=True)
    retire.add_argument(
        "--owner-profile",
        required=True,
        choices=tuple(OWNER_TO_DEPARTMENT),
    )
    retire.add_argument("--replacement")
    retire.add_argument("--owner-approved-no-replacement", action="store_true")
    retire.set_defaults(func=cmd_retire)

    status = sub.add_parser("status")
    status.set_defaults(func=cmd_status)

    report = sub.add_parser("report")
    report.add_argument("proposal_id")
    report.set_defaults(func=cmd_report)

    inventory = sub.add_parser("inventory")
    inventory.add_argument("--root", action="append")
    inventory.set_defaults(func=cmd_inventory)

    validate = sub.add_parser("validate")
    validate.set_defaults(func=cmd_validate)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        args.func(args)
    except (EvolutionSkillError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"evolution-skills: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
