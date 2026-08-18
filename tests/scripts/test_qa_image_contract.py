from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_qa_image_contains_agentic_rag_runtime_dependency() -> None:
    dockerfile = (
        ROOT / "departments" / "06-ai-qa-audit" / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "COPY skills/agentic-rag ./skills/agentic-rag" in dockerfile
    assert "COPY departments/04-quant-backtest/pipeline " \
        "./departments/04-quant-backtest/pipeline" in dockerfile
    assert "COPY departments/01-research/contracts " \
        "./departments/01-research/contracts" in dockerfile
    assert "numpy==2.3.2" in dockerfile


def test_compose_selects_scoped_database_runtime_roles() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert compose.count(
        "DATABASE_RUNTIME_ROLE: ${AUDIT_API_DATABASE_RUNTIME_ROLE:-svc_audit_api}"
    ) == 1
    assert compose.count(
        "DATABASE_RUNTIME_ROLE: ${QA_WORKER_DATABASE_RUNTIME_ROLE:-svc_qa_worker}"
    ) == 1
    assert compose.count(
        "DATABASE_RUNTIME_ROLE: "
        "${QA_REPRODUCER_DATABASE_RUNTIME_ROLE:-svc_qa_reproducer}"
    ) == 1
    assert "qa-reproduction-worker:" in compose
    assert '["python", "qa_events/reproduction_worker.py"]' in compose
    assert (
        'test: ["CMD", "python", "qa_events/worker.py", "--healthcheck"]'
        in compose
    )
    assert "QA_REPRODUCTION_TIMESCALE_DATABASE_URL:" in compose
    assert "${QA_DATABASE_RUNTIME_ROLE:-service_role}" not in compose
    assert compose.count(
        "DATABASE_URL: ${RISK_QA_DATABASE_URL:-${DATABASE_URL:-}}"
    ) == 3
    assert compose.count(
        "DATABASE_RUNTIME_ROLE: ${QUANT_DATABASE_RUNTIME_ROLE:-svc_quant}"
    ) == 2
    assert compose.count(
        "DATABASE_SESSION_URL: ${RISK_QA_DATABASE_SESSION_URL:-}"
    ) == 3
    assert compose.count(
        "DATABASE_SESSION_URL: ${QUANT_DATABASE_SESSION_URL:-}"
    ) == 2
