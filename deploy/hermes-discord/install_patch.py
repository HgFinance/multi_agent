"""Build-time source hook for the official Hermes gateway image."""

from __future__ import annotations

from pathlib import Path


MARKER = "# HgFinance Discord durable idempotency hook"


def find_adapter() -> Path:
    candidates = (
        Path("/opt/hermes/plugins/platforms/discord/adapter.py"),
        Path("/app/plugins/platforms/discord/adapter.py"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise SystemExit("Hermes Discord adapter.py was not found; refusing an unpatched gateway image")


def main() -> None:
    path = find_adapter()
    source = path.read_text(encoding="utf-8")
    required = ("class DiscordAdapter", "def _discord_message_admission", "async def send(")
    missing = [marker for marker in required if marker not in source]
    if missing:
        raise SystemExit(f"Hermes Discord adapter contract changed: missing {missing}")
    if MARKER in source:
        return
    source += (
        "\n\n"
        f"{MARKER}\n"
        "from gateway_patch import install as _hgfinance_install_discord_idempotency\n"
        "_hgfinance_install_discord_idempotency(DiscordAdapter)\n"
    )
    path.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()
