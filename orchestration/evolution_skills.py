"""Governed lifecycle for self-authored Hermes skills.

The model may draft a proposal, but it never writes the canonical skill tree.
Promotion is an explicit control-plane action after deterministic validation and
recorded QA/human approval. Runtime history lives outside git; active skill
source and its registry entry live in the repository.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = "hgfinance.evolution-skills.v1"
REGISTRY_VERSION = "hgfinance.evolution-skill-registry.v1"
_REGISTRY_SECTIONS = (
    ("skills", "evolved"),
    ("project_skills", "project-owned"),
)
PRODUCTION_GENERATION_MODEL = "qwen2.5-14b-instruct-awq"
OWNED_DEPARTMENTS = {
    "00-ceo-office": "ceo-agent",
    "01-research": "research-department",
    "02-trading": "trading-department",
    "03-risk": "risk-management",
    "04-quant-backtest": "quant-backtest-department",
    "05-accounting-portfolio": "accounting-portfolio-department",
    "06-ai-qa-audit": "qa-department",
    "07-agent-workforce": "hr-department",
}
OWNER_TO_DEPARTMENT = {
    owner: department for department, owner in OWNED_DEPARTMENTS.items()
}
TRACE_DEPARTMENT_TO_OWNER = {
    "ceo": "00-ceo-office",
    "ceo-agent": "00-ceo-office",
    "research": "01-research",
    "research-department": "01-research",
    "trading": "02-trading",
    "trading-department": "02-trading",
    "risk": "03-risk",
    "risk-management": "03-risk",
    "quant": "04-quant-backtest",
    "quant-backtest": "04-quant-backtest",
    "quant-backtest-department": "04-quant-backtest",
    "accounting": "05-accounting-portfolio",
    "accounting-portfolio": "05-accounting-portfolio",
    "accounting-portfolio-department": "05-accounting-portfolio",
    "qa": "06-ai-qa-audit",
    "qa-department": "06-ai-qa-audit",
    "hr": "07-agent-workforce",
    "hr-department": "07-agent-workforce",
}
MIN_OCCURRENCES = 3
MAX_SKILLS_PER_RUN = 2
PROPOSAL_STATES = frozenset(
    {"PROPOSED", "VALIDATED", "APPROVED", "ACTIVE", "SUPERSEDED", "RETIRED", "REJECTED"}
)
ALLOWED_TRANSITIONS = {
    "PROPOSED": frozenset({"REJECTED"}),
    "VALIDATED": frozenset({"APPROVED", "REJECTED"}),
    "APPROVED": frozenset({"ACTIVE", "REJECTED"}),
    "ACTIVE": frozenset({"SUPERSEDED", "RETIRED"}),
    "SUPERSEDED": frozenset({"RETIRED"}),
    "RETIRED": frozenset(),
    "REJECTED": frozenset(),
}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
_FORBIDDEN_IN_SKILL = (
    r"you\s+are\s+the\s+\w+[- ]agent",
    r"config\.yaml",
    r"SOUL\.md",
    r"승인\s*없이|권한\s*우회|통제\s*우회|건너뛴",
    r"personalities\s*:",
)
_PLACEHOLDERS = ("TODO", "TBD", "<스킬", "<도구>", "[SKILL_PRUNED]")


class EvolutionSkillError(ValueError):
    """A lifecycle transition or artifact violated the governed contract."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _restore_text(path: Path, previous: str | None) -> None:
    """Restore an atomic text snapshot after a failed multi-file promotion."""

    if previous is None:
        path.unlink(missing_ok=True)
        return
    _write_text_atomic(path, previous)


@contextmanager
def _proposal_state_lock(target: Path):
    """Serialize every cross-process mutation of one proposal state file."""

    lock_path = target / ".state.lock"
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+", encoding="utf-8") as lock:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield


def _append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True)
class Occurrence:
    kind: str
    detail: str = ""
    run_id: str = ""
    symbol: str = ""
    at: str = ""
    department: str = "01-research"
    source_type: str = "legacy"
    source_artifact_id: str = ""
    benchmark_id: str = ""
    improvement_type: str = ""


@dataclass(frozen=True)
class SkillCandidate:
    kind: str
    count: int
    runs: tuple[str, ...]
    samples: tuple[str, ...]
    department: str
    version: int = 1
    parent_version: int | None = None
    source_artifact_ids: tuple[str, ...] = ()
    benchmark_ids: tuple[str, ...] = ()
    improvement_type: str = "SKILL_CREATE"

    @property
    def slug(self) -> str:
        value = re.sub(r"[^a-z0-9]+", "-", self.kind.lower()).strip("-")
        value = value[:64].rstrip("-")
        return value or "unnamed"


def append_occurrences_to_path(
    path: Path,
    occurrences: Iterable[Occurrence],
) -> int:
    """Append valid occurrences once, with one lock covering read and write.

    Multiple producers share the same JSONL ledger. Deduplication outside the
    file lock allowed two concurrent writers to count one source run twice.
    """

    rows = [
        occurrence
        for occurrence in occurrences
        if occurrence.department in OWNED_DEPARTMENTS
        and occurrence.kind.strip()
        and occurrence.run_id.strip()
    ]
    if not rows:
        return 0

    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass

        handle.seek(0)
        existing: set[tuple[str, str, str]] = set()
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                key = (
                    str(item.get("department") or ""),
                    str(item.get("kind") or ""),
                    str(item.get("run_id") or ""),
                )
            except (AttributeError, TypeError, ValueError) as exc:
                raise EvolutionSkillError(
                    f"invalid occurrence ledger row {line_number}"
                ) from exc
            if key[2]:
                existing.add(key)

        pending: list[dict[str, Any]] = []
        for occurrence in rows:
            key = (
                occurrence.department,
                occurrence.kind.strip(),
                occurrence.run_id.strip(),
            )
            if key in existing:
                continue
            payload = asdict(occurrence)
            payload.update(
                {
                    "kind": key[1],
                    "run_id": key[2],
                    "recorded_at": _utcnow(),
                }
            )
            pending.append(payload)
            existing.add(key)

        if pending:
            handle.seek(0, os.SEEK_END)
            handle.writelines(
                json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                for item in pending
            )
            handle.flush()
            os.fsync(handle.fileno())
        return len(pending)


