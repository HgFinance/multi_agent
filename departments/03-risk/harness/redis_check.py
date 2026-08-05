"""Non-secret Redis health check for Risk/QA runtime wiring."""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlparse


def check_redis_urls(environ: Mapping[str, str] | None = None) -> dict:
    env = os.environ if environ is None else environ
    urls = {
        "risk_trading_state": env.get("REDIS_URL", "").strip(),
        "risk_qa_events": (
            env.get("RISK_QA_EVENT_REDIS_URL") or env.get("REDIS_URL", "")
        ).strip(),
    }
    checks = [_check_one(name, url) for name, url in urls.items()]
    return {
        "ready": all(item["ready"] for item in checks),
        "checks": checks,
        "secret_values_exposed": False,
    }


def _check_one(name: str, url: str) -> dict:
    result = {"name": name, "configured": bool(url), "ready": False}
    if not url:
        result["error_class"] = "REDIS_URL_MISSING"
        return result
    parsed = urlparse(url)
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
        result["error_class"] = "REDIS_URL_INVALID"
        return result
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=8, socket_timeout=8)
        result["ready"] = bool(client.ping())
        client.close()
    except Exception as exc:  # noqa: BLE001 - health boundary reports class only, never credentials
        result["error_class"] = type(exc).__name__
    return result


if __name__ == "__main__":
    import json

    print(json.dumps(check_redis_urls(), ensure_ascii=False, sort_keys=True))
