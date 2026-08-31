"""Contracts for the single-source GPU resource observability wiring."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY = ROOT / "deploy" / "observability"


def test_prometheus_scrapes_existing_gpu_and_vllm_native_endpoints() -> None:
    config = yaml.safe_load((OBSERVABILITY / "prometheus.yml").read_text())

    assert config["rule_files"] == ["/etc/prometheus/rules/gpu-resource-savings.yml"]
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    assert jobs["gpu"]["static_configs"][0]["targets"] == ["dcgm-exporter:9400"]
    assert jobs["vllm"]["static_configs"][0]["targets"] == ["vllm:8000"]


def test_gpu_rules_are_unique_and_keep_efficiency_honest() -> None:
    rules = yaml.safe_load(
        (OBSERVABILITY / "gpu-resource-savings.yml").read_text()
    )["groups"][0]["rules"]
    names = [rule["record"] for rule in rules]

    assert len(names) == len(set(names))
    assert {
        "hgfinance:vllm:tokens_per_busy_gpu_second",
        "hgfinance:vllm:energy_mj_per_1k_tokens",
        "hgfinance:vllm:energy_savings_vs_7d_ratio",
        "hgfinance:vllm:e2e_latency_p95_seconds",
        "hgfinance:vllm:preemption_rate_per_second",
    }.issubset(names)

    expressions = {rule["record"]: rule["expr"] for rule in rules}
    # Workload-normalized efficiency must not turn an idle GPU into a saving.
    assert "token_rate_per_second > 0" in expressions[
        "hgfinance:vllm:energy_mj_per_1k_tokens"
    ]
    # Historical comparison is explicitly unavailable until a matching week
    # exists; this prevents a fabricated zero-percent baseline.
    assert "offset 7d" in expressions["hgfinance:vllm:energy_savings_vs_7d_ratio"]


def test_grafana_dashboard_is_valid_and_provisioned() -> None:
    dashboard = json.loads(
        (OBSERVABILITY / "dashboards" / "gpu-resource-savings.json").read_text()
    )
    provider = yaml.safe_load(
        (OBSERVABILITY / "grafana-dashboards.yml").read_text()
    )

    assert dashboard["uid"] == "hgfinance-gpu-savings"
    datasource = yaml.safe_load(
        (OBSERVABILITY / "grafana-datasource.yml").read_text()
    )
    assert datasource["datasources"][0]["uid"] == "prometheus"
    assert any(
        target["expr"] == "hgfinance:vllm:energy_savings_vs_7d_ratio * 100"
        for panel in dashboard["panels"]
        for target in panel["targets"]
    )
    assert provider["providers"][0]["options"]["path"] == "/var/lib/grafana/dashboards"


def test_observability_compose_mounts_the_same_rule_file_once() -> None:
    compose = yaml.safe_load(
        (ROOT / "docker-compose.observability.yml").read_text()
    )
    prometheus_mounts = compose["services"]["prometheus"]["volumes"]
    grafana_mounts = compose["services"]["grafana"]["volumes"]

    rule_mounts = [mount for mount in prometheus_mounts if "gpu-resource-savings.yml" in mount]
    assert rule_mounts == [
        "./deploy/observability/gpu-resource-savings.yml:/etc/prometheus/rules/gpu-resource-savings.yml:ro"
    ]
    assert "./deploy/observability/dashboards:/var/lib/grafana/dashboards:ro" in grafana_mounts