def detect_candidates(
    occurrences: Iterable[Occurrence],
    *,
    department: str,
    min_occurrences: int = MIN_OCCURRENCES,
    active_versions: Mapping[str, int] | None = None,
    consumed_runs: Mapping[str, Iterable[str]] | None = None,
) -> list[SkillCandidate]:
    """Select repeatable candidates from distinct, not-yet-consumed evidence."""

    if department not in OWNED_DEPARTMENTS:
        raise PermissionError(f"{department} is not allowed to author evolution skills")
    active_versions = active_versions or {}
    consumed = {name: set(runs) for name, runs in (consumed_runs or {}).items()}
    buckets: dict[str, list[Occurrence]] = {}
    for occurrence in occurrences:
        if occurrence.department != department or not occurrence.kind.strip():
            continue
        buckets.setdefault(occurrence.kind.strip(), []).append(occurrence)

    candidates: list[SkillCandidate] = []
    for kind, items in buckets.items():
        slug = SkillCandidate(kind, 0, (), (), department).slug
        # An independent evidence ID is mandatory. For direct runtime feedback
        # this is the execution ID; for QA feedback it is the semantic artifact
        # ID. A trace fan-out for one request must never masquerade as repeated
        # proof.
        usable = [
            item
            for item in items
            if item.run_id
            and item.run_id not in consumed.get(slug, set())
            and item.source_type in {"qa-benchmark", "skill-performance"}
        ]
        runs = tuple(sorted({item.run_id for item in usable if item.run_id}))
        distinct = len(runs)
        if distinct < min_occurrences:
            continue
        parent = active_versions.get(slug)
        requested_types = {
            item.improvement_type for item in usable if item.improvement_type
        }
        if "SKILL_EVOLVE" in requested_types and parent is None:
            # A requested evolution must bind to an active canonical parent;
            # silently turning it into a new skill would bypass owner review.
            continue
        candidates.append(
            SkillCandidate(
                kind=kind,
                count=distinct,
                runs=runs,
                samples=tuple(item.detail for item in usable if item.detail)[:5],
                department=department,
                version=(parent or 0) + 1,
                parent_version=parent,
                source_artifact_ids=tuple(
                    sorted(
                        {
                            item.source_artifact_id
                            for item in usable
                            if item.source_artifact_id
                        }
                    )
                ),
                benchmark_ids=tuple(
                    sorted({item.benchmark_id for item in usable if item.benchmark_id})
                ),
                improvement_type=("SKILL_EVOLVE" if parent else "SKILL_CREATE"),
            )
        )
    candidates.sort(key=lambda candidate: (-candidate.count, candidate.slug))
    return candidates[:MAX_SKILLS_PER_RUN]


def check_boundary(body: str) -> list[str]:
    hits: list[str] = []
    for pattern in _FORBIDDEN_IN_SKILL:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            hits.append(f"{pattern} -> {match.group(0)[:40]!r}")
    return hits


_DRAFT_PROMPT = """아래 반복 사건을 다음 실행에서 재사용할 수 있는 한국어 Hermes 스킬로 작성한다.

사건 유형: {kind}
서로 다른 실행 수: {count}
관측 사례:
{samples}

필수 형식:
- 첫 제목은 '# {slug}'
- '## 왜 필요한가', '## 작업 순서', '## 하지 않을 것'을 포함한다
- 관측된 사실과 재현 가능한 절차만 쓴다
- 프로필, 페르소나, 권한 또는 승인 절차를 재정의하지 않는다
- 코드 전체를 복사하지 말고 정본 경로와 검증 명령만 쓴다
- 미완성 표시나 가상의 출력은 넣지 않는다
- 500단어 이내
"""


def draft_body(candidate: SkillCandidate, llm: Callable[[str], str]) -> str | None:
    samples = "\n".join(f"- {sample}" for sample in candidate.samples) or "- 상세 없음"
    try:
        body = llm(
            _DRAFT_PROMPT.format(
                kind=candidate.kind,
                count=candidate.count,
                samples=samples,
                slug=candidate.slug,
            )
        )
    except Exception:
        return None
    return body.strip() if body and len(body.strip()) >= 80 else None


def render_skill(candidate: SkillCandidate, body: str) -> str:
    description = (
        f"{candidate.kind} 문제가 서로 다른 실행에서 반복될 때 사용하는 검증된 복구 절차. "
        "일회성 오류나 관측되지 않은 문제에는 사용하지 않는다."
    )
    frontmatter = {
        "name": candidate.slug,
        "description": description,
        "version": f"{candidate.version}.0.0",
        "metadata": {
            "hermes": {
                "tags": ["evolution", "observed-procedure"],
                "source": "skill-evolution-pipeline",
            }
        },
    }
    return (
        "---\n"
        + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        + "\n---\n\n"
        + body.strip()
        + "\n"
    )


