from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_PATH = (
    ROOT / "departments/01-research/collectors/collector_scheduler.py"
)
SOURCE_REGISTRY_PATH = (
    ROOT / "departments/01-research/collectors/source_registry.py"
)
MCP_SERVER_PATH = ROOT / "departments/01-research/api/mcp_server.py"
EXTERNAL_SOURCES_PATH = ROOT / "departments/01-research/api/external_sources.py"
EXTERNAL_MACRO_PATH = ROOT / "departments/01-research/api/external_macro.py"
EXPECTED_MARKET_JOBS = {
    "market-archive": ("collectors/market_archive_exporter.py", "--export"),
    "universe-restrictions": (
        "collectors/universe_restriction_collector.py",
        "--collect",
    ),
    "data-steward": ("collectors/market_data_steward.py", "--audit"),
    "breadth": ("collectors/market_breadth_collector.py", "--collect"),
    "derivatives": ("collectors/derivatives_collector.py", "--collect"),
    "vkospi": ("collectors/volatility_index_collector.py", "--collect"),
    "style-index": ("collectors/style_index_collector.py", "--collect"),
    "calendar-observed": ("collectors/calendar_collector.py", "--collect"),
    "label-snapshot": ("collectors/label_snapshot_collector.py", "--collect"),
    "chart-daily-universe": (
        "collectors/chart_backfill_collector.py",
        "--daily",
        "--universe",
        "--recent-days",
        "3",
    ),
}

REQUEST_TIME_SOURCE_IDS = {
    "opendart",
    "bigkinds",
    "x_twitter",
    "truth_social",
    "naver_apihub",
    "tavily",
    "ecos",
    "fred",
    "kind",
    "consensus",
}
DISABLED_WITHOUT_MCP_ADAPTER = {"ls_news", "kosis", "gpr", "gdelt"}


