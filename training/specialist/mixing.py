"""Deterministic, provenance-preserving Common/department dataset mixing."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contamination import check_contamination, require_clean
from .schema import DatasetValidationError, ValidatedExample, file_sha256, load_jsonl


@dataclass(frozen=True)
class DatasetPool:
    name: str
    train_path: Path
    validation_path: Path
    train: tuple[ValidatedExample, ...]
    validation: tuple[ValidatedExample, ...]
    train_sha256: str
    validation_sha256: str

    @property
    def dataset_sha256(self) -> str:
        return hashlib.sha256(f"{self.train_sha256}:{self.validation_sha256}".encode()).hexdigest()


def _duplicate_stats(examples: Iterable[ValidatedExample]) -> dict[str, Any]:
    items = list(examples)
    exact: dict[str, str] = {}
    normalized: dict[str, str] = {}
    exact_duplicates: list[dict[str, str]] = []
    normalized_duplicates: list[dict[str, str]] = []
    for example in items:
        identifier = str(example.record["id"])
        if example.record_sha256 in exact:
            exact_duplicates.append({"id": identifier, "duplicate_of": exact[example.record_sha256]})
        else:
            exact[example.record_sha256] = identifier
        if example.normalized_sha256 in normalized:
            normalized_duplicates.append(
                {"id": identifier, "duplicate_of": normalized[example.normalized_sha256]}
            )
        else:
            normalized[example.normalized_sha256] = identifier
    return {
        "input_count": len(items),
        "exact_duplicate_count": len(exact_duplicates),
        "normalized_duplicate_count": len(normalized_duplicates),
        "exact_duplicates": exact_duplicates,
        "normalized_duplicates": normalized_duplicates,
    }


def _assert_unique(examples: tuple[ValidatedExample, ...], label: str) -> dict[str, Any]:
    stats = _duplicate_stats(examples)
    if stats["exact_duplicate_count"] or stats["normalized_duplicate_count"]:
        raise DatasetValidationError(
            f"{label} contains duplicates: exact={stats['exact_duplicate_count']} "
            f"normalized={stats['normalized_duplicate_count']}"
        )
    return stats


def _assert_split_isolation(pools: Iterable[DatasetPool]) -> None:
    exact: dict[str, tuple[str, str]] = {}
    normalized: dict[str, tuple[str, str]] = {}
    user_exact: dict[str, tuple[str, str]] = {}
    user_normalized: dict[str, tuple[str, str]] = {}
    for pool in pools:
        for split, examples in (("train", pool.train), ("validation", pool.validation)):
            location = f"{pool.name}:{split}"
            for example in examples:
                previous = exact.get(example.record_sha256)
                if previous and previous[0].rsplit(":", 1)[-1] != split:
                    raise DatasetValidationError(
                        f"exact train/validation overlap: {previous} and {location}:{example.record['id']}"
                    )
                exact[example.record_sha256] = (location, str(example.record["id"]))
                previous = normalized.get(example.normalized_sha256)
                if previous and previous[0].rsplit(":", 1)[-1] != split:
                    raise DatasetValidationError(
                        "normalized train/validation overlap: "
                        f"{previous} and {location}:{example.record['id']}"
                    )
                normalized[example.normalized_sha256] = (location, str(example.record["id"]))

                previous = user_exact.get(example.user_sha256)
                if previous and previous[0].rsplit(":", 1)[-1] != split:
                    raise DatasetValidationError(
                        "exact user/question train/validation overlap: "
                        f"{previous} and {location}:{example.record['id']}"
                    )
                user_exact[example.user_sha256] = (location, str(example.record["id"]))

                previous = user_normalized.get(example.normalized_user_sha256)
                if previous and previous[0].rsplit(":", 1)[-1] != split:
                    raise DatasetValidationError(
                        "normalized user/question train/validation overlap: "
                        f"{previous} and {location}:{example.record['id']}"
                    )
                user_normalized[example.normalized_user_sha256] = (
                    location,
                    str(example.record["id"]),
                )


def load_pool(name: str, train_path: Path, validation_path: Path) -> DatasetPool:
    train = tuple(load_jsonl(train_path, source_dataset=name))
    validation = tuple(load_jsonl(validation_path, source_dataset=name))
    return DatasetPool(
        name=name,
        train_path=train_path,
        validation_path=validation_path,
        train=train,
        validation=validation,
        train_sha256=file_sha256(train_path),
        validation_sha256=file_sha256(validation_path),
    )


def _deduplicate(examples: Iterable[ValidatedExample]) -> tuple[list[ValidatedExample], dict[str, int]]:
    kept: list[ValidatedExample] = []
    exact: set[str] = set()
    normalized: set[str] = set()
    exact_count = 0
    normalized_count = 0
    for example in examples:
        if example.record_sha256 in exact:
            exact_count += 1
            continue
        if example.normalized_sha256 in normalized:
            normalized_count += 1
            continue
        exact.add(example.record_sha256)
        normalized.add(example.normalized_sha256)
        kept.append(example)
    return kept, {"exact_duplicates_removed": exact_count, "normalized_duplicates_removed": normalized_count}


def _ratio_counts(target_size: int, ratios: Mapping[str, float]) -> dict[str, int]:
    if target_size <= 0:
        raise DatasetValidationError("target_size must be positive")
    if any(float(value) < 0 for value in ratios.values()):
        raise DatasetValidationError("ratios cannot be negative")
    active = {name: float(value) for name, value in ratios.items() if float(value) > 0}
    if not active:
        raise DatasetValidationError("at least one ratio must be positive")
    total = sum(active.values())
    raw = {name: target_size * value / total for name, value in active.items()}
    counts = {name: int(value) for name, value in raw.items()}
    for name in sorted(active, key=lambda key: (-(raw[key] - counts[key]), key))[: target_size - sum(counts.values())]:
        counts[name] += 1
    return counts


def _sample(examples: list[ValidatedExample], count: int, *, seed: int, pool_name: str, allow_replacement: bool) -> list[ValidatedExample]:
    if count == 0:
        return []
    if not examples:
        raise DatasetValidationError(f"pool {pool_name} has no deduplicated train examples")
    if count > len(examples) and not allow_replacement:
        raise DatasetValidationError(
            f"pool {pool_name} has {len(examples)} examples but ratio requests {count}; "
            "pass allow_replacement explicitly"
        )
    rng = random.Random(f"{seed}:{pool_name}")
    if count <= len(examples):
        indexes = rng.sample(range(len(examples)), count)
    else:
        indexes = [rng.randrange(len(examples)) for _ in range(count)]
    return [examples[index] for index in indexes]


def _with_provenance(example: ValidatedExample, pool_name: str) -> dict[str, Any]:
    output = dict(example.record)
    output["messages"] = [dict(message) for message in example.messages]
    for legacy in ("instruct", "input", "output"):
        output.pop(legacy, None)
    output.update(
        {
            "source_dataset": pool_name,
            "source_file": example.source_file,
            "source_row": example.source_row,
            "record_sha256": example.record_sha256,
            "normalized_record_sha256": example.normalized_sha256,
            "user_sha256": example.user_sha256,
            "normalized_user_sha256": example.normalized_user_sha256,
        }
    )
    if "sample_sha256" in example.record:
        output["source_sample_sha256"] = example.record["sample_sha256"]
    return output


def preserve_pool_split(pool: DatasetPool, split: str) -> list[dict[str, Any]]:
    """Validate and preserve one split without sampling or replacement."""

    if split not in {"train", "validation"}:
        raise DatasetValidationError(f"unsupported pool split: {split}")
    _assert_split_isolation((pool,))
    examples = pool.train if split == "train" else pool.validation
    _assert_unique(examples, f"{pool.name} {split}")
    return [_with_provenance(example, pool.name) for example in examples]


def mix_pools(
    pools: Mapping[str, DatasetPool],
    *,
    ratios: Mapping[str, float],
    target_size: int,
    seed: int = 66,
    allow_replacement: bool = False,
    benchmark_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not pools:
        raise DatasetValidationError("at least one dataset pool is required")
    if benchmark_root is None:
        raise DatasetValidationError("benchmark_root is required for training preparation")
    missing = [name for name, ratio in ratios.items() if float(ratio) > 0 and name not in pools]
    if missing:
        raise DatasetValidationError(f"active ratio has no pool: {missing}")
    _assert_split_isolation(pools.values())

    train_by_pool: dict[str, list[ValidatedExample]] = {}
    dedup_stats: dict[str, dict[str, Any]] = {}
    all_train: list[ValidatedExample] = []
    for name, pool in pools.items():
        deduped, stats = _deduplicate(pool.train)
        train_by_pool[name] = deduped
        dedup_stats[name] = stats
        all_train.extend(deduped)
    globally_deduped, global_stats = _deduplicate(all_train)
    allowed = {id(example) for example in globally_deduped}
    for name in train_by_pool:
        train_by_pool[name] = [example for example in train_by_pool[name] if id(example) in allowed]

    active_ratios = {name: float(ratio) for name, ratio in ratios.items() if float(ratio) > 0}
    counts = _ratio_counts(target_size, active_ratios)
    selected: list[dict[str, Any]] = []
    selected_by_pool: dict[str, int] = {}
    for name, count in counts.items():
        chosen = _sample(train_by_pool[name], count, seed=seed, pool_name=name, allow_replacement=allow_replacement)
        selected.extend(_with_provenance(example, name) for example in chosen)
        selected_by_pool[name] = count

    # The selected records retain all provenance; validate them through the same contract.
    selected_examples = [
        ValidatedExample(
            record,
            record["messages"],
            str(record["source_dataset"]),
            str(record["source_file"]),
            int(record["source_row"]),
        )
        for record in selected
    ]
    contamination = check_contamination(selected_examples, benchmark_root)
    require_clean(contamination)

    metadata: dict[str, Any] = {
        "seed": seed,
        "target_train_size": target_size,
        "actual_train_size": len(selected),
        "ratios_requested": dict(ratios),
        "effective_ratios": {name: value / sum(active_ratios.values()) for name, value in active_ratios.items()},
        "selected_by_pool": selected_by_pool,
        "allow_replacement": allow_replacement,
        "category_counts": {
            category: sum(1 for record in selected if record.get("category") == category)
            for category in sorted({str(record.get("category")) for record in selected})
        },
        "deduplication": {"per_pool": dedup_stats, "global": global_stats},
        "pool_sha256": {
            name: {"train": pool.train_sha256, "validation": pool.validation_sha256, "dataset": pool.dataset_sha256}
            for name, pool in pools.items()
        },
        "contamination": contamination,
    }
    metadata["mixture_sha256"] = hashlib.sha256(
        "\n".join(record["record_sha256"] for record in selected).encode()
    ).hexdigest()
    return selected, metadata


def write_jsonl(records: Iterable[Mapping[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
