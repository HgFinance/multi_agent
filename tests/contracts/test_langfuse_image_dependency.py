"""Langfuse 자격증명을 받는 서비스는 그 패키지도 이미지에 있어야 한다.

## 왜 이 테스트가 있나 (2026-08-23)

compose 에 LANGFUSE_* 4줄을 넣어도 그 이미지에 langfuse 가 없으면
publish 가 ModuleNotFoundError 를 삼키고 **조용히 False** 를 돌려준다.
코드도 자격증명도 프로필 해석도 전부 정상인데 이벤트만 0건이라, 어디가
끊겼는지 보이지 않는다 - ceo-kanban-supervisor 에서 실제로 그 상태를
추적하는 데 몇 시간이 걸렸고, 같은 결함이 audit-api/risk-api/research-mcp
세 곳에 더 있었다.

사람이 "키 넣을 때 패키지도 확인"하는 규율로는 네 번 연속 놓쳤다.
그래서 규율이 아니라 테스트로 고정한다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (
    ROOT / "docker-compose.yml",
    ROOT / "departments" / "07-agent-workforce" / "compose.yaml",
)


def _services_with_langfuse_env() -> dict[str, str]:
    """LANGFUSE_* 를 받는 서비스 -> 그 이미지를 만드는 Dockerfile 경로."""

    found: dict[str, str] = {}
    for compose_path in COMPOSE_FILES:
        if not compose_path.is_file():
            continue
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
        for name, service in (document.get("services") or {}).items():
            if not isinstance(service, dict):
                continue
            environment = service.get("environment") or {}
            keys = environment if isinstance(environment, dict) else {
                str(item).split("=", 1)[0] for item in environment
            }
            if not any(str(key).startswith("LANGFUSE") for key in keys):
                continue
            build = service.get("build")
            if isinstance(build, dict):
                dockerfile = build.get("dockerfile") or "Dockerfile"
            elif isinstance(build, str):
                dockerfile = "Dockerfile"
            else:
                # 순정 이미지를 그대로 쓰는 서비스는 우리가 패키지를 못 넣는다.
                continue
            # compose fragment 의 상대 경로는 그 파일 기준이다.
            candidate = (compose_path.parent / dockerfile).resolve()
            if not candidate.is_file():
                candidate = (ROOT / dockerfile).resolve()
            found[name] = str(candidate.relative_to(ROOT)).replace("\\", "/")
    return found


def _provides_langfuse_transport(dockerfile: Path) -> bool:
    text = dockerfile.read_text(encoding="utf-8")
    # 주석(# langfuse: ...)은 설치가 아니다 - 실제 설치 줄만 본다.
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    body = "\n".join(lines)
    if re.search(r"\blangfuse\b", body):
        return True
    # The head-trace collector deliberately uses the Langfuse OTLP HTTP
    # endpoint through the standard library. Requiring the SDK here would
    # widen the image only to satisfy a stale package-name assumption.
    if "orchestration/langfuse_otlp.py" in body:
        return True
    # requirements.txt 를 통째로 설치하는 이미지는 거기에 있으면 된다.
    if "requirements.txt" in body:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        return bool(re.search(r"^\s*langfuse", requirements, re.MULTILINE))
    return False


SERVICES = _services_with_langfuse_env()


def test_at_least_one_service_receives_langfuse_env() -> None:
    """탐지 자체가 깨지면 아래 테스트가 조용히 통과한다 - 그것부터 막는다."""

    assert SERVICES, "LANGFUSE_* 를 받는 서비스를 하나도 못 찾았다 - 탐지 로직 점검 필요"


@pytest.mark.parametrize(("service", "dockerfile"), sorted(SERVICES.items()))
def test_service_with_langfuse_env_has_the_package(service: str, dockerfile: str) -> None:
    """키를 주면서 패키지를 안 넣으면 그 서비스는 조용히 계측되지 않는다."""

    path = ROOT / dockerfile
    assert path.is_file(), f"{service}: Dockerfile 을 찾을 수 없다 ({dockerfile})"
    assert _provides_langfuse_transport(path), (
        f"{service} 는 LANGFUSE_* 를 받지만 {dockerfile} 이 SDK 또는 OTLP 전송 계층을 "
        "제공하지 않는다. 이 상태에서는 publish 가 조용히 False 를 돌려주고 "
        "이벤트가 0건이 된다."
    )
