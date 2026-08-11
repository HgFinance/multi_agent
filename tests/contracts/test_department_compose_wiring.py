"""Department Compose가 durable 저장소 모드를 실제 컨테이너에 주입하는지 검증한다."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _service_block(compose_text: str, service_name: str) -> str:
    """Return one service block from the small department Compose fragments."""
    lines = compose_text.splitlines()
    start = lines.index(f"  {service_name}:") + 1
    block = []
    for line in lines[start:]:
        if line.startswith("  ") and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


class DepartmentComposeWiringTests(unittest.TestCase):
    def test_trading_api_receives_paper_db_and_shared_database_url(self) -> None:
        compose = (ROOT / "departments/02-trading/compose.yaml").read_text(
            encoding="utf-8"
        )
        service = _service_block(compose, "trading-api")

        self.assertIn("PAPER_DB: ${PAPER_DB:-true}", service)
        self.assertIn("DATABASE_URL: ${DATABASE_URL:-}", service)

    def test_trading_hermes_uses_standard_profile_path(self) -> None:
        compose = (ROOT / "departments/02-trading/compose.yaml").read_text(
            encoding="utf-8"
        )
        # 호스트 접두사(${USERPROFILE} / /home/ubuntu)는 배포 환경마다 다르므로
        # 고정하지 않는다. 고정하는 것은 **레이아웃**이다 - profiles/<부서> 아래에
        # 있고, 구형 ~/.hermes-<부서> 로 돌아가지 않는다.
        self.assertIn("/.hermes/profiles/trading-department:/opt/data", compose)
        self.assertNotIn(".hermes-trading-department", compose)

        sync_script = (ROOT / "scripts/sync_hermes_profiles.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('DEST_ROOT="${HERMES_HOME:-$HOME/.hermes}/profiles"', sync_script)
        self.assertNotIn("HERMES_HOME_PREFIX", sync_script)

    def test_accounting_api_defaults_to_durable_paper_mode(self) -> None:
        compose = (ROOT / "departments/05-accounting-portfolio/compose.yaml").read_text(
            encoding="utf-8"
        )
        service = _service_block(compose, "accounting-api")

        self.assertIn("ACCOUNTING_MODE: ${ACCOUNTING_MODE:-PAPER_DB}", service)
        self.assertIn("DATABASE_URL: ${DATABASE_URL:-}", service)

    def test_accounting_hermes_uses_standard_profile_path(self) -> None:
        compose = (ROOT / "departments/05-accounting-portfolio/compose.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "/.hermes/profiles/accounting-portfolio-department:/opt/data", compose
        )
        self.assertNotIn(".hermes-accounting-portfolio-department", compose)
