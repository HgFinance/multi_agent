"""QA terminal projection의 package/import 및 image wiring 계약."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
QA_ROOT = ROOT / "departments" / "06-ai-qa-audit"
REPOSITORY = QA_ROOT / "audit" / "repository.py"
SUPERVISOR_DOCKERFILE = ROOT / "Dockerfile.ceo-supervisor"
COMPOSE = ROOT / "docker-compose.yml"


def test_audit_repository_imports_from_orchestration_style_context() -> None:
    """The supervisor's ``PYTHONPATH=/opt/hgfinance`` plus QA root import works."""

    env = os.environ.copy()
    env["PYTHONPATH"] = str(QA_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", "import audit.repository as module; print(module.__name__)"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "audit.repository"


def test_postgres_driver_is_lazy_and_matches_repository_contract() -> None:
    source = REPOSITORY.read_text(encoding="utf-8")

    assert "def _load_postgres_driver" in source
    assert "from psycopg2.extras import" in source
    assert "from psycopg2.pool import" in source
    assert "psycopg2.pool" in source
    assert "psycopg2.extras" in source
    assert "psycopg2" not in subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import audit.repository; print('psycopg2' in sys.modules)",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(QA_ROOT)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_ceo_supervisor_image_contains_postgres_driver_at_build_time() -> None:
    dockerfile = SUPERVISOR_DOCKERFILE.read_text(encoding="utf-8")
    compose = COMPOSE.read_text(encoding="utf-8")
    service = compose.split("  ceo-kanban-supervisor:\n", 1)[1].split(
        "\n  # ", 1
    )[0]

    assert "FROM nousresearch/hermes-agent:latest" in dockerfile
    assert "psycopg2-binary==2.9.12" in dockerfile
    assert "dockerfile: Dockerfile.ceo-supervisor" in service
    assert "image: hedgefund-ceo-supervisor:latest" in service
    assert "pip install" not in service
