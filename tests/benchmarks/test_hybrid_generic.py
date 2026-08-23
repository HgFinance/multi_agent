import unittest
from pathlib import Path

from benchmarks.quantization.glossary_rag import inject, load_glossary
from benchmarks.quantization.safe_expression import ExpressionError, evaluate_response
from benchmarks.quantization.structured_output import (
    control_envelope_schema,
    infer_schema_from_contract,
    unwrap_control_envelope,
    validate_json,
)


class GenericHybridTests(unittest.TestCase):
    def test_expression_tool_is_generic_and_safe(self):
        result = evaluate_response("EXPR: (15000000 / 50000000) * 100")
        self.assertEqual(result.value, 30)

        with self.assertRaises(ExpressionError):
            evaluate_response("EXPR: __import__('os').system('id')")

    def test_expression_tool_rejects_division_by_zero(self):
        with self.assertRaises(ExpressionError):
            evaluate_response("EXPR: 100 / 0")

    def test_structured_schema_is_inferred_from_contract_not_gold(self):
        context = (
            'Return JSON with exactly:\n'
            '- action: "APPROVE" or "RESIZE"\n'
            '- max_additional_notional: integer KRW\n'
        )
        schema = infer_schema_from_contract(context)
        self.assertIsNotNone(schema)
        self.assertTrue(validate_json('{"action":"RESIZE","max_additional_notional":1500000}', schema).valid)
        self.assertFalse(validate_json('{"action":"RESIZE","unexpected":1}', schema).valid)

    def test_structured_control_envelope_unwraps_only_valid_result(self):
        context = (
            'Return JSON with exactly:\n'
            '- action: "APPROVE" or "RESIZE"\n'
            '- max_additional_notional: integer KRW\n'
        )
        result_schema = infer_schema_from_contract(context)
        envelope_schema = control_envelope_schema(result_schema)
        raw = (
            '{"status":"SUCCESS","result":{"action":"RESIZE",'
            '"max_additional_notional":1500000},"expression":null,'
            '"missing_params":[],"reason":null}'
        )
        self.assertTrue(validate_json(raw, envelope_schema).valid)
        unwrapped = unwrap_control_envelope(raw, result_schema)
        self.assertTrue(unwrapped.valid)
        self.assertEqual(unwrapped.value["action"], "RESIZE")

        insufficient = (
            '{"status":"INSUFFICIENT_DATA","result":null,"expression":null,'
            '"missing_params":["denominator"],"reason":"missing input"}'
        )
        self.assertFalse(unwrap_control_envelope(insufficient, result_schema).valid)

    def test_glossary_does_not_match_substrings(self):
        path = Path("benchmarks/quantization/knowledge/bok800_2026/glossary_rag_v1.json")
        digest, entries = load_glossary(path)
        injected, terms = inject("In 2022 Q2, compare the segments.", entries)
        self.assertEqual(digest, "d3da5743695146b492835d1e71b7d373ffc268686917e8e4494378b7e823f369")
        self.assertNotIn("G2(Group of Two)", terms)
        self.assertIn("In 2022 Q2", injected)

    def test_glossary_query_scope_avoids_context_noise(self):
        path = Path("benchmarks/quantization/knowledge/bok800_2026/glossary_rag_v1.json")
        _, entries = load_glossary(path)
        injected, terms = inject(
            "The context contains EPS but the question is about cash proceeds.",
            entries,
            query="What were the cash proceeds?",
        )
        self.assertNotIn("주당순이익(EPS)", terms)
        self.assertIn("The context contains EPS", injected)


if __name__ == "__main__":
    unittest.main()
