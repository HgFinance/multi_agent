"""Report Risk/QA credential readiness without printing secret values."""

from __future__ import annotations

import os


def _present(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def _row(name: str, *, required: bool, reason: str) -> dict[str, object]:
    return {
        "name": name,
        "required": required,
        "configured": _present(name),
        "reason": reason,
    }


def _row_with_fallback(
    name: str,
    *,
    fallback: str | None,
    required: bool,
    reason: str,
) -> dict[str, object]:
    row = _row(name, required=required, reason=reason)
    row["configured"] = bool(
        os.environ.get(name, "").strip()
        or (fallback and os.environ.get(fallback, "").strip())
    )
    return row


def collect_status() -> dict[str, object]:
    ls_env = os.environ.get("LS_ENV", "LIVE").strip().upper()
    suffix = "_PAPER" if ls_env == "PAPER" else ""
    ingest_mode = os.environ.get("QA_INGEST_MODE", "disabled").strip().lower()
    legacy_ingestion = (
        os.environ.get("QA_ENABLE_LEGACY_EVIDENCE_INGESTION", "")
        .strip()
        .lower()
        == "true"
        and ingest_mode == "legacy-manual"
    )
    required = [
        _row(f"LS_APP_KEY{suffix}", required=ls_env in {"PAPER", "LIVE"}, reason="LS OAuth"),
        _row(
            f"LS_APP_SECRET_KEY{suffix}",
            required=ls_env in {"PAPER", "LIVE"},
            reason="LS OAuth secret",
        ),
        _row_with_fallback(
            f"LS_REST_BASE_URL{suffix}",
            fallback="LS_REST_BASE_URL",
            required=ls_env in {"PAPER", "LIVE"},
            reason="LS REST endpoint",
        ),
        _row(
            "DATABASE_URL",
            required=legacy_ingestion,
            reason="legacy manual evidence migration only",
        ),
        _row(
            "QA_POLICY_SOURCE_ID",
            required=legacy_ingestion,
            reason="legacy manual evidence migration only",
        ),
        _row(
            "OPENAI_API_KEY",
            required=legacy_ingestion,
            reason="legacy manual evidence migration only",
        ),
    ]
    return {
        "ls_environment": ls_env,
        "qa_ingest_mode": ingest_mode,
        "non_market_evidence_mode": "request_time_mcp",
        "legacy_evidence_ingestion_enabled": legacy_ingestion,
        "credentials": required,
        "all_required_configured": all(
            not item["required"] or item["configured"] for item in required
        ),
        "secret_values_exposed": False,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(collect_status(), ensure_ascii=False, indent=2, sort_keys=True))
