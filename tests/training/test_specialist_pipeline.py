from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.qlora.train_specialist_qlora import adapter_name
from training.specialist.config import QLoRAConfig
from training.specialist.contamination import check_contamination, require_clean
from training.specialist.metadata import build_training_metadata
from training.specialist.mixing import load_pool, mix_pools
from training.specialist.schema import DatasetValidationError, load_jsonl, validate_record


def _record(identifier: str, text: str, *, category: str = "reasoning") -> dict:
    return {
        "id": identifier,
        "messages": [
            {"role": "system", "content": "Use evidence and fail closed."},
            {"role": "user", "content": text},
            {"role": "assistant", "content": f"Answer for {identifier}."},
        ],
        "category": category,
        "behavior_themes": ["evidence-first"],
    }


def _write(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


class SpecialistPipelineTests(unittest.TestCase):
    def test_schema_accepts_messages_and_legacy_shape(self) -> None:
        messages = validate_record(
            _record("m1", "A valid question"),
            source_dataset="x",
            source_file="x.jsonl",
            source_row=1,
        )
        legacy = validate_record(
            {
                "id": "legacy",
                "instruct": "Use evidence.",
                "input": "What is known?",
                "output": "Only the supplied evidence is known.",
                "category": "general",
            },
            source_dataset="x",
            source_file="x.jsonl",
            source_row=2,
        )
        self.assertEqual(messages.messages[-1]["role"], "assistant")
        self.assertEqual(legacy.messages[0]["role"], "system")

    def test_invalid_shape_fails_closed(self) -> None:
        with self.assertRaises(DatasetValidationError):
            validate_record(
                {"id": "bad", "category": "x", "messages": [{"role": "user", "content": "x"}]},
                source_dataset="x",
                source_file="x.jsonl",
                source_row=1,
            )

    def test_ratio_and_seed_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pools = {}
            for name in ("common", "general", "department"):
                train = root / f"{name}_train.jsonl"
                validation = root / f"{name}_validation.jsonl"
                _write(train, [_record(f"{name}-{i}", f"{name} question {i}") for i in range(8)])
                _write(validation, [_record(f"{name}-v-{i}", f"{name} validation {i}") for i in range(2)])
                pools[name] = load_pool(name, train, validation)
            kwargs = dict(
                pools=pools,
                ratios={"common": 0.2, "general": 0.3, "department": 0.5},
                target_size=10,
                seed=123,
                benchmark_root=root / "empty-benchmarks",
            )
            (root / "empty-benchmarks").mkdir()
            first, first_meta = mix_pools(**kwargs)
            second, second_meta = mix_pools(**kwargs)
            self.assertEqual(first, second)
            self.assertEqual(first_meta["mixture_sha256"], second_meta["mixture_sha256"])
            self.assertEqual(first_meta["selected_by_pool"], {"common": 2, "general": 3, "department": 5})
            self.assertTrue(all("source_file" in record and "record_sha256" in record for record in first))

    def test_train_validation_overlap_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            same = _record("same", "shared text")
            common_train, common_val = root / "ct.jsonl", root / "cv.jsonl"
            dept_train, dept_val = root / "dt.jsonl", root / "dv.jsonl"
            _write(common_train, [same])
            _write(common_val, [_record("cv", "different validation")])
            _write(dept_train, [_record("dt", "different department")])
            _write(dept_val, [same])
            pools = {
                "common": load_pool("common", common_train, common_val),
                "department": load_pool("department", dept_train, dept_val),
            }
            with self.assertRaises(DatasetValidationError):
                mix_pools(pools, ratios={"common": 0.5, "department": 0.5}, target_size=2, seed=1)

    def test_cross_pool_duplicate_is_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common_train, common_val = root / "ct.jsonl", root / "cv.jsonl"
            dept_train, dept_val = root / "dt.jsonl", root / "dv.jsonl"
            duplicate_a = _record("a", "Same question, exactly.")
            duplicate_b = _record("b", "same question exactly")
            duplicate_b["messages"][-1]["content"] = duplicate_a["messages"][-1]["content"]
            _write(common_train, [duplicate_a, _record("c", "common unique")])
            _write(common_val, [_record("cv", "common validation")])
            _write(dept_train, [duplicate_b, _record("d", "department unique")])
            _write(dept_val, [_record("dv", "department validation")])
            pools = {
                "common": load_pool("common", common_train, common_val),
                "department": load_pool("department", dept_train, dept_val),
            }
            records, metadata = mix_pools(
                pools,
                ratios={"common": 0.5, "department": 0.5},
                target_size=2,
                seed=1,
            )
            self.assertEqual(len({record["normalized_record_sha256"] for record in records}), 2)
            self.assertGreaterEqual(metadata["deduplication"]["global"]["normalized_duplicates_removed"], 1)

    def test_duplicate_records_within_training_pool_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common_train, common_val = root / "ct.jsonl", root / "cv.jsonl"
            dept_train, dept_val = root / "dt.jsonl", root / "dv.jsonl"
            duplicate = _record("duplicate", "duplicate training text")
            _write(common_train, [duplicate, duplicate])
            _write(common_val, [_record("cv", "common validation")])
            _write(dept_train, [_record("dt", "department training")])
            _write(dept_val, [_record("dv", "department validation")])
            records, metadata = mix_pools(
                {
                    "common": load_pool("common", common_train, common_val),
                    "department": load_pool("department", dept_train, dept_val),
                },
                ratios={"common": 0.5, "department": 0.5},
                target_size=2,
                seed=1,
            )
            self.assertEqual(len(records), 2)
            self.assertEqual(metadata["deduplication"]["per_pool"]["common"]["exact_duplicates_removed"], 1)

    def test_benchmark_contamination_blocks_exact_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            benchmark_root = root / "benchmarks"
            benchmark_root.mkdir()
            _write(benchmark_root / "external50_v1.jsonl", [{"id": "heldout", "question": "A held out question with enough unique text to trigger exact matching."}])
            candidate = validate_record(
                _record("candidate", "A held out question with enough unique text to trigger exact matching."),
                source_dataset="candidate",
                source_file="candidate.jsonl",
                source_row=1,
            )
            result = check_contamination([candidate], benchmark_root)
            self.assertEqual(result["status"], "BLOCKED")
            with self.assertRaises(DatasetValidationError):
                require_clean(result)

    def test_adapter_name_and_metadata(self) -> None:
        self.assertEqual(adapter_name("risk", "1"), "hgfinance-risk-v1")
        self.assertEqual(adapter_name("accounting_portfolio", "v2"), "hgfinance-accounting-portfolio-v2")
        with self.assertRaises(DatasetValidationError):
            adapter_name("Risk!", "v1")
        metadata = build_training_metadata(
            repo_root=Path.cwd(),
            department="risk",
            adapter_name="hgfinance-risk-v1",
            adapter_version="v1",
            common_dataset_sha256="common-hash",
            department_dataset_sha256="department-hash",
            mixture_metadata={"effective_ratios": {"common": 0.25}, "actual_train_size": 1, "seed": 66, "contamination": {"status": "PASS"}},
            qlora=QLoRAConfig(),
            optimizer_args={"learning_rate": 0.0002},
            training_args={"validation_size": 1},
        )
        self.assertEqual(metadata["common_dataset_sha256"], "common-hash")
        self.assertEqual(metadata["lora"]["lora_r"], 16)


if __name__ == "__main__":
    unittest.main()
