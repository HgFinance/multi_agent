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

from departments.worker_model_gateway import ModelBinding, worker_llm
from orchestration.evolution_skills import (
    PRODUCTION_GENERATION_MODEL,
    EvolutionSkillError,
    EvolutionSkillStore,
    Occurrence,
    active_registry_bindings,
    detect_candidates,
    inventory_skills,
    load_registry,
    promote_proposal,
    retire_skill,
    validate_canonical_registry,
)

DEFAULT_STATE_ROOT = Path(
    os.environ.get("EVOLUTION_SKILLS_HOME", str(Path.home() / ".hermes/evolution-skills"))
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
        )
        for row in store.load_occurrences(department=department)
    ]


def _proposal_history(store: EvolutionSkillStore) -> tuple[dict[str, int], dict[str, set[str]]]:
    versions: dict[str, int] = {}
    consumed: dict[str, set[str]] = {}
    if not store.proposals_dir.exists():
        return versions, consumed
    for state_path in store.proposals_dir.glob("*/state.json"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            provenance = json.loads((state_path.parent / "provenance.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if state.get("status") == "REJECTED":
            continue
        slug = str(state.get("slug") or "")
        versions[slug] = max(versions.get(slug, 0), int(state.get("version") or 0))
        consumed.setdefault(slug, set()).update(str(run) for run in provenance.get("runs") or [])
    return versions, consumed


def _binding(base_url: str) -> ModelBinding:
    return ModelBinding(
        provider="vllm-openai",
        base_url=base_url.rstrip("/") if base_url.rstrip("/").endswith("/v1") else base_url.rstrip("/") + "/v1",
        model=PRODUCTION_GENERATION_MODEL,
        base_model=PRODUCTION_GENERATION_MODEL,
        adapter_id=None,
        adapter_version="none",
        api_key=os.environ.get("WORKER_MODEL_API_KEY", "vllm"),
        timeout_seconds=float(os.environ.get("WORKER_MODEL_TIMEOUT_SECONDS", "120")),
    )


def _run_proposals(args: argparse.Namespace, *, department: str | None = None) -> dict:
    store = _store(args)
    department = department or args.department
    proposal_versions, consumed = _proposal_history(store)
    registry = load_registry(Path(args.registry))
    active_versions = {
        slug: int(entry.get("current_version") or 0)
        for slug, entry in registry["skills"].items()
        if entry.get("status") == "active"
    }
    for slug, version in proposal_versions.items():
        active_versions[slug] = max(active_versions.get(slug, 0), version)
    candidates = detect_candidates(
        _occurrences(store, department),
        department=department,
        active_versions=active_versions,
        consumed_runs=consumed,
    )
    if args.dry_run or not candidates:
        return {
            "department": department,
            "candidate_count": len(candidates),
            "candidates": [
                {"slug": candidate.slug, "version": candidate.version, "runs": list(candidate.runs)}
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
            )
        )
    _print({"ingested": store.append_occurrences(rows), "state_root": str(store.root)})


def cmd_propose(args: argparse.Namespace) -> None:
    _print(_run_proposals(args))


def cmd_daemon(args: argparse.Namespace) -> None:
    interval = max(60, int(args.interval_seconds))
    while True:
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
        time.sleep(interval)


def cmd_approve(args: argparse.Namespace) -> None:
    _print(_store(args).approve(args.proposal_id, approved_by=args.approved_by, qa_verdict=args.qa_verdict))


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
    _store(args).record_feedback(
        slug=args.slug,
        version=args.version,
        run_id=args.run_id,
        score=args.score,
        detail=args.detail,
    )
    _print({"recorded": True, "slug": args.slug, "version": args.version, "run_id": args.run_id})


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
        raise EvolutionSkillError(f"canonical registry validation failed: {result['errors']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest")
    ingest.add_argument("--department", required=True, choices=("01-research", "04-quant-backtest"))
    ingest.add_argument("--input", required=True)
    ingest.set_defaults(func=cmd_ingest)

    propose = sub.add_parser("propose")
    propose.add_argument("--department", required=True, choices=("01-research", "04-quant-backtest"))
    propose.add_argument(
        "--model-base-url",
        default=os.environ.get("EVOLUTION_SKILL_MODEL_BASE_URL", "http://127.0.0.1:8000/v1"),
    )
    propose.add_argument("--dry-run", action="store_true")
    propose.set_defaults(func=cmd_propose)

    daemon = sub.add_parser("daemon")
    daemon.add_argument(
        "--department",
        required=True,
        action="append",
        choices=("01-research", "04-quant-backtest"),
    )
    daemon.add_argument(
        "--model-base-url",
        default=os.environ.get("EVOLUTION_SKILL_MODEL_BASE_URL", "http://127.0.0.1:8000/v1"),
    )
    daemon.add_argument("--dry-run", action="store_true")
    daemon.add_argument("--interval-seconds", type=int, default=900)
    daemon.set_defaults(func=cmd_daemon)

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
        choices=("research-department", "quant-backtest-department"),
    )
    retire.add_argument("--replacement")
    retire.add_argument("--owner-approved-no-replacement", action="store_true")
    retire.set_defaults(func=cmd_retire)

    status = sub.add_parser("status")
    status.set_defaults(func=cmd_status)

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
