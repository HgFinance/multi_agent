"""Executable contract checks for the unified Domain API specification.

The route probe intentionally runs each FastAPI app in a subprocess. Several
apps use the conventional module name app and have import-time state, so
loading them into one Python process would make the comparison order-dependent.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "docs" / "02-engineering" / "contracts"
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+\.v[0-9]+$")


def load_json(name: str) -> dict[str, Any]:
    with (CONTRACTS / name).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AssertionError(f"{name} must contain a JSON object")
    return value


def route_pairs(operations: list[dict[str, str]]) -> set[tuple[str, str]]:
    return {(item["method"].upper(), item["path"]) for item in operations}


def probe_openapi(app: dict[str, Any]) -> dict[str, Any]:
    """Return the visible OpenAPI paths for one registry app."""

    probe = """
import importlib
import json
import sys

module = importlib.import_module(sys.argv[1])
openapi = module.app.openapi()
print("__UNIFIED_OPENAPI__" + json.dumps(openapi.get("paths", {}), sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, app["module_name"]],
        cwd=ROOT / app["working_directory"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"{app['app_id']} OpenAPI probe failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    marker = "__UNIFIED_OPENAPI__"
    for line in reversed(result.stdout.splitlines()):
        if line.startswith(marker):
            return json.loads(line[len(marker) :])
    raise AssertionError(
        f"{app['app_id']} OpenAPI probe did not return a marker; stdout="
        f"{result.stdout[-500:]}"
    )


def openapi_pairs(paths: dict[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            raise AssertionError(f"OpenAPI path item is not an object: {path}")
        for method, operation in path_item.items():
            if method.upper() not in HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                raise AssertionError(f"OpenAPI operation is not an object: {method} {path}")
            pairs.add((method.upper(), path))
    return pairs


class UnifiedRouteRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_json("route-registry.v1.json")
        cls.apps = {item["app_id"]: item for item in cls.registry["apps"]}

    def test_registry_shape_and_statuses(self) -> None:
        registry = self.registry
        statuses = set(registry["statuses"])
        self.assertEqual(registry["schema_version"], "route-registry.v1")
        self.assertEqual(len(self.apps), len(registry["apps"]), "duplicate app_id")

        for app_id, entry in registry["actual_routes"].items():
            self.assertIn(app_id, self.apps)
            self.assertEqual(self.apps[app_id]["openapi_status"], "ready")
            self.assertIn(entry["status"], statuses)
            seen: set[tuple[str, str]] = set()
            for operation in entry["operations"]:
                self.assert_valid_operation(operation)
                pair = (operation["method"], operation["path"])
                self.assertNotIn(pair, seen, f"duplicate actual route: {app_id} {pair}")
                seen.add(pair)

        planned: set[tuple[str, str, str]] = set()
        for entry in registry["planned_routes"]:
            app_id = entry["app_id"]
            self.assertIn(app_id, self.apps)
            self.assertEqual(entry.get("status"), "PLANNED")
            for operation in entry["operations"]:
                self.assert_valid_operation(operation)
                pair = (app_id, operation["method"], operation["path"])
                self.assertNotIn(pair, planned, f"duplicate planned route: {pair}")
                planned.add(pair)

        actual = {
            (app_id, method, path)
            for app_id, entry in registry["actual_routes"].items()
            for method, path in route_pairs(entry["operations"])
        }
        self.assertTrue(actual.isdisjoint(planned), "planned route is already registered as actual")

        for excluded in registry["excluded_routes"]:
            self.assert_valid_operation(excluded)
            self.assertNotIn(
                (excluded["app_id"], excluded["method"], excluded["path"]),
                actual,
                "excluded route must not be in actual_routes",
            )

    def test_ready_apps_match_app_openapi_exactly(self) -> None:
        """Missing, extra, and method-mismatched operations all fail this test."""

        for app_id, app in self.apps.items():
            if app["openapi_status"] != "ready":
                continue

            actual = openapi_pairs(probe_openapi(app))
            registered = route_pairs(self.registry["actual_routes"][app_id]["operations"])
            missing = sorted(registered - actual)
            extra = sorted(actual - registered)
            self.assertFalse(
                missing or extra,
                f"{app_id} Route Registry != app.openapi(): missing={missing}; extra={extra}",
            )

            for planned_entry in self.registry["planned_routes"]:
                if planned_entry["app_id"] != app_id:
                    continue
                planned = route_pairs(planned_entry["operations"])
                self.assertTrue(
                    actual.isdisjoint(planned),
                    f"planned route appeared in OpenAPI for {app_id}: {sorted(actual & planned)}",
                )

            excluded = {
                (item["method"], item["path"])
                for item in self.registry["excluded_routes"]
                if item["app_id"] == app_id
            }
            self.assertTrue(
                actual.isdisjoint(excluded),
                f"excluded route appeared in OpenAPI for {app_id}: {sorted(actual & excluded)}",
            )

    def assert_valid_operation(self, operation: dict[str, Any]) -> None:
        self.assertIn(operation.get("method"), HTTP_METHODS)
        path = operation.get("path")
        self.assertIsInstance(path, str)
        self.assertTrue(path.startswith("/"), path)
        self.assertNotIn("//", path, path)


class UnifiedEventRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_json("event-registry.v1.json")

    def test_event_registry_is_unique_and_source_accurate(self) -> None:
        registry = self.registry
        statuses = set(registry["statuses"])
        bindings = set(registry["case_bindings"])
        events = registry["events"]
        event_types = [event["event_type"] for event in events]

        self.assertEqual(registry["schema_version"], "event-registry.v1")
        self.assertEqual(len(event_types), len(set(event_types)), "duplicate event_type")
        for event in events:
            self.assertRegex(event["event_type"], EVENT_TYPE_RE)
            self.assertIn(event["status"], statuses)
            self.assertIn(event["case_binding"], bindings)
            self.assertTrue(event["producer"])
            self.assertTrue(event["consumers"])
            if event["status"].startswith("IMPLEMENTED"):
                source = ROOT / event["source"]
                self.assertTrue(source.is_file(), f"missing source for {event['event_type']}: {source}")
                self.assertIn(
                    event["event_type"],
                    source.read_text(encoding="utf-8"),
                    f"event string not found in source for {event['event_type']}",
                )

        canonical = set(event_types)
        for alias in registry["aliases"]:
            self.assertEqual(alias["status"], "LEGACY_ALIAS")
            self.assertNotEqual(alias["alias"], alias["canonical"])
            self.assertIn(alias["canonical"], canonical)


class JsonSchemaSubsetTest(unittest.TestCase):
    """Validate the contract examples without adding a runtime dependency."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.envelope = load_json("event-envelope.v1.json")
        cls.worker = load_json("worker-context.v1.json")

    def test_contract_schemas_have_required_object_shape(self) -> None:
        for schema in (self.envelope, self.worker):
            self.assertEqual(schema["type"], "object")
            self.assertTrue(schema["required"])
            self.assertIsInstance(schema["properties"], dict)
            self.assertFalse(schema["additionalProperties"])

    def test_valid_event_envelope_and_worker_context(self) -> None:
        envelope = {
            "event_id": "00000000-0000-0000-0000-000000000001",
            "event_type": "research.packet.v1",
            "schema_version": "event-envelope-v1",
            "case_id": "00000000-0000-0000-0000-000000000002",
            "trace_id": "00000000-0000-0000-0000-000000000003",
            "producer": "research-api",
            "occurred_at": "2026-08-04T00:35:00Z",
            "idempotency_key": "research:case:packet:v3",
            "payload_ref": {
                "artifact_type": "RESEARCH_PACKET",
                "artifact_id": "00000000-0000-0000-0000-000000000004",
                "artifact_schema": "research-contracts-v2",
                "content_hash": "sha256:" + "0" * 64,
            },
        }
        worker = {
            "context_id": "00000000-0000-0000-0000-000000000011",
            "schema_version": "risk.worker-context.v1",
            "department": "risk-management",
            "trace_id": "00000000-0000-0000-0000-000000000012",
            "case_id": None,
            "producer_worker": "compliance-policy-worker",
            "consumer_worker": "risk-supervisor",
            "status": "COMPLETED",
            "advisory": {"summary": "Risk evidence collected", "suggested_verdict": "RESIZE"},
            "reason_codes": ["MAX_SYMBOL_WEIGHT"],
            "input_refs": [
                {
                    "type": "ORDER_INTENT",
                    "id": "00000000-0000-0000-0000-000000000013",
                    "content_hash": "sha256:" + "1" * 64,
                }
            ],
            "output_refs": [],
            "profile_version": "risk-worker-profile-v3",
            "created_at": "2026-08-04T00:30:00Z",
            "attempt": 1,
            "timeout_ms": 30000,
            "calculation_version": "risk-engine-v2",
        }
        validate_json_schema(envelope, self.envelope)
        validate_json_schema(worker, self.worker)

    def test_missing_required_and_unknown_fields_fail(self) -> None:
        envelope = {
            "event_type": "research.packet.v1",
            "schema_version": "event-envelope-v1",
            "trace_id": "00000000-0000-0000-0000-000000000003",
            "producer": "research-api",
            "occurred_at": "2026-08-04T00:35:00Z",
            "idempotency_key": "key",
        }
        with self.assertRaises(AssertionError):
            validate_json_schema(envelope, self.envelope)

        invalid_worker = {
            "context_id": "00000000-0000-0000-0000-000000000011",
            "schema_version": "worker-context.v1",
            "department": "qa-department",
            "trace_id": "00000000-0000-0000-0000-000000000012",
            "producer_worker": "evidence-worker",
            "consumer_worker": "qa-head",
            "status": "ESCALATE",
            "advisory": {"summary": "insufficient evidence"},
            "reason_codes": ["INSUFFICIENT_EVIDENCE"],
            "input_refs": [],
            "output_refs": [],
            "profile_version": "qa-worker-profile-v1",
            "created_at": "2026-08-04T00:30:00Z",
            "unexpected": True,
        }
        with self.assertRaises(AssertionError):
            validate_json_schema(invalid_worker, self.worker)


def validate_json_schema(
    instance: Any,
    schema: dict[str, Any],
    path: str = "$",
    root: dict[str, Any] | None = None,
) -> None:
    """Small deterministic validator for the JSON Schema keywords used here."""

    root = root or schema
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/$defs/"):
            raise AssertionError(f"{path}: unsupported reference {reference}")
        target = root["$defs"][reference.removeprefix("#/$defs/")]
        validate_json_schema(instance, target, path, root)
        return

    if "anyOf" in schema:
        errors: list[str] = []
        for option in schema["anyOf"]:
            try:
                validate_json_schema(instance, option, path, root)
                break
            except AssertionError as error:
                errors.append(str(error))
        else:
            raise AssertionError(f"{path}: no anyOf branch matched: {errors}")
        return

    if "const" in schema and instance != schema["const"]:
        raise AssertionError(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise AssertionError(f"{path}: value is not in enum")

    expected = schema.get("type")
    if expected is not None and not matches_type(instance, expected):
        raise AssertionError(f"{path}: expected {expected}, got {type(instance).__name__}")
    if instance is None:
        return

    if isinstance(instance, dict):
        for required in schema.get("required", []):
            if required not in instance:
                raise AssertionError(f"{path}: missing required property {required}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = set(instance) - set(properties)
            if unknown:
                raise AssertionError(f"{path}: unknown properties {sorted(unknown)}")
        for key, value in instance.items():
            if key in properties:
                validate_json_schema(value, properties[key], f"{path}.{key}", root)
    elif isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise AssertionError(f"{path}: fewer than minItems")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in instance}) != len(instance):
            raise AssertionError(f"{path}: items are not unique")
        if "items" in schema:
            for index, item in enumerate(instance):
                validate_json_schema(item, schema["items"], f"{path}[{index}]", root)
    elif isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            raise AssertionError(f"{path}: shorter than minLength")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise AssertionError(f"{path}: longer than maxLength")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            raise AssertionError(f"{path}: pattern mismatch")
        if schema.get("format") == "uuid":
            try:
                uuid.UUID(instance)
            except ValueError as error:
                raise AssertionError(f"{path}: invalid UUID") from error
        if schema.get("format") == "date-time":
            try:
                parsed = datetime.fromisoformat(instance.replace("Z", "+00:00"))
            except ValueError as error:
                raise AssertionError(f"{path}: invalid date-time") from error
            if parsed.tzinfo is None:
                raise AssertionError(f"{path}: date-time must include timezone")
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            raise AssertionError(f"{path}: below minimum")


def matches_type(value: Any, expected: str | list[str]) -> bool:
    expected_types = [expected] if isinstance(expected, str) else expected
    for item in expected_types:
        if item == "null" and value is None:
            return True
        if item == "object" and isinstance(value, dict):
            return True
        if item == "array" and isinstance(value, list):
            return True
        if item == "string" and isinstance(value, str):
            return True
        if item == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if item == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if item == "boolean" and isinstance(value, bool):
            return True
    return False


if __name__ == "__main__":
    unittest.main()
