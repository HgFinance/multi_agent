from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.qlora.train_specialist_qlora import (
    AssistantOnlyCollator,
    adapter_name,
    audit_dataset_lengths,
    main as training_main,
    require_base_revision,
)
from training.specialist.config import DEFAULT_OPTIMIZER, QLoRAConfig
from training.specialist.contamination import check_contamination, require_clean
from training.specialist.metadata import build_training_metadata
from training.specialist.mixing import load_pool, mix_pools
from training.specialist.schema import (
    DEFAULT_HGFINANCE_SYSTEM_POLICY,
    DatasetValidationError,
    LEGACY_USER_SEPARATOR,
    validate_record,
)


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


def _benchmark_root(root: Path, questions: dict[str, str] | None = None) -> Path:
    questions = questions or {}
    root.mkdir()
    files = {
        "external50_v1.json": "external question",
        "internal50_v1.json": "internal v1 question",
        "internal50_v2_reasoning.json": "internal v2 question",
    }
    for filename, default_question in files.items():
        question = questions.get(filename, default_question)
        (root / filename).write_text(
            json.dumps({"cases": [{"id": filename, "question": question}]}),
            encoding="utf-8",
        )
    return root


class FakeTokenizer:
    chat_template = "fake"

    def __init__(self):
        self.vocab = {}

    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        rendered = " ".join(f"{message['role']}: {message['content']}" for message in messages)
        if add_generation_prompt:
            rendered += " assistant:"
        return rendered

    def __call__(self, text, *, add_special_tokens=False, truncation=False, max_length=None):
        tokens = text.split()
        if truncation and max_length is not None:
            tokens = tokens[:max_length]
        ids = []
        for token in tokens:
            if token not in self.vocab:
                self.vocab[token] = len(self.vocab) + 1
            ids.append(self.vocab[token])
        return {"input_ids": ids, "attention_mask": [1] * len(ids)}

    def pad(self, features, *, return_tensors):
        import torch

        width = max(len(feature["input_ids"]) for feature in features)
        return {
            "input_ids": torch.tensor(
                [feature["input_ids"] + [0] * (width - len(feature["input_ids"])) for feature in features]
            ),
            "attention_mask": torch.tensor(
                [feature["attention_mask"] + [0] * (width - len(feature["attention_mask"])) for feature in features]
            ),
        }


class PrefixMismatchTokenizer(FakeTokenizer):
    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        rendered = super().apply_chat_template(
            messages, tokenize=tokenize, add_generation_prompt=add_generation_prompt
        )
        return rendered + (" mismatch" if add_generation_prompt else "")


class FakeTensor:
    def __init__(self, data):
        self.data = data
        width = len(data[0]) if data and isinstance(data[0], list) else 0
        self.shape = (len(data), width) if width else (len(data),)

    def tolist(self):
        return self.data

    def __getitem__(self, index):
        value = self.data[index]
        return FakeTensor(value) if isinstance(value, list) else value