def parse_skill_markdown(markdown: str) -> tuple[dict[str, Any], str]:
    if not markdown.startswith("---\n"):
        raise EvolutionSkillError("SKILL.md must start with YAML frontmatter")
    try:
        raw_frontmatter, body = markdown[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise EvolutionSkillError("SKILL.md frontmatter is not closed") from exc
    frontmatter = yaml.safe_load(raw_frontmatter)
    if not isinstance(frontmatter, dict):
        raise EvolutionSkillError("SKILL.md frontmatter must be a mapping")
    return frontmatter, body.strip()


def validate_artifacts(
    markdown: str,
    provenance: Mapping[str, Any],
    *,
    expected_slug: str,
    expected_version: int,
) -> dict[str, Any]:
    """Validate structure and governance invariants without an LLM."""

    errors: list[str] = []
    try:
        frontmatter, body = parse_skill_markdown(markdown)
    except EvolutionSkillError as exc:
        return {"ok": False, "errors": [str(exc)], "validated_at": _utcnow()}
    name = str(frontmatter.get("name") or "")
    if name != expected_slug or not _NAME_RE.fullmatch(name):
        errors.append("frontmatter name does not match the governed slug")
    if not str(frontmatter.get("description") or "").strip():
        errors.append("frontmatter description is required")
    if str(frontmatter.get("version") or "") != f"{expected_version}.0.0":
        errors.append("frontmatter version does not match proposal version")
    for heading in ("## 왜 필요한가", "## 작업 순서", "## 하지 않을 것"):
        if heading not in body:
            errors.append(f"required heading missing: {heading}")
    if not body.startswith(f"# {expected_slug}"):
        errors.append("body title does not match the governed slug")
    errors.extend(f"boundary violation: {hit}" for hit in check_boundary(body))
    errors.extend(
        f"unfinished placeholder: {token}"
        for token in _PLACEHOLDERS
        if token in markdown
    )
    if len(markdown) > 30_000:
        errors.append("skill entrypoint exceeds 30,000 characters")
    if provenance.get("schema_version") != SCHEMA_VERSION:
        errors.append("provenance schema version mismatch")
    if provenance.get("classification") != "evolved":
        errors.append("classification must be evolved")
    if provenance.get("generation_model") != PRODUCTION_GENERATION_MODEL:
        errors.append(
            "production evolution skills must be generated by the governed 14B model"
        )
    if (
        provenance.get("slug") != expected_slug
        or provenance.get("version") != expected_version
    ):
        errors.append("provenance identity mismatch")
    if int(provenance.get("occurrences") or 0) < MIN_OCCURRENCES:
        errors.append("fewer than three distinct occurrences")
    if len(set(provenance.get("runs") or [])) < MIN_OCCURRENCES:
        errors.append("fewer than three distinct run IDs")
    return {
        "ok": not errors,
        "errors": errors,
        "validated_at": _utcnow(),
        "content_hash": hashlib.sha256(markdown.encode()).hexdigest(),
    }


class EvolutionSkillStore:
    """Persistent proposal/event state outside the canonical repository tree."""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.occurrences_path = self.root / "occurrences.jsonl"
        self.candidates_path = self.root / "candidates.jsonl"
        self.events_path = self.root / "events.jsonl"
        self.feedback_path = self.root / "feedback.jsonl"
        self.proposals_dir = self.root / "proposals"

    def append_occurrences(self, occurrences: Iterable[Occurrence]) -> int:
        return append_occurrences_to_path(self.occurrences_path, occurrences)

    def load_occurrences(
        self, *, department: str | None = None
    ) -> list[dict[str, Any]]:
        if not self.occurrences_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.occurrences_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if department is None or row.get("department") == department:
                rows.append(row)
        return rows

    def proposal_dir(self, proposal_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9-]+-v[1-9][0-9]*-[a-f0-9]{12}", proposal_id):
            raise EvolutionSkillError("invalid proposal ID")
        return self.proposals_dir / proposal_id

    def record_candidate(self, candidate: SkillCandidate) -> str:
        identity = hashlib.sha256(
            json.dumps(
                {
                    "department": candidate.department,
                    "slug": candidate.slug,
                    "version": candidate.version,
                    "runs": candidate.runs,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:12]
        candidate_id = f"{candidate.slug}-v{candidate.version}-{identity}"
        existing: set[str] = set()
        if self.candidates_path.is_file():
            for line in self.candidates_path.read_text(encoding="utf-8").splitlines():
                try:
                    existing.add(str(json.loads(line).get("candidate_id") or ""))
                except (TypeError, ValueError):
                    continue
        if candidate_id not in existing:
            _append_jsonl(
                self.candidates_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "candidate_id": candidate_id,
                    "status": "CANDIDATE",
                    **asdict(candidate),
                    "slug": candidate.slug,
                    "detected_at": _utcnow(),
                },
            )
        return candidate_id

    def _proposal_markdown_for_version(self, slug: str, version: int) -> str:
        if version < 1 or not self.proposals_dir.is_dir():
            return ""
        for state_path in sorted(self.proposals_dir.glob("*/state.json")):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if state.get("slug") == slug and int(state.get("version") or 0) == version:
                skill_path = state_path.with_name("SKILL.md")
                if skill_path.is_file():
                    return skill_path.read_text(encoding="utf-8")
        return ""

    def create_proposal(
        self,
        candidate: SkillCandidate,
        llm: Callable[[str], str],
        *,
        model_metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        candidate_id = self.record_candidate(candidate)
        if model_metadata.get("model_version") != PRODUCTION_GENERATION_MODEL:
            raise EvolutionSkillError(
                "proposal generation must use the governed 14B model"
            )
        body = draft_body(candidate, llm)
        if body is None:
            raise EvolutionSkillError("LLM returned no usable skill body")
        markdown = render_skill(candidate, body)
        previous_markdown = self._proposal_markdown_for_version(
            candidate.slug, int(candidate.parent_version or 0)
        )
        proposal_diff = "".join(
            difflib.unified_diff(
                previous_markdown.splitlines(keepends=True),
                markdown.splitlines(keepends=True),
                fromfile=(
                    f"{candidate.slug}@v{candidate.parent_version}/SKILL.md"
                    if candidate.parent_version
                    else "/dev/null"
                ),
                tofile=f"{candidate.slug}@v{candidate.version}/SKILL.md",
            )
        )
        diff_hash = hashlib.sha256(proposal_diff.encode()).hexdigest()
        provenance = {
            "schema_version": SCHEMA_VERSION,
            "classification": "evolved",
            "generated_by": "skill-evolution-pipeline",
            "candidate_id": candidate_id,
            "generation_model": model_metadata["model_version"],
            "base_model": model_metadata.get("base_model"),
            "adapter_id": model_metadata.get("adapter_id"),
            "department": candidate.department,
            "owner_profile": OWNED_DEPARTMENTS[candidate.department],
            "slug": candidate.slug,
            "version": candidate.version,
            "parent_version": candidate.parent_version,
            "kind": candidate.kind,
            "occurrences": candidate.count,
            "runs": list(candidate.runs),
            "samples": list(candidate.samples),
            "source_artifact_ids": list(candidate.source_artifact_ids),
            "benchmark_ids": list(candidate.benchmark_ids),
            "improvement_type": candidate.improvement_type,
            "proposal_diff_hash": diff_hash,
            "generated_at": _utcnow(),
        }
        validation = validate_artifacts(
            markdown,
            provenance,
            expected_slug=candidate.slug,
            expected_version=candidate.version,
        )
        validation["stages"] = {
            "structure_and_provenance": "PASS" if validation["ok"] else "FAIL",
            # The generator emits Markdown/provenance only. It is forbidden
            # from creating or executing code; executable resources require a
            # separate reviewed implementation pipeline.
            "execution": "NOT_APPLICABLE_DOCUMENTATION_ONLY",
            "canonical_regression": "PENDING_PROMOTION",
        }
        identity = hashlib.sha256(
            f"{candidate.department}:{candidate.slug}:{candidate.version}:{validation.get('content_hash')}".encode()
        ).hexdigest()[:12]
        proposal_id = f"{candidate.slug}-v{candidate.version}-{identity}"
        target = self.proposal_dir(proposal_id)
        if target.exists():
            raise EvolutionSkillError(f"proposal already exists: {proposal_id}")
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text(markdown, encoding="utf-8")
        (target / "diff.patch").write_text(proposal_diff, encoding="utf-8")
        _write_json_atomic(target / "provenance.json", provenance)
        state = {
            "schema_version": SCHEMA_VERSION,
            "proposal_id": proposal_id,
            "slug": candidate.slug,
            "version": candidate.version,
            "owner_profile": OWNED_DEPARTMENTS[candidate.department],
            "status": "VALIDATED" if validation["ok"] else "PROPOSED",
            "validation": validation,
            "content_hash": validation.get("content_hash"),
            "provenance_hash": hashlib.sha256(_json_bytes(provenance)).hexdigest(),
            "diff_hash": diff_hash,
            "source_artifact_ids": list(candidate.source_artifact_ids),
            "benchmark_ids": list(candidate.benchmark_ids),
            "improvement_type": candidate.improvement_type,
            "approved_by": None,
            "qa_verdict": None,
            "review_delivery_status": "PENDING",
            "created_at": _utcnow(),
            "updated_at": _utcnow(),
        }
        _write_json_atomic(target / "state.json", state)
        self.record_event(proposal_id, "PROPOSED", {"validation_ok": validation["ok"]})
        if validation["ok"]:
            self.record_event(proposal_id, "VALIDATED", validation)
        return state

    def pending_review_proposals(self) -> list[dict[str, Any]]:
        """Return validated proposals that have not claimed a Discord card."""

        if not self.proposals_dir.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for state_path in sorted(self.proposals_dir.glob("*/state.json")):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                state.get("status") == "VALIDATED"
                and state.get("review_delivery_status", "PENDING") == "PENDING"
            ):
                rows.append(state)
        return rows

    def pending_approved_proposals(self) -> list[dict[str, Any]]:
        """Return only exact-hash proposals that passed the second approval."""

        if not self.proposals_dir.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for state_path in sorted(self.proposals_dir.glob("*/state.json")):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                state.get("status") == "APPROVED"
                and state.get("qa_verdict") == "PASS"
                and state.get("approved_by")
            ):
                rows.append(state)
        return rows

    def pending_activation_notices(self) -> list[dict[str, Any]]:
        """Return active proposals whose evidence card has not been claimed."""

        if not self.proposals_dir.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for state_path in sorted(self.proposals_dir.glob("*/state.json")):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                state.get("status") == "ACTIVE"
                and state.get("activation_delivery_status", "PENDING") == "PENDING"
            ):
                rows.append(state)
        return rows

    def update_review_delivery(
        self,
        proposal_id: str,
        *,
        expected: str,
        status: str,
        message_id: str = "",
        error_code: str = "",
    ) -> bool:
        """Atomically fence one proposal review delivery transition."""

        if status not in {"CLAIMED", "DELIVERED", "FAILED_FINAL"}:
            raise EvolutionSkillError("invalid proposal review delivery status")
        target = self.proposal_dir(proposal_id)
        state_path = target / "state.json"
        with _proposal_state_lock(target):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("review_delivery_status", "PENDING") != expected:
                return False
            state.update(
                {
                    "review_delivery_status": status,
                    "review_message_id": message_id or None,
                    "review_delivery_error": error_code or None,
                    "updated_at": _utcnow(),
                }
            )
            _write_json_atomic(state_path, state)
        self.record_event(
            proposal_id,
            f"REVIEW_{status}",
            {"message_id": message_id or None, "error_code": error_code or None},
        )
        return True

    def update_activation_delivery(
        self,
        proposal_id: str,
        *,
        expected: str,
        status: str,
        message_id: str = "",
        error_code: str = "",
    ) -> bool:
        """Atomically fence the one-shot activation evidence card."""

        if status not in {"CLAIMED", "DELIVERED", "FAILED_FINAL"}:
            raise EvolutionSkillError("invalid activation delivery status")
        target = self.proposal_dir(proposal_id)
        state_path = target / "state.json"
        with _proposal_state_lock(target):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("activation_delivery_status", "PENDING") != expected:
                return False
            state.update(
                {
                    "activation_delivery_status": status,
                    "activation_message_id": message_id or None,
                    "activation_delivery_error": error_code or None,
                    "updated_at": _utcnow(),
                }
            )
            _write_json_atomic(state_path, state)
        self.record_event(
            proposal_id,
            f"ACTIVATION_NOTICE_{status}",
            {"message_id": message_id or None, "error_code": error_code or None},
        )
        return True

    def load_proposal(self, proposal_id: str) -> tuple[Path, dict[str, Any]]:
        target = self.proposal_dir(proposal_id)
        state_path = target / "state.json"
        if not state_path.is_file():
            raise EvolutionSkillError(f"proposal not found: {proposal_id}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("status") not in PROPOSAL_STATES:
            raise EvolutionSkillError("unknown proposal lifecycle state")
        return target, state

    def approve(
        self,
        proposal_id: str,
        *,
        approved_by: str,
        qa_verdict: str,
        reason: str = "",
        decision_ref: str = "",
    ) -> dict[str, Any]:
        if qa_verdict not in {"PASS", "FAIL"} or not approved_by.strip():
            raise EvolutionSkillError(
                "review requires PASS or FAIL and a named approver"
            )
        target = self.proposal_dir(proposal_id)
        event = "APPROVED" if qa_verdict == "PASS" else "REJECTED"
        with _proposal_state_lock(target):
            _, state = self.load_proposal(proposal_id)
            if state["status"] != "VALIDATED":
                raise EvolutionSkillError("only a validated proposal can be approved")
            if qa_verdict == "FAIL":
                detail = {
                    "approved_by": approved_by.strip(),
                    "qa_verdict": "FAIL",
                    "review_reason": reason[:240],
                    "decision_ref": decision_ref[:160],
                    "reviewed_at": _utcnow(),
                }
            else:
                detail = {
                    "approved_by": approved_by.strip(),
                    "qa_verdict": qa_verdict,
                    "approved_at": _utcnow(),
                    "review_reason": reason[:240],
                    "decision_ref": decision_ref[:160],
                }
            state.update({"status": event, "updated_at": _utcnow(), **detail})
            _write_json_atomic(target / "state.json", state)
        self.record_event(
            proposal_id,
            event,
            {
                "approved_by": approved_by,
                "qa_verdict": qa_verdict,
                "reason": reason[:240],
                "decision_ref": decision_ref[:160],
                "content_hash": state.get("content_hash"),
                "provenance_hash": state.get("provenance_hash"),
                "diff_hash": state.get("diff_hash"),
            },
        )
        return state

    def set_status(
        self, proposal_id: str, status: str, detail: Mapping[str, Any]
    ) -> dict[str, Any]:
        if status not in PROPOSAL_STATES:
            raise EvolutionSkillError(f"unknown lifecycle state: {status}")
        target = self.proposal_dir(proposal_id)
        with _proposal_state_lock(target):
            _, state = self.load_proposal(proposal_id)
            current = str(state["status"])
            if status not in ALLOWED_TRANSITIONS[current]:
                raise EvolutionSkillError(
                    f"invalid lifecycle transition: {current} -> {status}"
                )
            state["status"] = status
            state["updated_at"] = _utcnow()
            state.update(detail)
            _write_json_atomic(target / "state.json", state)
        self.record_event(proposal_id, status, detail)
        return state

    def record_event(
        self, proposal_id: str, event: str, detail: Mapping[str, Any]
    ) -> None:
        _append_jsonl(
            self.events_path,
            {
                "schema_version": SCHEMA_VERSION,
                "proposal_id": proposal_id,
                "event": event,
                "detail": dict(detail),
                "at": _utcnow(),
            },
        )

    def record_feedback(
        self,
        *,
        slug: str,
        version: int,
        run_id: str,
        score: float,
        detail: str = "",
        department: str | None = None,
    ) -> None:
        if not _NAME_RE.fullmatch(slug) or not run_id.strip():
            raise EvolutionSkillError("feedback requires a valid slug and run ID")
        score_value = float(score)
        if not 0.0 <= score_value <= 1.0:
            raise EvolutionSkillError("feedback score must be between 0 and 1")
        if department is not None and department not in OWNED_DEPARTMENTS:
            raise EvolutionSkillError(
                "feedback department is not an evolution skill owner"
            )
        _append_jsonl(
            self.feedback_path,
            {
                "schema_version": SCHEMA_VERSION,
                "slug": slug,
                "version": int(version),
                "run_id": run_id.strip(),
                "score": score_value,
                "detail": detail[:500],
                "at": _utcnow(),
            },
        )
        # Three independent low-score executions become evidence for the next
        # version. Positive feedback is retained but never creates churn alone.
        if department and score_value < 0.5:
            self.append_occurrences(
                [
                    Occurrence(
                        kind=slug,
                        detail=(
                            f"active skill v{int(version)} low score {score_value:.3f}: {detail}"
                        )[:180],
                        run_id=run_id.strip(),
                        department=department,
                        source_type="skill-performance",
                        improvement_type="SKILL_EVOLVE",
                    )
                ]
            )

    def write_inventory(self, report: Mapping[str, Any]) -> Path:
        path = self.root / "inventory-latest.json"
        _write_json_atomic(path, dict(report))
        return path


def record_trace_occurrences(
    store: EvolutionSkillStore,
    *,
    department: str,
    run_id: str,
    finding_codes: Iterable[str],
    detail: str = "",
    at: str = "",
) -> int:
    """Convert deterministic trace findings into deduplicated occurrences."""

    owner_department = TRACE_DEPARTMENT_TO_OWNER.get(str(department).strip().lower())
    if not owner_department or not str(run_id).strip():
        return 0
    rows = []
    for raw_code in dict.fromkeys(str(code).strip() for code in finding_codes):
        if not raw_code:
            continue
        normalized = re.sub(r"[^a-z0-9]+", "-", raw_code.lower()).strip("-")
        if not normalized:
            continue
        rows.append(
            Occurrence(
                kind=f"trace-{normalized}",
                detail=detail[:180],
                run_id=str(run_id).strip(),
                at=at,
                department=owner_department,
            )
        )
    return store.append_occurrences(rows)


def record_qa_feedback_occurrences(
    store: EvolutionSkillStore,
    *,
    department: str,
    source_run_ids: Iterable[str],
    finding_codes: Iterable[str],
    detail: str,
    artifact_id: str,
    benchmark_id: str,
    improvement_type: str,
    target_skill_slug: str = "",
    at: str = "",
) -> int:
    """Admit only manager-approved, benchmark-passed QA evidence.

    The feedback SQLite ledger remains the approval/benchmark authority. This
    function projects one semantic QA artifact into the existing Evolution
    JSONL occurrence ledger. Source trace IDs are lineage within that artifact,
    not independent repetitions. The normal three-distinct-evidence gate must
    therefore be satisfied by three different approved artifacts.

    Reconciliation is safe to repeat because append_occurrences deduplicates by
    department, kind, and artifact ID.
    """

    owner_department = TRACE_DEPARTMENT_TO_OWNER.get(str(department).strip().lower())
    if not owner_department:
        return 0
    if improvement_type not in {"SKILL_CREATE", "SKILL_EVOLVE"}:
        return 0
    if not artifact_id.startswith("feedback-") or not benchmark_id.strip():
        raise EvolutionSkillError(
            "QA feedback occurrence requires artifact and passed benchmark IDs"
        )
    normalized_findings = sorted(
        {
            re.sub(r"[^a-z0-9]+", "-", str(code).lower()).strip("-")
            for code in finding_codes
            if str(code).strip()
        }
    )
    if improvement_type == "SKILL_EVOLVE":
        if not _NAME_RE.fullmatch(target_skill_slug):
            raise EvolutionSkillError(
                "SKILL_EVOLVE requires a canonical target skill slug"
            )
        kind = target_skill_slug
    else:
        if not normalized_findings:
            raise EvolutionSkillError("SKILL_CREATE requires a finding code")
        kind = f"{owner_department}-{normalized_findings[0]}"

    source_runs = {
        str(value).strip() for value in source_run_ids if str(value).strip()
    }
    if not source_runs:
        raise EvolutionSkillError("QA feedback occurrence requires source lineage")
    return store.append_occurrences(
        [
            Occurrence(
                kind=kind,
                detail=detail[:180],
                run_id=artifact_id,
                at=at,
                department=owner_department,
                source_type="qa-benchmark",
                source_artifact_id=artifact_id,
                benchmark_id=benchmark_id.strip(),
                improvement_type=improvement_type,
            )
        ]
    )


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def build_resolution_report(
    store: EvolutionSkillStore, proposal_id: str
) -> dict[str, Any]:
    """Build one auditable problem-to-outcome view without claiming early success."""

    target, state = store.load_proposal(proposal_id)
    provenance = json.loads((target / "provenance.json").read_text(encoding="utf-8"))
    events = [
        row
        for row in _load_jsonl(store.events_path)
        if row.get("proposal_id") == proposal_id
    ]
    feedback = [
        row
        for row in _load_jsonl(store.feedback_path)
        if row.get("slug") == state.get("slug")
        and int(row.get("version") or 0) == int(state.get("version") or 0)
    ]
    distinct_feedback = {
        str(row.get("run_id") or ""): row
        for row in feedback
        if str(row.get("run_id") or "")
    }
    scores = [float(row["score"]) for row in distinct_feedback.values()]
    mean_score = sum(scores) / len(scores) if scores else None
    lifecycle = str(state.get("status") or "UNKNOWN")
    if lifecycle == "ACTIVE" and len(scores) >= MIN_OCCURRENCES:
        if all(score >= 0.8 for score in scores):
            outcome = "VERIFIED_IMPROVED"
        elif all(score < 0.5 for score in scores):
            outcome = "REGRESSION_CANDIDATE"
        else:
            outcome = "POST_ACTIVATION_REVIEW_REQUIRED"
    elif lifecycle == "ACTIVE":
        outcome = "ACTIVE_PENDING_FEEDBACK"
    elif lifecycle == "APPROVED":
        outcome = "APPROVED_PENDING_PROMOTION"
    elif lifecycle == "VALIDATED":
        outcome = "VALIDATED_PENDING_SECOND_APPROVAL"
    else:
        outcome = lifecycle
    return {
        "schema_version": f"{SCHEMA_VERSION}.resolution.v1",
        "proposal_id": proposal_id,
        "skill": {
            "slug": state.get("slug"),
            "version": state.get("version"),
            "owner_profile": state.get("owner_profile"),
            "content_hash": state.get("content_hash"),
            "provenance_hash": state.get("provenance_hash"),
            "diff_hash": state.get("diff_hash"),
        },
        "problem_evidence": {
            "kind": provenance.get("kind"),
            "samples": provenance.get("samples") or [],
            "source_run_ids": provenance.get("runs") or [],
            "source_artifact_ids": provenance.get("source_artifact_ids") or [],
            "baseline_benchmark_ids": provenance.get("benchmark_ids") or [],
        },
        "change_evidence": {
            "generation_model": provenance.get("generation_model"),
            "parent_version": provenance.get("parent_version"),
            "validation": state.get("validation"),
            "approved_by": state.get("approved_by"),
            "approved_at": state.get("approved_at"),
            "activated_at": state.get("activated_at"),
            "canonical_path": state.get("canonical_path"),
        },
        "outcome_evidence": {
            "status": outcome,
            "required_distinct_runs": MIN_OCCURRENCES,
            "observed_distinct_runs": len(scores),
            "mean_score": round(mean_score, 6) if mean_score is not None else None,
            "runs": sorted(distinct_feedback),
            "claim": (
                "반복 운영 성과로 개선 확인"
                if outcome == "VERIFIED_IMPROVED"
                else "아직 해결 완료로 판정하지 않음"
            ),
        },
        "lifecycle_events": events,
    }


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "registry_version": REGISTRY_VERSION,
            "skills": {},
            "project_skills": {},
        }
    registry = json.loads(path.read_text(encoding="utf-8"))
    if registry.get("registry_version") != REGISTRY_VERSION or not isinstance(
        registry.get("skills"), dict
    ):
        raise EvolutionSkillError("invalid evolution skill registry")
    if not isinstance(registry.setdefault("project_skills", {}), dict):
        raise EvolutionSkillError("invalid project skill registry")
    return registry


