"""Focused tests for the bounded MemoHarness D5 retention policy."""

from __future__ import annotations

import unittest
from unittest import mock

from orchestration.experience_retention import ExperienceRetentionWorker
from orchestration.experience_retention_policy import (
    D5_CLEANUP_RELATION_BYTES,
    D5_HARD_LIMIT_RELATION_BYTES,
    D5_WRITE_STOP_RELATION_BYTES,
    capacity_band,
    retention_bucket,
    retention_days,
)


class _Cursor:
    def __init__(self, *, scalar=0, rowcount=0):
        self.scalar = scalar
        self.rowcount = rowcount
        self.executed: list[tuple[str, object]] = []

    def execute(self, query, args=()):
        self.executed.append((query, args))

    def fetchone(self):
        return (self.scalar,)

    def close(self):
        return None


class _Connection:
    def __init__(self, *, scalar=0, rowcount=0):
        self.cursor_obj = _Cursor(scalar=scalar, rowcount=rowcount)
        self.commits = 0
        self.rollbacks = 0
        self.autocommit = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        return None


class RetentionPolicyTest(unittest.TestCase):
    def test_failure_classes_have_requested_retention(self):
        self.assertEqual(retention_bucket(True, ()), "success")
        self.assertEqual(retention_days(True, ()), 90)
        self.assertEqual(retention_bucket(False, ("DUPLICATE_MATERIALIZATION",)), "orchestration_failure")
        self.assertEqual(retention_days(False, ("DUPLICATE_MATERIALIZATION",)), 30)
        self.assertEqual(retention_bucket(False, ("PROVIDER_QUOTA",)), "operational_failure")
        self.assertEqual(retention_days(False, ("PROVIDER_QUOTA",)), 14)

    def test_capacity_bands_are_scoped_to_d5_relation(self):
        self.assertEqual(capacity_band(0), "normal")
        self.assertEqual(capacity_band(30 * 1024 * 1024), "warning")
        self.assertEqual(capacity_band(D5_CLEANUP_RELATION_BYTES), "cleanup")
        self.assertEqual(capacity_band(D5_WRITE_STOP_RELATION_BYTES), "write_stop")
        self.assertEqual(capacity_band(D5_HARD_LIMIT_RELATION_BYTES), "hard_limit")

    def test_disabled_worker_does_not_connect(self):
        connect = mock.Mock(side_effect=AssertionError("disabled retention must not connect"))
        worker = ExperienceRetentionWorker(
            "postgresql://unused",
            enabled=False,
            connect_factory=connect,
        )
        result = worker.run_once()
        self.assertEqual(result.capacity, "disabled")
        connect.assert_not_called()

    def test_cleanup_is_bounded_and_protects_recent_representatives(self):
        connection = _Connection(scalar=36 * 1024 * 1024, rowcount=2)
        worker = ExperienceRetentionWorker(
            "postgresql://test",
            enabled=True,
            batch_size=500,
            max_pressure_batches=1,
            vacuum_analyze=False,
            connect_factory=lambda *_args, **_kwargs: connection,
        )
        result = worker.run_once()
        self.assertTrue(result.available)
        self.assertEqual(result.expired_deleted, 2)
        self.assertEqual(result.pressure_deleted, 2)
        self.assertEqual(connection.commits, 2)
        sql = "\n".join(query for query, _args in connection.cursor_obj.executed)
        self.assertIn("row_number() OVER", sql)
        self.assertIn("PARTITION BY case_type, binding, orchestration_policy", sql)
        self.assertIn("LIMIT", sql)
        self.assertIn("PROVIDER_AUTH", str(connection.cursor_obj.executed))


if __name__ == "__main__":
    unittest.main()
