"""Build-time Hermes patch contract for explicit initial `blocked` cards."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PATCH = (
    Path(__file__).resolve().parents[2]
    / "deploy/hermes-patches/install_sticky_initial_block.py"
)
ANCHOR_SOURCE = '''                _inherit_notify_subs(conn, task_id, parents, created_at=now)
            return task_id
'''


class HermesInitialBlockPatchTest(unittest.TestCase):
    def test_patch_records_a_sticky_block_event_and_refuses_repeat_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "kanban_db.py"
            target.write_text(ANCHOR_SOURCE, encoding="utf-8")

            applied = subprocess.run(
                [sys.executable, str(PATCH), "--target", str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            patched = target.read_text(encoding="utf-8")
            self.assertIn('"blocked"', patched)
            self.assertIn('"reason": "initial_status=blocked"', patched)

            repeated = subprocess.run(
                [sys.executable, str(PATCH), "--target", str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(repeated.returncode, 0)


if __name__ == "__main__":
    unittest.main()