def promote_proposal(
    store: EvolutionSkillStore,
    proposal_id: str,
    *,
    repository_root: Path,
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """Promote one approved proposal into the canonical repository tree."""

    target, state = store.load_proposal(proposal_id)
    if (
        state["status"] != "APPROVED"
        or state.get("qa_verdict") != "PASS"
        or not state.get("approved_by")
    ):
        raise EvolutionSkillError("promotion requires recorded QA PASS and approval")
    markdown = (target / "SKILL.md").read_text(encoding="utf-8")
    proposal_diff = (target / "diff.patch").read_text(encoding="utf-8")
    provenance = json.loads((target / "provenance.json").read_text(encoding="utf-8"))
    validation = validate_artifacts(
        markdown,
        provenance,
        expected_slug=state["slug"],
        expected_version=int(state["version"]),
    )
    if not validation["ok"] or validation["content_hash"] != state["validation"].get(
        "content_hash"
    ):
        raise EvolutionSkillError(
            f"proposal changed after validation: {validation['errors']}"
        )
    if hashlib.sha256(proposal_diff.encode()).hexdigest() != state.get("diff_hash"):
        raise EvolutionSkillError("proposal diff changed after validation")

    repo = repository_root.resolve()
    registry_file = (registry_path or repo / "skills/evolution-registry.json").resolve()
    canonical_dir = repo / "skills/evolved" / state["slug"]
    colliding_sources = [
        path
        for path in (repo / "skills").rglob("SKILL.md")
        if path.parent.name == state["slug"]
        and path.parent.resolve() != canonical_dir.resolve()
    ]
    if colliding_sources:
        raise EvolutionSkillError(
            f"skill slug collides with an existing project-owned source: {colliding_sources[0]}"
        )
    canonical_dir.mkdir(parents=True, exist_ok=True)
    skill_path = canonical_dir / "SKILL.md"
    provenance_path = canonical_dir / "provenance.json"
    existing_registry = load_registry(registry_file)
    existing_regression = validate_canonical_registry(repo, registry_file)
    if not existing_regression["ok"]:
        raise EvolutionSkillError(
            "existing canonical registry failed regression validation: "
            f"{existing_regression['errors']}"
        )
    existing = existing_registry["skills"].get(state["slug"])
    if (
        existing
        and existing.get("proposal_id") == proposal_id
        and existing.get("status") == "active"
        and existing.get("content_hash") == validation["content_hash"]
    ):
        regression = validate_canonical_registry(repo, registry_file)
        if not regression["ok"]:
            raise EvolutionSkillError(
                f"partially promoted registry failed validation: {regression['errors']}"
            )
        return store.set_status(
            proposal_id,
            "ACTIVE",
            {
                "activated_at": existing.get("activated_at") or _utcnow(),
                "canonical_path": str(skill_path),
                "regression_validation": regression,
                "activation_delivery_status": "PENDING",
                "promotion_reconciled": True,
            },
        )
    if existing and int(existing.get("current_version") or 0) >= int(state["version"]):
        raise EvolutionSkillError("registry already contains this or a newer version")
    if existing and existing.get("status") != "active":
        raise EvolutionSkillError(
            "retired skill cannot be overwritten; create a new slug"
        )

    canonical_provenance = dict(provenance)
    canonical_provenance.update(
        {
            "approved_by": state["approved_by"],
            "qa_verdict": state["qa_verdict"],
            "activated_at": _utcnow(),
            "content_hash": validation["content_hash"],
        }
    )
    previous_skill = (
        skill_path.read_text(encoding="utf-8") if skill_path.is_file() else None
    )
    previous_provenance = (
        provenance_path.read_text(encoding="utf-8")
        if provenance_path.is_file()
        else None
    )
    previous_registry = (
        registry_file.read_text(encoding="utf-8") if registry_file.is_file() else None
    )
    existing_registry["skills"][state["slug"]] = {
        "classification": "evolved",
        "status": "active",
        "owner_profiles": [state["owner_profile"]],
        "current_version": int(state["version"]),
        "source": str(skill_path.relative_to(repo)),
        "content_hash": validation["content_hash"],
        "approved_by": state["approved_by"],
        "qa_verdict": state["qa_verdict"],
        "activated_at": canonical_provenance["activated_at"],
        "replacement": None,
        "proposal_id": proposal_id,
    }
    try:
        _write_text_atomic(skill_path, markdown)
        _write_json_atomic(provenance_path, canonical_provenance)
        _write_json_atomic(registry_file, existing_registry)
        regression = validate_canonical_registry(repo, registry_file)
        if not regression["ok"]:
            raise EvolutionSkillError(
                f"canonical registry regression failed: {regression['errors']}"
            )
    except Exception:
        _restore_text(skill_path, previous_skill)
        _restore_text(provenance_path, previous_provenance)
        _restore_text(registry_file, previous_registry)
        raise

    if existing:
        previous_id = existing.get("proposal_id")
        if previous_id:
            store.set_status(
                str(previous_id), "SUPERSEDED", {"superseded_by": proposal_id}
            )
    return store.set_status(
        proposal_id,
        "ACTIVE",
        {
            "activated_at": canonical_provenance["activated_at"],
            "canonical_path": str(skill_path),
            "regression_validation": regression,
            "activation_delivery_status": "PENDING",
        },
    )


def retire_skill(
    store: EvolutionSkillStore,
    slug: str,
    *,
    repository_root: Path,
    approved_by: str,
    owner_profile: str,
    replacement: str | None = None,
    owner_approved_no_replacement: bool = False,
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """Retire without deleting source; zero usage is never sufficient."""

    if not approved_by.strip():
        raise EvolutionSkillError("retirement requires a named approver")
    if not replacement and not owner_approved_no_replacement:
        raise EvolutionSkillError(
            "retirement requires a replacement or explicit no-replacement approval"
        )
    repo = repository_root.resolve()
    registry_file = (registry_path or repo / "skills/evolution-registry.json").resolve()
    registry = load_registry(registry_file)
    entry = registry["skills"].get(slug)
    if not entry or entry.get("status") != "active":
        raise EvolutionSkillError("only an active evolved skill can be retired")
    if owner_profile not in set(entry.get("owner_profiles") or []):
        raise EvolutionSkillError(
            "retirement approval must name a registered owner profile"
        )
    if replacement:
        replacement_entry = registry["skills"].get(replacement)
        if not replacement_entry or replacement_entry.get("status") != "active":
            raise EvolutionSkillError("replacement must be an active evolved skill")
        if not set(entry.get("owner_profiles") or []).issubset(
            set(replacement_entry.get("owner_profiles") or [])
        ):
            raise EvolutionSkillError(
                "replacement does not cover the retiring skill owners"
            )
    entry.update(
        {
            "status": "retired",
            "retired_at": _utcnow(),
            "retired_by": approved_by.strip(),
            "retired_owner_profile": owner_profile,
            "replacement": replacement,
            "owner_approved_no_replacement": bool(owner_approved_no_replacement),
        }
    )
    _write_json_atomic(registry_file, registry)
    proposal_id = entry.get("proposal_id")
    if proposal_id:
        store.set_status(
            str(proposal_id),
            "RETIRED",
            {"retired_by": approved_by, "replacement": replacement},
        )
    return entry


def active_registry_bindings(
    path: Path,
) -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    registry = load_registry(path)
    active: set[str] = set()
    owners: dict[str, frozenset[str]] = {}
    for section, classification in _REGISTRY_SECTIONS:
        for slug, entry in registry[section].items():
            if slug in owners:
                raise EvolutionSkillError(f"duplicate registered skill name: {slug}")
            if not _NAME_RE.fullmatch(slug):
                raise EvolutionSkillError(
                    f"invalid {classification} skill name: {slug}"
                )
            if (
                not isinstance(entry, Mapping)
                or entry.get("classification") != classification
            ):
                raise EvolutionSkillError(
                    f"invalid {classification} skill classification: {slug}"
                )
            owner_set = frozenset(
                str(owner) for owner in entry.get("owner_profiles") or []
            )
            if not owner_set or not owner_set.issubset(
                set(OWNED_DEPARTMENTS.values())
            ):
                raise EvolutionSkillError(
                    f"invalid {classification} skill owners: {slug}"
                )
            owners[slug] = owner_set
            if entry.get("status") == "active":
                active.add(slug)
    return frozenset(active), owners


def validate_canonical_registry(
    repository_root: Path,
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """Regression-check every registered source and provenance hash."""

    repo = repository_root.expanduser().resolve()
    registry_file = (registry_path or repo / "skills/evolution-registry.json").resolve()
    registry = load_registry(registry_file)
    errors: list[str] = []
    checked: list[str] = []
    entries = [
        (slug, entry, classification)
        for section, classification in _REGISTRY_SECTIONS
        for slug, entry in registry[section].items()
    ]
    if len({slug for slug, _entry, _classification in entries}) != len(entries):
        errors.append("duplicate skill slug across registry sections")
    for slug, entry, classification in sorted(entries, key=lambda item: item[0]):
        try:
            source = (repo / str(entry.get("source") or "")).resolve()
            source.relative_to(repo)
        except (ValueError, OSError):
            errors.append(f"{slug}: source escapes repository")
            continue
        raw_provenance = (
            entry.get("provenance")
            if classification == "project-owned"
            else source.with_name("provenance.json").relative_to(repo)
        )
        try:
            provenance_path = (repo / str(raw_provenance or "")).resolve()
            provenance_path.relative_to(repo)
        except (ValueError, OSError):
            errors.append(f"{slug}: provenance escapes repository")
            continue
        if not source.is_file() or not provenance_path.is_file():
            errors.append(f"{slug}: canonical source or provenance missing")
            continue
        try:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            markdown = source.read_text(encoding="utf-8")
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"{slug}: {type(exc).__name__}")
            continue
        if classification == "evolved":
            result = validate_artifacts(
                markdown,
                provenance,
                expected_slug=slug,
                expected_version=int(entry.get("current_version") or 0),
            )
            if not result["ok"]:
                errors.extend(f"{slug}: {message}" for message in result["errors"])
            if result.get("content_hash") != entry.get("content_hash"):
                errors.append(f"{slug}: registry content hash mismatch")
            if provenance.get("approved_by") != entry.get("approved_by"):
                errors.append(f"{slug}: approval provenance mismatch")
        else:
            content_hash = hashlib.sha256(markdown.encode()).hexdigest()
            owner_profiles = set(entry.get("owner_profiles") or [])
            if provenance.get("classification") != "project-owned":
                errors.append(f"{slug}: project provenance classification mismatch")
            if provenance.get("slug") != slug:
                errors.append(f"{slug}: project provenance slug mismatch")
            if provenance.get("owner_profile") not in owner_profiles:
                errors.append(f"{slug}: project provenance owner mismatch")
            if str(provenance.get("version") or "") != str(
                entry.get("current_version") or ""
            ):
                errors.append(f"{slug}: project provenance version mismatch")
            if content_hash != provenance.get("content_hash"):
                errors.append(f"{slug}: project provenance content hash mismatch")
            if content_hash != entry.get("content_hash"):
                errors.append(f"{slug}: registry content hash mismatch")
        checked.append(slug)
    return {
        "ok": not errors,
        "checked": checked,
        "errors": errors,
        "validated_at": _utcnow(),
    }


def inventory_skills(
    roots: Iterable[Path],
    *,
    repository_root: Path,
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """Classify skill sources without using access counts or deleting files."""

    roots = tuple(Path(root) for root in roots)
    repo = repository_root.expanduser().resolve()
    project_skills = (repo / "skills").resolve()
    registry_file = registry_path or project_skills / "evolution-registry.json"
    registry = load_registry(registry_file)
    evolved = registry["skills"]
    entries: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for raw_root in roots:
        root = raw_root.expanduser().resolve()
        if not root.is_dir():
            continue
        bundled_names: set[str] = set()
        for manifest in root.rglob(".bundled_manifest"):
            try:
                bundled_names.update(
                    line.split(":", 1)[0].strip()
                    for line in manifest.read_text(encoding="utf-8").splitlines()
                    if ":" in line
                )
            except OSError:
                continue
        for skill_path in root.rglob("SKILL.md"):
            skill_path = skill_path.resolve()
            if skill_path in seen_paths:
                continue
            seen_paths.add(skill_path)
            slug = skill_path.parent.name
            provenance_path = skill_path.with_name("provenance.json")
            provenance: dict[str, Any] = {}
            if provenance_path.is_file():
                try:
                    raw = json.loads(provenance_path.read_text(encoding="utf-8"))
                    provenance = raw if isinstance(raw, dict) else {}
                except (OSError, ValueError):
                    provenance = {}
            parts = {part.lower() for part in skill_path.parts}
            if provenance.get("classification") == "evolved" or slug in evolved:
                classification = "evolved"
            elif (
                "proposals" in parts or ".cache" in parts or "generated-cache" in parts
            ):
                classification = "generated-cache"
            elif skill_path.is_relative_to(project_skills):
                classification = "project-owned"
            elif slug in bundled_names:
                classification = "bundled"
            else:
                classification = "legacy-custom"
            entries.append(
                {
                    "name": slug,
                    "path": str(skill_path),
                    "classification": classification,
                    "provenance_present": provenance_path.is_file(),
                    "owner_profile": provenance.get("owner_profile"),
                    "version": provenance.get("version"),
                    "status": (evolved.get(slug) or {}).get("status"),
                    # Inventory is a quality signal. It never authorizes removal.
                    "removal_action": "preserve",
                }
            )
    counts: dict[str, int] = {}
    for entry in entries:
        label = str(entry["classification"])
        counts[label] = counts.get(label, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utcnow(),
        "roots": [str(Path(root).expanduser().resolve()) for root in roots],
        "counts": dict(sorted(counts.items())),
        "entries": sorted(
            entries, key=lambda entry: (str(entry["name"]), str(entry["path"]))
        ),
        "deletion_policy": "usage counts are quality signals only; inventory never deletes",
    }


__all__ = [
    "MAX_SKILLS_PER_RUN",
    "MIN_OCCURRENCES",
    "OWNED_DEPARTMENTS",
    "OWNER_TO_DEPARTMENT",
    "PRODUCTION_GENERATION_MODEL",
    "REGISTRY_VERSION",
    "SCHEMA_VERSION",
    "EvolutionSkillError",
    "EvolutionSkillStore",
    "Occurrence",
    "SkillCandidate",
    "active_registry_bindings",
    "append_occurrences_to_path",
    "build_resolution_report",
    "check_boundary",
    "detect_candidates",
    "draft_body",
    "inventory_skills",
    "load_registry",
    "promote_proposal",
    "record_qa_feedback_occurrences",
    "record_trace_occurrences",
    "render_skill",
    "retire_skill",
    "validate_artifacts",
    "validate_canonical_registry",
]
