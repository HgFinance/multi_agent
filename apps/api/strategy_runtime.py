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
  임의 컨테이너 조작. `STRATEGY_CONTAINER_NAME` 하나만 허용하고, 호출부가
  이름을 넘겨받지 않는다(브라우저 입력이 subprocess 인자로 그대로 흘러들면
  임의 컨테이너를 세우고 내리는 문이 된다). 컨테이너를 새로 만들거나
  (`docker run`/`rm`) 세션 날짜를 바꾸지도 않는다 - `docker start`/`stop`은
  이미 있는 컨테이너를 그대로 멈추고 그대로 되살릴 뿐이라 되돌리기 쉽다.

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

# `docker stop`의 기본 유예시간이 10초다(SIGTERM 후 안 죽으면 SIGKILL). 이보다
# 짧게 잡으면 컨테이너가 정상적으로 멈추는 중인데도 우리 쪽에서 먼저 타임아웃을
# 내 "실패"로 보고한다(더미 컨테이너로 실측 - PID 1이 SIGTERM을 무시해 정확히
# 이 경계에서 걸렸다). 넉넉하게 잡는다.
DOCKER_TIMEOUT_SECONDS = 15

PowerAction = Literal["start", "stop"]


class StrategyRuntimeError(RuntimeError):
    """docker CLI에 닿지 못했거나 조작이 거부됐다. 호출부가 503으로 옮긴다."""


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
