#!/usr/bin/env python3
"""지금 실제로 떠 있는 페이퍼 전략 컨테이너(mlpipe-paper) 하나의 상태·성과 조회와
전원(start/stop) 제어.

▶ 왜 `strategy.strategies`/`quant.hypotheses` 대신 여기서 파일·컨테이너를 직접 보는가
  `strategy.strategies`/`versions`/`deployments`/`evaluations`는 스키마만 있고
  실제로 쓰는 코드가 아직 없다(`pipeline/strategy_lifecycle.py` 자체 주석,
  2026-08-04 실측, 호출처 0개). 반면 채택된 알파 전략은 이미 실물로 떠 있다 -
  `~/mlpipe-paper/run_paper.sh`가 `docker run --name strategy-spike-fade-v2`로
  띄운 컨테이너이고, 그 스크립트 주석 자체가 "stable container name for the
  front"라고 적어뒀다. 없는 레지스트리를 조회하는 척하며 빈 결과를 "채택된
  전략 없음"으로 보여주는 대신, 실제로 도는 것을 있는 그대로 읽는다.

▶ 이 모듈이 절대 하지 않는 것
  임의 컨테이너 조작. 기존 고정 컨테이너는 `STRATEGY_CONTAINER_NAME` 하나만
  전원 제어하고, 동적 PAPER 배포는 서버가 만든 `deployment-<24 hex>` ID에서
  계산한 이름만 허용한다. 호출부가 이미지·command·host path·credential을
  넘길 수 없고, child container에는 Docker socket도 없다. 제거는 해당 PAPER
  child만 폐기하며 연구 원장과 상태 volume은 보존한다.

▶ 전원 조작 기본값이 꺼져 있는 이유
  이 BFF가 컨테이너로 배포되면(AWS EB) docker 소켓 자체가 안 보여 이 모듈은
  자연히 503으로 떨어진다. 하지만 host에서 직접 뜨는 배포(현재 이 서버)는
  docker CLI가 그대로 보이므로, 그 경로에서도 운영자가 명시적으로 켜야만
  조작이 가능하게 한다 - `ENABLE_LS_ORDER_EVENTS`와 같은 기본값 정책이다.

자체 점검: python apps/api/strategy_runtime.py (docker 없으면 순수 함수만 검증)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

# 지금 실제로 떠 있는 페이퍼 전략 컨테이너 중 하나다(2026-08-23 `docker ps`
# 실측 - `strategy-spike-fade`/`strategy-vacuum-long`/`strategy-ride-to-close`
# 3개가 각각 `<name>:engine` 이미지로 떠 있다). 이 패널은 우선 하나만 본다.
# 여러 전략을 함께 보여줄 때는 이 상수를 조회 가능한 목록으로 바꾸되, 그때도
# 브라우저가 임의 이름을 보내게 하지 않는다.
STRATEGY_CONTAINER_NAME = "strategy-spike-fade"

# `run_paper.sh`가 원장(ledger)·팩(pack)을 남기는 호스트 경로와 같다. docker
# 접근 없이도 이 파일들만으로 성과·모델 구성을 읽을 수 있다.
MLPIPE_HOME = Path(os.getenv("MLPIPE_PAPER_HOME", str(Path.home() / "mlpipe-paper")))

STRATEGY_CONTAINER_CONTROL_ENABLED = os.getenv(
    "ENABLE_STRATEGY_CONTAINER_CONTROL", "false"
).casefold() in {"1", "true", "yes", "on"}

# Dynamic release is still an allowlisted operation. The sidecar may launch
# only the fixed PAPER executor image, on its own compose network, with the
# research volume read-only. It never accepts an image, command, host path, or
# broker credential from a browser request.
STRATEGY_PAPER_IMAGE = os.getenv(
    "STRATEGY_PAPER_IMAGE", "hedgefund-operations-runtime:latest"
)
STRATEGY_RUNTIME_CONTROL_NAME = os.getenv(
    "STRATEGY_RUNTIME_CONTROL_NAME", "hedgefund-strategy-runtime-control"
)
STRATEGY_LAB_VOLUME = os.getenv(
    "STRATEGY_LAB_VOLUME", "hedgefund_autonomous_research_lab"
)
STRATEGY_RUNTIME_STATE_VOLUME = os.getenv(
    "STRATEGY_RUNTIME_STATE_VOLUME", "hedgefund_strategy_runtime_data"
)
STRATEGY_RUNTIME_STATE_ROOT = Path(
    os.getenv("STRATEGY_RUNTIME_STATE_ROOT", "/var/lib/strategy-runtime")
)
_DEPLOYMENT_ID_RE = re.compile(r"^deployment-[0-9a-f]{24}$")

# `docker stop`의 기본 유예시간이 10초다(SIGTERM 후 안 죽으면 SIGKILL). 이보다
# 짧게 잡으면 컨테이너가 정상적으로 멈추는 중인데도 우리 쪽에서 먼저 타임아웃을
# 내 "실패"로 보고한다(더미 컨테이너로 실측 - PID 1이 SIGTERM을 무시해 정확히
# 이 경계에서 걸렸다). 넉넉하게 잡는다.
DOCKER_TIMEOUT_SECONDS = 15

PowerAction = Literal["start", "stop"]


class StrategyRuntimeError(RuntimeError):
    """docker CLI에 닿지 못했거나 조작이 거부됐다. 호출부가 503으로 옮긴다."""


def _deployment_container_name(deployment_id: str) -> str:
    if not _DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        raise StrategyRuntimeError("배포 ID가 허용된 형식이 아닙니다.")
    return f"strategy-paper-{deployment_id.removeprefix('deployment-')}"


def _read_runtime_state(deployment_id: str) -> dict[str, Any] | None:
    path = STRATEGY_RUNTIME_STATE_ROOT / f"{deployment_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def deploy_paper_bundle(
    *, deployment_id: str, request_id: str, bundle_path: str, bundle_hash: str | None
) -> dict[str, Any]:
    """Launch one immutable, signal-only PAPER strategy container.

    The caller supplies only a path previously written by the BFF. This sidecar
    revalidates the path and content hash, then constructs every Docker option
    itself. The resulting container has no Docker socket, broker key, or write
    access to the research lab.
    """

    if not STRATEGY_CONTAINER_CONTROL_ENABLED:
        raise StrategyRuntimeError("전략 컨테이너 배포가 이 환경에서 꺼져 있습니다.")
    if not request_id or not bundle_path.startswith("/var/lib/autonomous-research/labs/"):
        raise StrategyRuntimeError("배포 Bundle 경로가 허용된 연구실 경로가 아닙니다.")
    if Path(bundle_path).name != f"{deployment_id}.json":
        raise StrategyRuntimeError("배포 Bundle 파일명이 deployment_id와 다릅니다.")
    if not bundle_hash or not re.fullmatch(r"[0-9a-f]{64}", bundle_hash):
        raise StrategyRuntimeError("배포 Bundle hash가 없습니다.")
    container_name = _deployment_container_name(deployment_id)

    existing = container_status(container_name)
    if existing.get("found"):
        return {
            "deployment_id": deployment_id,
            "request_id": request_id,
            "container_name": container_name,
            "container_id": existing.get("container_id"),
            "runtime_status": "RUNNING" if existing.get("running") else "STOPPED",
            "container": existing,
            "execution_status": "SIGNAL_ONLY",
        }

    image = _docker("image", "inspect", STRATEGY_PAPER_IMAGE)
    if image.returncode != 0:
        raise StrategyRuntimeError(
            f"허용된 전략 실행기 이미지가 없습니다: {STRATEGY_PAPER_IMAGE}"
        )
    control = container_status(STRATEGY_RUNTIME_CONTROL_NAME)
    if not control.get("found"):
        raise StrategyRuntimeError("strategy-runtime-control 컨테이너가 없습니다.")

    args = [
        "run",
        "-d",
        "--name",
        container_name,
        "--restart",
        "unless-stopped",
        "--label",
        "com.hgfinance.strategy-deployment=paper",
        "--label",
        f"com.hgfinance.deployment-id={deployment_id}",
        "--label",
        f"com.hgfinance.request-id={request_id}",
        "--network",
        f"container:{STRATEGY_RUNTIME_CONTROL_NAME}",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "64",
        "--memory",
        "512m",
        "--cpus",
        "0.5",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--mount",
        f"type=volume,source={STRATEGY_LAB_VOLUME},target=/var/lib/autonomous-research,readonly",
        "--mount",
        f"type=volume,source={STRATEGY_RUNTIME_STATE_VOLUME},target=/var/lib/strategy-runtime",
        "--env",
        "MARKET_API_URL=http://market-api:8036",
        "--env",
        f"STRATEGY_PAPER_RUNTIME_STATE_DIR=/var/lib/strategy-runtime",
        "--env",
        f"STRATEGY_DEPLOYMENT_ID={deployment_id}",
        STRATEGY_PAPER_IMAGE,
        "python",
        "-m",
        "apps.api.strategy_paper_executor",
        "--bundle",
        bundle_path,
        "--expected-hash",
        bundle_hash,
    ]
    result = _docker(*args)
    if result.returncode != 0:
        raise StrategyRuntimeError((result.stderr or "docker run 실패").strip())
    status = container_status(container_name)
    status["container_id"] = (result.stdout or "").strip() or None
    return {
        "deployment_id": deployment_id,
        "request_id": request_id,
        "container_name": container_name,
        "container_id": status.get("container_id"),
        "runtime_status": "RUNNING" if status.get("running") else "STARTED",
        "container": status,
        "execution_status": "SIGNAL_ONLY",
    }


def power_paper_deployment(*, deployment_id: str, action: PowerAction) -> dict[str, Any]:
    """Start or stop only the deterministic container for one deployment."""

    if action not in ("start", "stop"):
        raise StrategyRuntimeError(f"알 수 없는 PAPER 전략 action: {action!r}")
    if not STRATEGY_CONTAINER_CONTROL_ENABLED:
        raise StrategyRuntimeError("전략 컨테이너 전원 조작이 이 배포에서 꺼져 있습니다.")
    container_name = _deployment_container_name(deployment_id)
    current = container_status(container_name)
    if not current.get("found"):
        raise StrategyRuntimeError("해당 PAPER 전략 컨테이너가 없습니다.")
    result = _docker(action, container_name)
    if result.returncode != 0:
        raise StrategyRuntimeError((result.stderr or f"docker {action} 실패").strip())
    after = container_status(container_name)
    return {
        "deployment_id": deployment_id,
        "container_name": container_name,
        "container": after,
        "runtime_status": "RUNNING" if action == "start" else "STOPPED",
        "execution_status": "SIGNAL_ONLY",
    }


def remove_paper_deployment(*, deployment_id: str) -> dict[str, Any]:
    """Remove only the deterministic PAPER container; its state volume remains."""

    if not STRATEGY_CONTAINER_CONTROL_ENABLED:
        raise StrategyRuntimeError("전략 컨테이너 제거가 이 배포에서 꺼져 있습니다.")
    container_name = _deployment_container_name(deployment_id)
    current = container_status(container_name)
    if current.get("found"):
        result = _docker("rm", "-f", container_name)
        if result.returncode != 0 and container_status(container_name).get("found"):
            raise StrategyRuntimeError((result.stderr or "docker rm 실패").strip())
    return {
        "deployment_id": deployment_id,
        "container_name": container_name,
        "runtime_status": "REMOVED",
        "execution_status": "DISABLED",
    }


def paper_deployment_snapshot(deployment_id: str) -> dict[str, Any]:
    container_name = _deployment_container_name(deployment_id)
    container = container_status(container_name)
    return {
        "deployment_id": deployment_id,
        "container_name": container_name,
        "container": container,
        "runtime": _read_runtime_state(deployment_id),
        "execution_status": "SIGNAL_ONLY",
    }


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise StrategyRuntimeError("이 서버에서 docker CLI를 찾을 수 없습니다.") from exc
    except subprocess.TimeoutExpired as exc:
        raise StrategyRuntimeError("docker 명령이 시간 내에 응답하지 않았습니다.") from exc


def container_status(name: str = STRATEGY_CONTAINER_NAME) -> dict[str, Any]:
    """`docker inspect`로 현재 상태만 읽는다. 쓰기 없음."""
    result = _docker("inspect", "--format", "{{json .State}}", name)
    if result.returncode != 0:
        # "No such object"는 컨테이너가 아예 없다는 뜻이다 - 아직 한 번도 안
        # 띄웠거나 이름이 바뀐 것이다. 오류를 "꺼짐"으로 위장하지 않는다.
        return {
            "found": False,
            "running": False,
            "detail": (result.stderr or "").strip() or "컨테이너를 찾을 수 없습니다.",
        }
    state = json.loads(result.stdout)
    return {
        "found": True,
        "running": bool(state.get("Running")),
        "status": state.get("Status"),
        "started_at": state.get("StartedAt"),
        "finished_at": None if state.get("Running") else state.get("FinishedAt"),
        "exit_code": state.get("ExitCode"),
        "restarting": bool(state.get("Restarting")),
    }


# 컨테이너 env를 통째로 넘기지 않는다 - 지금은 자격증명이 없어도, 나중에 누가
# 브로커 키를 이 컨테이너 env에 추가하면 그대로 새게 된다. 화면에 보여줄
# 값만 이름으로 허용목록에 올린다(차단목록이 아니라 허용목록인 이유).
_ENV_DISPLAY_ALLOWLIST = ("PAPER_NOTIONAL_KRW", "BROKER")


def container_settings(name: str = STRATEGY_CONTAINER_NAME) -> dict[str, str]:
    """허용목록에 있는 env 값만 문자열로 돌려준다. 비밀값은 절대 포함하지 않는다."""
    result = _docker("inspect", "--format", "{{json .Config.Env}}", name)
    if result.returncode != 0:
        return {}
    try:
        raw_env: list[str] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    settings: dict[str, str] = {}
    for entry in raw_env:
        key, _, value = entry.partition("=")
        if key in _ENV_DISPLAY_ALLOWLIST:
            settings[key] = value
    return settings


def set_power(action: PowerAction, name: str = STRATEGY_CONTAINER_NAME) -> dict[str, Any]:
    """`docker start`/`stop` 그 컨테이너 하나만.

    이미 그 상태여도(예: 꺼진 것을 또 끔) docker는 조용히 성공을 돌려주므로,
    별도 idempotency key 없이도 재시도가 안전하다.
    """
    if action not in ("start", "stop"):
        raise ValueError(f"알 수 없는 action: {action!r}")
    if not STRATEGY_CONTAINER_CONTROL_ENABLED:
        raise StrategyRuntimeError("전략 컨테이너 전원 조작이 이 배포에서 꺼져 있습니다.")
    result = _docker(action, name)
    if result.returncode != 0:
        raise StrategyRuntimeError((result.stderr or "").strip() or f"docker {action} 실패")
    return container_status(name)


def _latest_file(pattern: str) -> Path | None:
    if not MLPIPE_HOME.is_dir():
        return None
    matches = sorted(MLPIPE_HOME.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def latest_ledger() -> dict[str, Any] | None:
    """가장 최근에 갱신된 실시간 원장(체결·미체결·집계). 없으면 None."""
    path = _latest_file("ledgers/live-*.json")
    if path is None:
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    data["_source_file"] = path.name
    return data


def latest_pack() -> tuple[dict[str, Any], Path] | None:
    """가장 최근 팩(모델·피처 구성 메타데이터). 모델 바이너리는 포함하지 않는다."""
    path = _latest_file("packs/*/pack.json")
    if path is None:
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data, path.parent


@lru_cache(maxsize=4)
def _model_head(path_str: str, mtime_ns: int) -> dict[str, Any]:
    """XGBoost 모델 JSON에서 사람이 읽을 만한 머리말만 뽑는다.

    파일이 1.9MB짜리라 트리 전체를 반환하지 않는다. `mtime_ns`를 캐시 키에
    넣어 팩이 갱신되기 전까지는 다시 파싱하지 않는다(패널이 주기적으로
    폴링해도 매번 수 MB JSON을 다시 읽지 않도록).
    """
    with open(path_str, encoding="utf-8") as handle:
        model = json.load(handle)
    learner = model.get("learner", {})
    gbtree = learner.get("gradient_booster", {}).get("model", {})
    return {
        "objective": learner.get("objective", {}).get("name"),
        "num_feature": learner.get("learner_model_param", {}).get("num_feature"),
        "num_trees": gbtree.get("gbtree_model_param", {}).get("num_trees"),
        "base_score": learner.get("learner_model_param", {}).get("base_score"),
    }


def model_summary(pack_dir: Path) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for side in ("long", "short"):
        path = pack_dir / f"model-{side}.json"
        if path.exists():
            stat = path.stat()
            summary[side] = _model_head(str(path), stat.st_mtime_ns)
    return summary


def strategy_snapshot(name: str = STRATEGY_CONTAINER_NAME) -> dict[str, Any]:
    """패널 하나가 필요로 하는 값을 한 번에 묶는다. 전부 읽기 전용."""
    ledger = latest_ledger()
    pack_result = latest_pack()
    pack, models = None, {}
    if pack_result is not None:
        pack, pack_dir = pack_result
        models = model_summary(pack_dir)

    return {
        "container_name": name,
        "container": container_status(name),
        "settings": container_settings(name),
        "control_enabled": STRATEGY_CONTAINER_CONTROL_ENABLED,
        "ledger": ledger,
        "pack": pack,
        "models": models,
    }


# ── 자체 점검 (docker·파일 없이도 돈다) ────────────────────────────────────────

def _check_unknown_action_rejected() -> None:
    try:
        set_power("pause")  # type: ignore[arg-type]
    except ValueError:
        pass
    else:
        raise AssertionError("알 수 없는 action이 조용히 통과했다")


def _check_control_disabled_by_default() -> None:
    # 이 파일을 import할 때 환경변수를 건드리지 않았다면 기본은 꺼짐이다.
    if os.getenv("ENABLE_STRATEGY_CONTAINER_CONTROL", "false").casefold() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        assert STRATEGY_CONTAINER_CONTROL_ENABLED is False, "기본값이 켜져 있다"


def _check_missing_container_is_not_masked_as_off() -> None:
    # docker가 아예 없을 때도(이 실행 환경) found=False로 정직하게 떨어져야 한다.
    status = container_status("definitely-not-a-real-container-name")
    assert status["found"] is False
    assert status["running"] is False


if __name__ == "__main__":
    _check_unknown_action_rejected()
    print("알 수 없는 action 거부           OK")
    _check_control_disabled_by_default()
    print("전원 조작 기본값 = 꺼짐          OK")
    _check_missing_container_is_not_masked_as_off()
    print("없는 컨테이너를 OFF로 위장하지 않음  OK")
    print("strategy_runtime 자체 점검 통과")
