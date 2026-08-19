"""Reproducibility metadata helpers."""

from __future__ import annotations

import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import QLoRAConfig


def git_commit(cwd: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"


def runtime_metadata() -> dict[str, Any]:
    result: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    try:
        import torch

        result.update({"torch": torch.__version__, "cuda_available": bool(torch.cuda.is_available())})
        if torch.cuda.is_available():
            result["cuda_device"] = torch.cuda.get_device_name(0)
    except ImportError:
        result.update({"torch": None, "cuda_available": False})
    return result


def write_metadata(path: Path, metadata: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(metadata), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_training_metadata(
    *,
    repo_root: Path,
    department: str | None,
    adapter_name: str,
    adapter_version: str,
    common_dataset_sha256: str | None,
    department_dataset_sha256: str | None,
    mixture_metadata: Mapping[str, Any],
    qlora: QLoRAConfig,
    optimizer_args: Mapping[str, Any],
    training_args: Mapping[str, Any],
    training_mode: str = "department",
    common_train_sha256: str | None = None,
    common_validation_sha256: str | None = None,
    resolved_base_revision: str | None = None,
    final_metrics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "training_mode": training_mode,
        "adapter_name": adapter_name,
        "adapter_version": adapter_version,
        "base_model": qlora.base_model,
        "base_revision": qlora.base_revision,
        "requested_base_revision": qlora.base_revision,
        "resolved_base_revision": resolved_base_revision,
        "git_commit": git_commit(repo_root),
        "common_dataset_sha256": common_dataset_sha256,
        "mixture_ratio": mixture_metadata.get("effective_ratios"),
        "train_size": mixture_metadata.get("actual_train_size"),
        "validation_size": training_args.get("validation_size"),
        "dedup_statistics": mixture_metadata.get("deduplication"),
        "contamination_check": mixture_metadata.get("contamination", {"status": "NOT_RUN"}),
        "seed": mixture_metadata.get("seed"),
        "lora": qlora.as_dict(),
        "optimizer_args": dict(optimizer_args),
        "training_args": dict(training_args),
        "runtime": runtime_metadata(),
        "final_metrics": dict(final_metrics or {}),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if department is not None:
        metadata["department"] = department
    if common_train_sha256 is not None:
        metadata["common_train_sha256"] = common_train_sha256
    if common_validation_sha256 is not None:
        metadata["common_validation_sha256"] = common_validation_sha256
    if department_dataset_sha256 is not None:
        metadata["department_dataset_sha256"] = department_dataset_sha256
    return metadata