def _load_scheduler():
    spec = importlib.util.spec_from_file_location(
        "market_data_scheduler_boundary", SCHEDULER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _service_block(compose: str, service_name: str) -> str:
    lines = compose.splitlines()
    start = lines.index(f"  {service_name}:") + 1
    block: list[str] = []
    for line in lines[start:]:
        if line.startswith("  ") and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


def test_always_on_scheduler_is_market_data_only() -> None:
    scheduler = _load_scheduler()
    jobs = {job.name: job for job in scheduler.JOBS}

    assert tuple(jobs) == tuple(EXPECTED_MARKET_JOBS)
    assert {name: job.argv for name, job in jobs.items()} == EXPECTED_MARKET_JOBS
    assert scheduler.MARKET_DATA_JOB_NAMES == frozenset(EXPECTED_MARKET_JOBS)
    assert not {
        job.argv[0] for job in jobs.values()
    } & scheduler.FORBIDDEN_ALWAYS_ON_COLLECTOR_PATHS
    assert scheduler.SCHEDULER_VERSION == "market-data-scheduler-v2"

    mcp_server = _load_module("market_health_mcp_boundary", MCP_SERVER_PATH)
    assert mcp_server.ACTIVE_MARKET_COLLECTOR_JOB_NAMES == frozenset(
        EXPECTED_MARKET_JOBS
    )


def test_batch_service_cannot_receive_non_market_collection_credentials() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    service = _service_block(compose, "batch-collectors")

    assert "TIMESCALE_DATABASE_URL:" in service
    assert "DATABASE_URL:" in service
    for forbidden in (
        "OPEN_DART_API_KEY:",
        "NAVER_CLIENT_ID:",
        "NAVER_CLIENT_SECRET:",
        "ECOS_API_KEY:",
        "FRED_API_KEY:",
        "KOSIS_API_KEY:",
        "TAVILY_API_KEY:",
        "BIGKINDS_API_KEY:",
        "X_API_KEY:",
        "ALPACA_API_KEY:",
        "ALPACA_SECRET_KEY:",
        "APCA_API_KEY_ID:",
        "APCA_API_SECRET_KEY:",
        "BLUESKY_HANDLE:",
        "BLUESKY_APP_PASSWORD:",
        "SUPABASE_URL:",
        "SUPABASE_SERVICE_ROLE_KEY:",
        "RESEARCH_API_URL:",
    ):
        assert forbidden not in service


def test_non_market_sources_are_request_time_only() -> None:
    registry = _load_module("market_boundary_source_registry", SOURCE_REGISTRY_PATH)
    specs = {spec.source_id: spec for spec in registry.SOURCES}

    assert REQUEST_TIME_SOURCE_IDS <= specs.keys()
    for source_id in REQUEST_TIME_SOURCE_IDS:
        spec = specs[source_id]
        assert spec.raw_bucket is None, source_id
        assert spec.normalized_target is None, source_id
        assert set(spec.allowed_uses) <= {registry.UseScope.SEARCH_ONLY}, source_id

    for source_id in DISABLED_WITHOUT_MCP_ADAPTER:
        assert registry.SourceRegistry(env={}).status(source_id) is (
            registry.SourceStatus.DISABLED
        )
        assert specs[source_id].allowed_uses == ()


def test_ls_registry_uses_one_ls_env_for_rest_and_websocket_credentials() -> None:
    registry = _load_module("single_ls_env_source_registry", SOURCE_REGISTRY_PATH)

    live = registry.SourceRegistry(
        env={
            "LS_ENV": "LIVE",
            "LS_APP_KEY": "live-key",
            "LS_APP_SECRET_KEY": "live-secret",
            "LS_REST_BASE_URL": "https://example.test",
        }
    )
    paper = registry.SourceRegistry(
        env={
            "LS_ENV": "PAPER",
            "LS_APP_KEY_PAPER": "paper-key",
            "LS_APP_SECRET_KEY_PAPER": "paper-secret",
            "LS_REST_BASE_URL": "https://example.test",
        }
    )
    mismatched = registry.SourceRegistry(
        env={
            "LS_ENV": "LIVE",
            "LS_APP_KEY_PAPER": "paper-key",
            "LS_APP_SECRET_KEY_PAPER": "paper-secret",
            "LS_REST_BASE_URL": "https://example.test",
        }
    )

    for source_id in ("ls_openapi_rest", "ls_openapi_ws"):
        assert live.status(source_id) is registry.SourceStatus.AVAILABLE
        assert paper.status(source_id) is registry.SourceStatus.AVAILABLE
        assert mismatched.status(source_id) is registry.SourceStatus.KEY_MISSING


def test_every_enabled_search_source_maps_to_a_real_mcp_callable() -> None:
    registry = _load_module("mcp_coverage_source_registry", SOURCE_REGISTRY_PATH)
    external = _load_module("external_sources", EXTERNAL_SOURCES_PATH)
    macro = _load_module("external_macro", EXTERNAL_MACRO_PATH)
    implementations = {
        name
        for module in (external, macro)
        for name in dir(module)
        if callable(getattr(module, name, None))
    }

    for spec in registry.SOURCES:
        if (
            spec.contracted
            and not spec.disabled_reason
            and registry.UseScope.SEARCH_ONLY in spec.allowed_uses
        ):
            assert spec.request_tool in implementations, spec.source_id


def test_request_time_citations_do_not_persist_external_responses() -> None:
    external = _load_module("external_sources_persistence_boundary", EXTERNAL_SOURCES_PATH)
    snapshot_source = inspect.getsource(external._snapshot)
    for forbidden in ("open(", ".write(", ".mkdir(", "CITE_DIR", "MCP_CITE_LOG_DIR"):
        assert forbidden not in snapshot_source
    assert external._snapshot("news_search", {"q": "x"}, {"items": [1]})

    # DART corpCode.xml도 외부 응답이다. 종목코드 해석을 빠르게 하려고 파일에
    # 캐시하면 뉴스·공시 본문만 비영속이라는 반쪽 경계가 된다.
    corp_index_source = inspect.getsource(external._load_corp_index)
    for forbidden in (
        "read_text(",
        "write_text(",
        "mkdir(",
        "_CORP_CACHE",
        "MCP_CORP_CACHE",
    ):
        assert forbidden not in corp_index_source


def test_non_market_collectors_have_no_compose_reactivation_path() -> None:
    for filename in ("docker-compose.yml", "docker-compose.override.yml"):
        path = ROOT / filename
        if not path.exists():
            continue
        compose = path.read_text(encoding="utf-8")
        for name in ("news-watcher", "ls-news"):
            assert f"  {name}:" not in compose


def test_retired_non_market_collector_entrypoints_are_removed() -> None:
    collectors = ROOT / "departments/01-research/collectors"
    retired = {
        "alpaca_news_collector.py",
        "bluesky_watch_collector.py",
        "capability_audit.py",
        "corporate_action_collector.py",
        "geopolitical_collector.py",
        "ls_news_collector.py",
        "macro_collector.py",
        "naver_news_collector.py",
        "news.py",
        "news_pipeline.py",
        "news_watch_service.py",
        "news_watch_tiers.py",
        "opendart_cashflow.py",
        "opendart_collector.py",
        "opendart_company_collector.py",
        "opendart_document_collector.py",
        "opendart_financial.py",
        "research_data_steward.py",
        "watchlist_builder.py",
    }
    assert not {path.name for path in collectors.iterdir()} & retired


def test_research_image_has_safe_market_only_default() -> None:
    dockerfile = (
        ROOT / "departments/01-research/Dockerfile"
    ).read_text(encoding="utf-8")
    assert 'CMD ["python", "collectors/collector_scheduler.py", "--check"]' in dockerfile
    assert "news_watch_service.py" not in dockerfile
    assert "COPY collectors ./collectors" not in dockerfile
    assert "COPY agents ./agents" not in dockerfile
    assert "COPY scripts.py ./scripts.py" not in dockerfile
    for forbidden in (
        "retention_enforcer.py",
        "replay_restore_drill.py",
        "packet_outcome_scorer.py",
    ):
        assert forbidden not in dockerfile

    mcp_dockerfile = (
        ROOT / "departments/01-research/Dockerfile.mcp"
    ).read_text(encoding="utf-8")
    assert "COPY departments/01-research/collectors ./collectors" not in mcp_dockerfile
    assert "collector_scheduler.py" not in mcp_dockerfile
    assert "COPY departments/01-research/agents" not in mcp_dockerfile
    assert "COPY departments/01-research/scripts.py" not in mcp_dockerfile

    dockerignore = (
        ROOT / "departments/01-research/.dockerignore"
    ).read_text(encoding="utf-8").splitlines()
    assert {"**/__pycache__", "**/*.pyc", ".env*"} <= set(dockerignore)

    factory_dockerfile = (ROOT / "Dockerfile.factory").read_text(encoding="utf-8")
    assert "COPY departments/01-research/collectors ./" not in factory_dockerfile
    assert (
        "COPY departments/01-research/collectors/source_registry.py "
        "./departments/01-research/collectors/source_registry.py"
    ) in factory_dockerfile

    root_dockerignore = (ROOT / ".dockerignore").read_text(
        encoding="utf-8"
    ).splitlines()
    assert {
        "market-archive/",
        "quant-data/",
        "artifacts/",
        "audit-artifacts/",
        "test-results/",
    } <= set(root_dockerignore)
    assert {
        "timescaledb/*",
        "!timescaledb/migrations/",
        "!timescaledb/migrations/**",
    } <= set(root_dockerignore)
    assert "timescaledb/" not in root_dockerignore


def test_liaison_surface_excludes_worker_runtime_capabilities() -> None:
    mcp_server = _load_module("research_mcp_liaison_boundary", MCP_SERVER_PATH)
    assert {"run_research_workers", "worker_model_health"} <= (
        mcp_server.LIAISON_EXCLUDED_TOOLS
    )


def test_retired_stock_packet_pipeline_is_not_an_mcp_capability() -> None:
    source = MCP_SERVER_PATH.read_text(encoding="utf-8")
    for retired_tool in (
        "run_research_packet",
        "get_packet_job",
        "list_recent_packets",
        "geopolitical_state",
    ):
        assert f'name="{retired_tool}"' not in source
    assert "from geopolitical_analyst import" not in source


def test_market_label_has_no_narrative_input() -> None:
    source = (
        ROOT / "departments/01-research/collectors/label_snapshot_collector.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "snapshot_geopolitical",
        "geopolitical_analyst",
        "RESEARCH_API_URL",
        "/macro/observations",
    ):
        assert forbidden not in source


def test_exchange_index_collectors_sync_only_their_market_source() -> None:
    collectors = ROOT / "departments/01-research/collectors"
    for filename in ("volatility_index_collector.py", "style_index_collector.py"):
        source = (collectors / filename).read_text(encoding="utf-8")
        assert "sync_data_sources(specs=[market_source])" in source
        assert "sync_data_sources()" not in source
