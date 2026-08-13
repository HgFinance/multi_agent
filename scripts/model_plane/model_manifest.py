#!/usr/bin/env python3
"""모델 아티팩트 manifest - 파일별 sha256 과 전체 digest 를 기록·검증한다.

소유: 재일 (Model Plane)
근거: FINAL_RUNTIME_ARCHITECTURE.md §8.3-8.4 "S3 = Source of Truth,
      EBS = Runtime Cache. S3 → checksum → EBS → vLLM load".
      checksum 단계가 없으면 EBS 가 깨졌는지 S3 와 다른지 알 길이 없다.

▶ artifact_digest
  파일별 sha256 을 (상대경로, digest) 로 정렬해 이어붙인 문자열의 sha256.
  파일 하나가 바뀌거나 빠지면 digest 가 바뀐다. Worker Registry 의
  model_digest / adapter_digest 필드(§14)에 이 값을 쓴다.

쓰기: python3 model_manifest.py --model-dir /opt/hgfinance/models/X --write
검증: python3 model_manifest.py --model-dir /opt/hgfinance/models/X --verify
자체 점검: python3 model_manifest.py   (네트워크 없음, 임시 디렉터리)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

MODULE_VERSION = "model-manifest-v1"
MANIFEST_NAME = "manifest.json"

# manifest 자신과 HF 캐시 부산물은 digest 대상이 아니다
_EXCLUDE = {MANIFEST_NAME, ".gitattributes"}
_EXCLUDE_DIRS = {".cache", "hf-cache", "__pycache__"}


def _iter_files(model_dir: Path):
    for p in sorted(model_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(model_dir)
        if rel.name in _EXCLUDE or any(part in _EXCLUDE_DIRS for part in rel.parts):
            continue
        yield rel, p


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(model_dir: Path) -> dict:
    files: dict[str, dict] = {}
    total = 0
    for rel, p in _iter_files(model_dir):
        digest = _sha256(p)
        size = p.stat().st_size
        files[rel.as_posix()] = {"sha256": digest, "bytes": size}
        total += size
    if not files:
        raise SystemExit(f"모델 파일이 없다: {model_dir} - 받다 만 디렉터리인가?")
    joined = "\n".join(f"{name}:{meta['sha256']}"
                       for name, meta in sorted(files.items()))
    return {
        "manifest_version": MODULE_VERSION,
        "model_dir_name": model_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(files),
        "total_bytes": total,
        "artifact_digest": "sha256:" + hashlib.sha256(joined.encode()).hexdigest(),
        "files": files,
    }


def write_manifest(model_dir: Path) -> dict:
    manifest = build_manifest(model_dir)
    (model_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def verify_manifest(model_dir: Path) -> tuple[bool, list[str]]:
    """저장된 manifest 와 현재 파일을 대조한다. 불일치는 전부 나열한다."""
    path = model_dir / MANIFEST_NAME
    if not path.exists():
        return False, [f"manifest 가 없다: {path}"]
    stored = json.loads(path.read_text(encoding="utf-8"))
    current = build_manifest(model_dir)
    problems: list[str] = []
    stored_files = stored.get("files", {})
    for name, meta in current["files"].items():
        if name not in stored_files:
            problems.append(f"manifest 에 없는 파일: {name}")
        elif stored_files[name]["sha256"] != meta["sha256"]:
            problems.append(f"digest 불일치: {name}")
    for name in stored_files:
        if name not in current["files"]:
            problems.append(f"파일이 사라졌다: {name}")
    if stored.get("artifact_digest") != current["artifact_digest"] and not problems:
        problems.append("artifact_digest 불일치 (파일 목록은 같은데 내용이 다르다)")
    return not problems, problems


# ---------------------------------------------------------------------------
# 자체 점검 - 임시 디렉터리, 네트워크 없음
# ---------------------------------------------------------------------------

def _self_check():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "model.safetensors").write_bytes(b"weights-1")
        (d / "config.json").write_text('{"a":1}', encoding="utf-8")
        (d / "hf-cache").mkdir()
        (d / "hf-cache" / "junk").write_bytes(b"x")  # 캐시는 digest 대상이 아니다

        m = write_manifest(d)
        assert m["file_count"] == 2 and m["artifact_digest"].startswith("sha256:")
        ok, problems = verify_manifest(d)
        assert ok, problems

        # 내용이 바뀌면 잡는다
        (d / "model.safetensors").write_bytes(b"weights-CORRUPT")
        ok, problems = verify_manifest(d)
        assert not ok and any("digest 불일치" in p for p in problems), problems

        # 파일이 사라져도 잡는다
        (d / "model.safetensors").unlink()
        ok, problems = verify_manifest(d)
        assert not ok and any("사라졌다" in p for p in problems), problems
    print("model-manifest 자체 점검 통과 (쓰기·검증·변조 감지)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model-dir", type=Path)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if not args.model_dir:
        _self_check()
        return 0
    model_dir = args.model_dir.resolve()
    if args.write:
        m = write_manifest(model_dir)
        print(f"manifest 기록: {model_dir / MANIFEST_NAME}")
        print(f"  files={m['file_count']} total={m['total_bytes']:,}B")
        print(f"  artifact_digest={m['artifact_digest']}")
        return 0
    if args.verify:
        ok, problems = verify_manifest(model_dir)
        if ok:
            print(f"검증 통과: {model_dir}")
            return 0
        print(f"검증 실패: {model_dir}")
        for p in problems:
            print(f"  - {p}")
        return 1
    ap.error("--write 또는 --verify 를 지정하라")
    return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
