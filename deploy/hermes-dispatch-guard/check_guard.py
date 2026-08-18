#!/usr/bin/env python3
"""Fail startup unless the installed Hermes spawn hook is credential-scoped."""

from __future__ import annotations

import os
import sys


def main() -> int:
    try:
        import hermes_cli.kanban_db as kb
    except Exception:
        print("dispatcher credential-scope preflight: Hermes import failed",
              file=sys.stderr)
        return 78

    spawn = getattr(kb, "_default_spawn", None)
    active = bool(getattr(spawn, "_hgfinance_secret_scope_active", False))
    status = os.environ.get("HGFINANCE_DISPATCH_SECRET_SCOPE_STATUS", "")
    if not active or status != "ACTIVE_V1":
        print("dispatcher credential-scope preflight: inactive (fail closed)",
              file=sys.stderr)
        return 78
    print("dispatcher credential-scope preflight: ACTIVE_V1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
