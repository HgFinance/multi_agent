"""Policy corpus readiness checks for request-time QA evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER_MARKER = "SAMPLE_PLACEHOLDER"


class PolicyCorpusError(RuntimeError):
    """Raised when a corpus is not safe for production QA."""


@dataclass(frozen=True)
class PolicyCorpusStatus:
    directory: str
    document_count: int
    placeholder_count: int
    corpus_hash: str
    ready: bool
    reason: str | None


def inspect_policy_corpus(directory: Path) -> PolicyCorpusStatus:
    paths = tuple(sorted(path for path in directory.glob("*.md") if path.is_file()))
    digest = hashlib.sha256()
    placeholder_count = 0
    for path in paths:
        content = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        if PLACEHOLDER_MARKER.encode("utf-8") in content:
            placeholder_count += 1
    reason: str | None = None
    if not paths:
        reason = "no policy documents found"
    elif placeholder_count:
        reason = "placeholder policy documents are not production evidence"
    return PolicyCorpusStatus(
        directory=str(directory),
        document_count=len(paths),
        placeholder_count=placeholder_count,
        corpus_hash=digest.hexdigest(),
        ready=bool(paths) and placeholder_count == 0,
        reason=reason,
    )


def require_production_policy_corpus(directory: Path) -> PolicyCorpusStatus:
    status = inspect_policy_corpus(directory)
    if not status.ready:
        raise PolicyCorpusError(status.reason or "policy corpus is not ready")
    return status
