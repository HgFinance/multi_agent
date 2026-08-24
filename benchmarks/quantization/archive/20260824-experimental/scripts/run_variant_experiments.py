#!/usr/bin/env python3
"""Plan and validate variant runs without mutating frozen benchmark inputs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from .variant_manifest import admit_manifest, load_manifest
except ImportError:
    from variant_manifest import admit_manifest, load_manifest


def prepare(manifest_path: Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    decision, errors = admit_manifest(manifest)
    return {"variant": manifest.get("variant"), "decision": decision, "errors": errors, "manifest": manifest}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"variant": result["variant"], "decision": result["decision"], "errors": result["errors"]}, ensure_ascii=False))
    return 0 if result["decision"] == "ADMIT" else 2


if __name__ == "__main__":
    raise SystemExit(main())