class FakeTorch:
    long = object()

    @staticmethod
    def tensor(data, dtype=None):
        return FakeTensor(data)


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
        self.assertEqual(legacy.messages[1]["role"], "user")
        self.assertIn("Use evidence.", legacy.messages[1]["content"])
        self.assertNotEqual(legacy.messages[0]["content"], legacy.messages[1]["content"])

    def test_legacy_empty_input_and_required_fields(self) -> None:
        empty = validate_record(
            {"id": "empty", "instruct": "Instruction", "input": "", "output": "Answer", "category": "x"},
            source_dataset="x",
            source_file="x.jsonl",
            source_row=1,
        )
        nonempty = validate_record(
            {"id": "nonempty", "instruct": "Instruction", "input": "Question", "output": "Answer", "category": "x"},
            source_dataset="x",
            source_file="x.jsonl",
            source_row=2,
        )
        self.assertEqual(empty.messages[0]["content"], DEFAULT_HGFINANCE_SYSTEM_POLICY)
        self.assertEqual(empty.messages[1]["content"], "Instruction")
        self.assertEqual(nonempty.messages[1]["content"], "Instruction" + LEGACY_USER_SEPARATOR + "Question")
        for field in ("instruct", "output"):
            record = {"id": "missing", "instruct": "Instruction", "output": "Answer", "category": "x"}
            record.pop(field)
            with self.assertRaises(DatasetValidationError):
                validate_record(record, source_dataset="x", source_file="x.jsonl", source_row=3)

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
            benchmark_root = _benchmark_root(root / "benchmarks")
            kwargs = dict(
                pools=pools,
                ratios={"common": 0.2, "general": 0.3, "department": 0.5},
                target_size=10,
                seed=123,
                benchmark_root=benchmark_root,
            )
            first, first_meta = mix_pools(**kwargs)
            second, second_meta = mix_pools(**kwargs)
            self.assertEqual(first, second)
            self.assertEqual(first_meta["mixture_sha256"], second_meta["mixture_sha256"])
            self.assertEqual(first_meta["selected_by_pool"], {"common": 2, "general": 3, "department": 5})
            self.assertTrue(all("source_file" in record and "record_sha256" in record for record in first))

    def test_common_only_dry_run_preserves_common_splits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "training_runs" / "hgfinance-common-v1"
            result = training_main(
                [
                    "--common-only",
                    "--adapter-version",
                    "v1",
                    "--common-dir",
                    "hgfinance_common_training_v1",
                    "--benchmark-root",
                    "benchmarks/quantization",
                    "--output-dir",
                    str(output_dir),
                    "--base-model",
                    "Qwen/Qwen2.5-14B-Instruct",
                    "--base-revision",
                    "pinned-test-revision",
                    "--dry-run",
                ]
            )
            self.assertEqual(result, 0)
            metadata = json.loads((output_dir / "training_metadata.json").read_text(encoding="utf-8"))
            train = (output_dir / "prepared" / "train.jsonl").read_text(encoding="utf-8").splitlines()
            validation = (output_dir / "prepared" / "validation.jsonl").read_text(encoding="utf-8").splitlines()
            source_train = (Path("hgfinance_common_training_v1") / "common_train.jsonl").read_text(encoding="utf-8").splitlines()
            source_validation = (Path("hgfinance_common_training_v1") / "common_validation.jsonl").read_text(encoding="utf-8").splitlines()
            prepared_train = [json.loads(line) for line in train]
            prepared_validation = [json.loads(line) for line in validation]
            original_train = [json.loads(line) for line in source_train]
            original_validation = [json.loads(line) for line in source_validation]
            self.assertEqual(metadata["training_mode"], "common_only")
            self.assertEqual(metadata["adapter_name"], "hgfinance-common-v1")
            self.assertEqual(metadata["train_size"], 2545)
            self.assertEqual(metadata["validation_size"], 223)
            self.assertEqual(len(metadata["common_train_sha256"]), 64)
            self.assertEqual(len(metadata["common_validation_sha256"]), 64)
            self.assertEqual(len(train), 2545)
            self.assertEqual(len(validation), 223)
            self.assertEqual([row["id"] for row in prepared_train], [row["id"] for row in original_train])
            self.assertEqual([row["id"] for row in prepared_validation], [row["id"] for row in original_validation])
            self.assertEqual([row["messages"] for row in prepared_train], [row["messages"] for row in original_train])
            self.assertEqual([row["messages"] for row in prepared_validation], [row["messages"] for row in original_validation])
            self.assertFalse(metadata["training_args"]["resplit"])
            self.assertFalse(metadata["training_args"]["allow_replacement"])
            self.assertNotIn("department_dataset_sha256", metadata)
            self.assertNotIn("department", metadata)
            self.assertEqual(metadata["contamination_check"]["status"], "PASS")
            self.assertEqual(metadata["contamination_check"]["train"]["status"], "PASS")
            self.assertEqual(metadata["contamination_check"]["validation"]["status"], "PASS")

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
                mix_pools(
                    pools,
                    ratios={"common": 0.5, "department": 0.5},
                    target_size=2,
                    seed=1,
                    benchmark_root=_benchmark_root(root / "benchmarks"),
                )

    def test_same_user_different_answer_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            train = _record("train", "same user question")
            validation = _record("validation", "same user question")
            validation["messages"][-1]["content"] = "Different answer"
            paths = [root / name for name in ("ct.jsonl", "cv.jsonl", "dt.jsonl", "dv.jsonl")]
            _write(paths[0], [train])
            _write(paths[1], [_record("cv", "other validation")])
            _write(paths[2], [_record("dt", "other department")])
            _write(paths[3], [validation])
            pools = {
                "common": load_pool("common", paths[0], paths[1]),
                "department": load_pool("department", paths[2], paths[3]),
            }
            with self.assertRaisesRegex(DatasetValidationError, "user/question"):
                mix_pools(
                    pools,
                    ratios={"common": 0.5, "department": 0.5},
                    target_size=2,
                    seed=1,
                    benchmark_root=_benchmark_root(root / "benchmarks"),
                )

    def test_normalized_equivalent_user_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            train = _record("train", "What is the balance?")
            validation = _record("validation", "what is the balance")
            validation["messages"][-1]["content"] = "Different answer"
            paths = [root / name for name in ("ct.jsonl", "cv.jsonl", "dt.jsonl", "dv.jsonl")]
            _write(paths[0], [train])
            _write(paths[1], [_record("cv", "other validation")])
            _write(paths[2], [_record("dt", "other department")])
            _write(paths[3], [validation])
            pools = {
                "common": load_pool("common", paths[0], paths[1]),
                "department": load_pool("department", paths[2], paths[3]),
            }
            with self.assertRaisesRegex(DatasetValidationError, "user/question"):
                mix_pools(
                    pools,
                    ratios={"common": 0.5, "department": 0.5},
                    target_size=2,
                    seed=1,
                    benchmark_root=_benchmark_root(root / "benchmarks"),
                )

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
                benchmark_root=_benchmark_root(root / "benchmarks"),
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
                benchmark_root=_benchmark_root(root / "benchmarks"),
            )
            self.assertEqual(len(records), 2)
            self.assertEqual(metadata["deduplication"]["per_pool"]["common"]["exact_duplicates_removed"], 1)

    def test_benchmark_root_validation_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(DatasetValidationError):
                check_contamination([], root / "missing")
            not_directory = root / "not-directory"
            not_directory.write_text("not a directory", encoding="utf-8")
            with self.assertRaises(DatasetValidationError):
                check_contamination([], not_directory)
            empty = root / "empty"
            empty.mkdir()
            with self.assertRaises(DatasetValidationError):
                check_contamination([], empty)
            malformed = root / "malformed"
            malformed.mkdir()
            (malformed / "external50_v1.json").write_text("not json", encoding="utf-8")
            with self.assertRaises(DatasetValidationError):
                check_contamination([], malformed)

    def test_clean_nonempty_benchmark_root_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            benchmark_root = _benchmark_root(Path(directory) / "benchmarks")
            result = check_contamination(
                [validate_record(_record("candidate", "unrelated candidate"), source_dataset="x", source_file="x", source_row=1)],
                benchmark_root,
            )
            self.assertEqual(result["status"], "PASS")
            self.assertGreater(result["benchmark_text_count"], 0)

    def test_exact_and_near_contamination_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exact_question = "This is a sufficiently long held out question for exact contamination testing."
            exact_root = _benchmark_root(root / "exact", {"external50_v1.json": exact_question})
            exact_candidate = validate_record(_record("exact", exact_question), source_dataset="x", source_file="x", source_row=1)
            exact_result = check_contamination([exact_candidate], exact_root)
            with self.assertRaises(DatasetValidationError):
                require_clean(exact_result)

            near_question = "This is a sufficiently long held out question for near contamination testing."
            near_root = _benchmark_root(root / "near", {"external50_v1.json": near_question})
            near_candidate = validate_record(
                _record("near", "This is a sufficiently long held out question for near contamination testings."),
                source_dataset="x",
                source_file="x",
                source_row=1,
            )
            near_result = check_contamination([near_candidate], near_root)
            with self.assertRaises(DatasetValidationError):
                require_clean(near_result)

    def test_assistant_only_collator_masks_prompt(self) -> None:
        feature = _record("normal", "What is known?")
        tokenizer = FakeTokenizer()
        with patch.dict(sys.modules, {"torch": FakeTorch}):
            batch = AssistantOnlyCollator(tokenizer, max_length=20)([feature])
        labels = batch["labels"][0].tolist()
        first_label = next(index for index, value in enumerate(labels) if value != -100)
        self.assertGreater(first_label, 0)
        self.assertTrue(all(value == -100 for value in labels[:first_label]))
        self.assertTrue(any(value != -100 for value in labels[first_label:]))

    def test_fully_truncated_assistant_fails_closed(self) -> None:
        with self.assertRaisesRegex(DatasetValidationError, "sample_id=full"):
            AssistantOnlyCollator(FakeTokenizer(), max_length=2)([_record("full", "long answer")])

    def test_partially_truncated_assistant_fails_closed(self) -> None:
        with self.assertRaisesRegex(DatasetValidationError, "full_token_length"):
            AssistantOnlyCollator(FakeTokenizer(), max_length=5)([_record("partial", "long answer")])

    def test_template_prefix_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(DatasetValidationError, "prefix"):
            AssistantOnlyCollator(PrefixMismatchTokenizer(), max_length=20)([_record("mismatch", "question")])

    def test_length_audit_reports_percentiles_and_over_limit_count(self) -> None:
        datasets = {
            "train": [_record("train", "question")],
            "validation": [_record("validation", "another question")],
        }
        stats = audit_dataset_lengths(FakeTokenizer(), datasets, max_seq_length=20)
        self.assertEqual(stats["example_count"], 2)
        self.assertIn("p50_token_length", stats)
        self.assertIn("p95_token_length", stats)
        self.assertIn("p99_token_length", stats)
        self.assertIn("max_token_length", stats)
        self.assertEqual(stats["over_limit_count"], 0)

    def test_adapter_name_metadata_and_training_defaults(self) -> None:
        self.assertEqual(adapter_name("risk", "1"), "hgfinance-risk-v1")
        self.assertEqual(adapter_name(None, "1", common_only=True), "hgfinance-common-v1")
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
            optimizer_args={"learning_rate": 0.0002, "optimizer": DEFAULT_OPTIMIZER},
            training_args={"validation_size": 1, "optimizer": DEFAULT_OPTIMIZER, "gradient_checkpointing": True},
        )
        self.assertEqual(metadata["common_dataset_sha256"], "common-hash")
        self.assertEqual(metadata["requested_base_revision"], None)
        self.assertEqual(metadata["resolved_base_revision"], None)
        self.assertEqual(metadata["lora"]["lora_r"], 16)
        self.assertEqual(metadata["lora"]["optimizer"], DEFAULT_OPTIMIZER)
        self.assertTrue(metadata["lora"]["gradient_checkpointing"])
        with self.assertRaises(DatasetValidationError):
            require_base_revision(None, dry_run=False)
        require_base_revision(None, dry_run=True)


if __name__ == "__main__":
    unittest.main()
