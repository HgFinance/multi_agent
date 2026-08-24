#!/usr/bin/env python3
"""Worker Model Gateway - 직원 Worker 가 부를 모델의 단일 해석 지점.

소유: 재일 (리서치본부 첫 적용, 계약은 부서 공용)
근거: docs/02-engineering/FINAL_RUNTIME_ARCHITECTURE.md §7.2 (Worker Model
      Gateway 와 Multi-LoRA), §12 Phase 4. TECH_STACK_DECISIONS.md:163
      "Worker Graph 는 직접 boto3 나 Ollama URL 을 호출하지 않고 Gateway 를
      주입한다".

▶ 왜 이 모듈이 필요한가
  지금까지 Worker 의 모델 호출은 employee_worker_runtime.default_worker_llm
  (Ollama 전용, openai 패키지 필요, timeout 기본 8초)에 박혀 있었다.
  AWS 의 Worker 모델은 vLLM 위 Qwen2.5-14B AWQ v1 이고, 부서별 LoRA 가 붙으면
  worker_id → adapter 해석이 필요해진다. 그 해석을 Worker 나 부서 코드가
  하지 않고 **여기서만** 한다 - Worker 는 adapter_id 를 고르지 않는다(§7.2).

▶ 표준 라이브러리만 쓴다
  research-mcp 이미지에는 openai 패키지가 없다. 호출 모양은 이미 검증된
  departments/01-research/evidence/llm_client.py(urllib) 를 따른다.
  observability(record_llm_call)는 있으면 쓰고 없으면 조용히 넘어간다 -
  게이트웨이가 계측 모듈 부재로 죽으면 안 된다.

▶ 해석 규칙 (환경변수 계약)
  WORKER_MODEL_BASE_URL 이 설정돼 있으면 → vLLM(OpenAI 호환) 경로:
    WORKER_MODEL_NAME             (기본 qwen2.5-14b-instruct-awq)
    WORKER_MODEL_API_KEY          (기본 vllm - vLLM 은 키를 검사하지 않지만
                                   OpenAI 호환 헤더 형식은 지킨다)
    WORKER_MODEL_TIMEOUT_SECONDS  (기본 120 - 14B 는 8초 안에 못 끝난다.
                                   Ollama 기본 8초를 그대로 쓰면 전멸한다)
  설정돼 있지 않으면 → 기존 DEV Ollama 경로 그대로:
    OLLAMA_BASE_URL (기본 http://127.0.0.1:11434/v1)
    OLLAMA_CHAT_MODEL (기본 qwen3:1.7b), OLLAMA_TIMEOUT_SECONDS (기본 8)
  즉 이 모듈을 끼워도 DEV 동작은 바뀌지 않는다.

▶ Worker Registry (worker_id → adapter)
  WORKER_MODEL_REGISTRY_PATH 의 JSON(worker-model-registry.v1)이 워커별
  adapter 를 결정한다. adapter 는 status 가 "enabled" 일 때만 적용되고,
  그 외에는 base model 로 서빙한다. 현재 Qwen AWQ v1은 전사 LLM Worker의
  base model이며 arithmetic adapter는 명시적 route 전용이다. 파일이 없으면
  전원 base model 이다(조용한 실패가 아니라 **의도된 기본값**이다).

실행: python departments/worker_model_gateway.py   # 자체 점검 (네트워크 없음)
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

MODULE_VERSION = "worker-model-gateway-v1"
REGISTRY_VERSION = "worker-model-registry.v1"

DEFAULT_VLLM_MODEL = "qwen2.5-14b-instruct-awq"
DEFAULT_VLLM_TIMEOUT = 120.0
DEFAULT_OLLAMA_BASE = "http://127.0.0.1:11434/v1"
DEFAULT_OLLAMA_MODEL = "qwen3:1.7b"
DEFAULT_OLLAMA_TIMEOUT = 8.0

WorkerLLM = Callable[..., str]


@dataclass(frozen=True)
class ModelBinding:
    """Worker 하나가 실제로 부를 모델의 확정 좌표.

    envelope 의 model_version/adapter_version(orchestration/contracts/mas.py)
    에 그대로 실을 수 있도록 필드 이름을 계약과 맞춘다.
    """

    provider: str            # "vllm-openai" | "ollama"
    base_url: str            # .../v1 로 끝나는 OpenAI 호환 루트
    model: str               # 이번 호출에 보낼 model 필드 (adapter 명 포함 가능)
    base_model: str          # adapter 를 걷어낸 base 서빙 이름
    adapter_id: str | None   # 적용된 LoRA adapter (없으면 None)
    adapter_version: str     # 계약 필수 필드 - 없으면 "none"
    api_key: str
    timeout_seconds: float

    def as_metadata(self) -> dict:
        """job 결과·telemetry 에 싣는 비밀 없는 요약."""
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model_version": self.model,
            "base_model": self.base_model,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "timeout_seconds": self.timeout_seconds,
        }


def _normalize_v1(raw: str) -> str:
    base = (raw or "").strip().rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def load_registry(path: str | os.PathLike | None) -> dict:
    """registry JSON 을 읽는다. 없거나 깨졌으면 빈 registry (전원 base model).

    깨진 파일을 조용히 무시하지 않는다 - stderr 로 남기고 빈 registry 를
    돌려준다. 게이트웨이가 registry 문법 오류로 Worker 전체를 죽이는 것보다
    base model 로 도는 쪽이 안전하다(어느 쪽이든 기록은 남는다).
    """
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    # ValueError 로 넓게 잡는다 - JSONDecodeError 뿐 아니라 UnicodeDecodeError
    # (Windows 편집기가 CP949 로 저장한 경우, 실측 리뷰에서 재현)도 여기 속한다.
    # registry 하나 때문에 워커 면 전체가 죽는 것보다 base model 로 도는 쪽이
    # 안전하다는 계약을 예외 타입 하나가 뚫으면 안 된다.
    except (OSError, ValueError) as e:
        import sys
        print(f"⚠ worker model registry 를 읽지 못했다({p}): "
              f"{type(e).__name__}: {e} - 전원 base model 로 동작한다",
              file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _adapter_for(registry: Mapping, worker_id: str | None) -> tuple[str | None, str]:
    """registry 에서 (adapter_id, adapter_version) 을 해석한다.

    adapter 는 status == "enabled" 일 때만 붙는다. registered/implemented
    상태의 adapter 를 서빙에 쓰면 Worker Registry 의 상태 구분
    (FINAL_RUNTIME_ARCHITECTURE §14)이 무의미해진다.
    """
    if not worker_id:
        return None, "none"
    workers = registry.get("workers")
    entry = workers.get(worker_id) if isinstance(workers, Mapping) else None
    if not isinstance(entry, Mapping):
        # 항목이 dict 가 아니면(손편집 실수) adapter 없음으로 degrade 한다
        return None, "none"
    adapter_id = entry.get("adapter_id")
    if adapter_id and entry.get("status") == "enabled":
        return str(adapter_id), str(entry.get("adapter_version") or "unknown")
    return None, "none"


def _float_env(raw: str | None, default: float) -> float:
    """timeout 류 env 를 관대하게 읽는다 - 오타가 워커 면 전체를 죽이면 안 된다."""
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        import sys
        print(f"⚠ 숫자가 아닌 timeout 값 {raw!r} - 기본값 {default} 을 쓴다",
              file=sys.stderr)
        return default


def resolve(worker_id: str | None = None, *,
            env: Mapping[str, str] | None = None,
            registry_path: str | None = None) -> ModelBinding:
    """환경변수 + registry 로 이번 호출의 모델 좌표를 확정한다."""
    e = os.environ if env is None else env
    reg_path = registry_path or e.get("WORKER_MODEL_REGISTRY_PATH") or ""
    registry = load_registry(reg_path)
    adapter_id, adapter_version = _adapter_for(registry, worker_id)

    vllm_base = (e.get("WORKER_MODEL_BASE_URL") or "").strip()
    if vllm_base:
        base_model = (e.get("WORKER_MODEL_NAME") or "").strip() or DEFAULT_VLLM_MODEL
        return ModelBinding(
            provider="vllm-openai",
            base_url=_normalize_v1(vllm_base),
            # vLLM 은 --lora-modules 로 등록된 adapter 를 model 필드의
            # 이름으로 고른다 - adapter 가 켜져 있으면 그 이름을 보낸다.
            model=adapter_id or base_model,
            base_model=base_model,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            api_key=(e.get("WORKER_MODEL_API_KEY") or "vllm").strip() or "vllm",
            timeout_seconds=_float_env(e.get("WORKER_MODEL_TIMEOUT_SECONDS"),
                                       DEFAULT_VLLM_TIMEOUT),
        )

    # DEV/TEST fallback - 기존 Ollama 경로와 같은 기본값.
    base_model = (e.get("OLLAMA_CHAT_MODEL") or "").strip() or DEFAULT_OLLAMA_MODEL
    return ModelBinding(
        provider="ollama",
        base_url=_normalize_v1(e.get("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_BASE),
        model=adapter_id or base_model,
        base_model=base_model,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        api_key=(e.get("OLLAMA_API_KEY") or "ollama").strip() or "ollama",
        timeout_seconds=_float_env(e.get("OLLAMA_TIMEOUT_SECONDS"),
                                   DEFAULT_OLLAMA_TIMEOUT),
    )


def _record_llm_call(**kwargs) -> None:
    """orchestration.llm_observability 가 있으면 계측하고 없으면 무시한다."""
    try:
        from orchestration.llm_observability import record_llm_call
    except Exception:  # noqa: BLE001 - 계측 부재가 호출을 죽이면 안 된다
        return
    try:
        record_llm_call(**kwargs)
    except Exception:  # noqa: BLE001
        return


class _UsageView:
    """record_llm_call 이 기대하는 usage 객체 모양(속성 접근)으로 감싼다."""

    def __init__(self, usage: Mapping | None):
        u = usage or {}
        self.prompt_tokens = u.get("prompt_tokens")
        self.completion_tokens = u.get("completion_tokens")


def worker_llm(binding: ModelBinding) -> WorkerLLM:
    """binding 하나로 (system, prompt) -> str 호출자를 만든다.

    employee_worker_runtime.WorkerLLM 계약과 같다. temperature 0 -
    Worker 출력은 계약 검증 대상이라 창의성이 아니라 재현성이 필요하다.
    실패는 예외로 올린다 - Worker 그래프의 재시도(max_attempts)가 잡는다.
    """

    def call(system: str, prompt: str, *,
             json_schema: Mapping | None = None) -> str:
        payload = {
            "model": binding.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": prompt}],
        }
        if json_schema is not None:
            # vLLM and Ollama both implement the OpenAI-compatible JSON Schema
            # response_format.  This constrains decoding; Pydantic validation at
            # the Worker boundary still fails closed on semantic/context errors.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "worker_context", "schema": json_schema},
            }
        req = urllib.request.Request(
            binding.base_url + "/chat/completions", method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {binding.api_key}"},
            data=json.dumps(payload).encode())
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=binding.timeout_seconds) as r:
                out = json.loads(r.read())
        except Exception:
            _record_llm_call(
                latency_ms=int((time.perf_counter() - started) * 1000), error=True)
            raise
        _record_llm_call(
            usage=_UsageView(out.get("usage")),
            latency_ms=int((time.perf_counter() - started) * 1000))
        return str(out["choices"][0]["message"]["content"] or "")

    call._json_schema_capable = True  # type: ignore[attr-defined]
    return call


def llm_for_worker(worker_id: str | None = None, *,
                   env: Mapping[str, str] | None = None,
                   registry_path: str | None = None) -> tuple[WorkerLLM, ModelBinding]:
    """편의 함수 - binding 해석과 호출자 생성을 한 번에."""
    binding = resolve(worker_id, env=env, registry_path=registry_path)
    return worker_llm(binding), binding


def enabled_adapters(*, env: Mapping[str, str] | None = None,
                     registry_path: str | None = None) -> dict[str, str]:
    """registry 에 enabled 로 켜진 worker→adapter_id 맵 (진단용).

    health 검사가 이걸 서빙 목록과 대조한다 - registry 는 켰는데 vLLM 에
    adapter 가 없으면 그 불일치가 여기서 표면화된다.
    """
    e = os.environ if env is None else env
    reg = load_registry(registry_path or e.get("WORKER_MODEL_REGISTRY_PATH") or "")
    out: dict[str, str] = {}
    workers = reg.get("workers")
    if not isinstance(workers, Mapping):
        return out
    for worker_id in workers:
        adapter_id, _ = _adapter_for(reg, str(worker_id))
        if adapter_id:
            out[str(worker_id)] = adapter_id
    return out


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음
# ---------------------------------------------------------------------------

def _check_env_resolution():
    b = resolve(env={"WORKER_MODEL_BASE_URL": "http://vllm:8000"})
    assert b.provider == "vllm-openai" and b.base_url == "http://vllm:8000/v1"
    assert b.model == DEFAULT_VLLM_MODEL and b.adapter_version == "none"
    # 14B 는 8초 안에 못 끝난다 - 기본 timeout 이 120 이어야 한다
    assert b.timeout_seconds == 120.0, b.timeout_seconds

    b = resolve(env={"WORKER_MODEL_BASE_URL": "http://vllm:8000/v1/",
                     "WORKER_MODEL_NAME": "m-x",
                     "WORKER_MODEL_TIMEOUT_SECONDS": "45"})
    assert b.base_url == "http://vllm:8000/v1" and b.model == "m-x"
    assert b.timeout_seconds == 45.0

    # vLLM env 가 없으면 DEV Ollama 기본값 그대로 - 이 모듈을 끼워도
    # 로컬 동작이 바뀌지 않는다는 계약이다
    b = resolve(env={})
    assert b.provider == "ollama" and b.base_url == DEFAULT_OLLAMA_BASE
    assert b.model == DEFAULT_OLLAMA_MODEL and b.timeout_seconds == 8.0
    b = resolve(env={"OLLAMA_BASE_URL": "http://host.docker.internal:11434",
                     "OLLAMA_CHAT_MODEL": "qwen3:8b"})
    assert b.base_url == "http://host.docker.internal:11434/v1"
    assert b.model == "qwen3:8b"
    print("  env 해석(vllm/ollama)    OK")


def _check_registry(tmp: Path):
    reg = tmp / "registry.json"
    reg.write_text(json.dumps({
        "registry_version": REGISTRY_VERSION,
        "workers": {
            "with-adapter": {"adapter_id": "research-lora", "adapter_version": "v3",
                             "status": "enabled"},
            "adapter-pending": {"adapter_id": "research-lora", "adapter_version": "v3",
                                "status": "implemented"},
            "no-adapter": {"adapter_id": None, "adapter_version": "none"},
        }}), encoding="utf-8")
    env = {"WORKER_MODEL_BASE_URL": "http://vllm:8000"}

    b = resolve("with-adapter", env=env, registry_path=str(reg))
    assert b.model == "research-lora" and b.adapter_version == "v3"
    assert b.base_model == DEFAULT_VLLM_MODEL

    # enabled 가 아니면 adapter 를 쓰지 않는다 - Registry 상태 구분의 요점
    b = resolve("adapter-pending", env=env, registry_path=str(reg))
    assert b.model == DEFAULT_VLLM_MODEL and b.adapter_version == "none"

    b = resolve("no-adapter", env=env, registry_path=str(reg))
    assert b.model == DEFAULT_VLLM_MODEL and b.adapter_id is None

    # 모르는 워커·registry 없음 → base model (의도된 기본값)
    b = resolve("unknown-worker", env=env, registry_path=str(reg))
    assert b.model == DEFAULT_VLLM_MODEL
    b = resolve("x", env=env, registry_path=str(tmp / "missing.json"))
    assert b.model == DEFAULT_VLLM_MODEL

    # 깨진 파일 → 빈 registry (죽지 않는다)
    bad = tmp / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    assert load_registry(str(bad)) == {}

    # CP949 로 저장된 파일(UnicodeDecodeError)도 degrade 다 - Windows 편집기
    # 실수 하나가 워커 면 전체를 죽이면 안 된다 (2026-08-13 리뷰 재현)
    cp949 = tmp / "cp949.json"
    cp949.write_bytes('{"_comment": "한국어 주석"}'.encode("cp949"))
    assert load_registry(str(cp949)) == {}
    b = resolve("w", env={**env, "WORKER_MODEL_REGISTRY_PATH": str(cp949)})
    assert b.model == DEFAULT_VLLM_MODEL

    # workers 항목이 dict 가 아니어도(손편집 실수) 예외가 아니라 degrade 다
    malformed = tmp / "malformed.json"
    malformed.write_text(json.dumps({"workers": {"w1": "research-lora"}}),
                         encoding="utf-8")
    b = resolve("w1", env=env, registry_path=str(malformed))
    assert b.model == DEFAULT_VLLM_MODEL and b.adapter_id is None
    assert enabled_adapters(env=env, registry_path=str(malformed)) == {}

    # timeout 오타는 기본값으로 산다
    b = resolve(env={"WORKER_MODEL_BASE_URL": "http://vllm:8000",
                     "WORKER_MODEL_TIMEOUT_SECONDS": "12O"})
    assert b.timeout_seconds == 120.0

    # enabled_adapters 는 enabled 만 나열한다
    got = enabled_adapters(env=env, registry_path=str(reg))
    assert got == {"with-adapter": "research-lora"}, got
    print("  registry 해석            OK")


def _check_payload_shape():
    seen: dict = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": '{"summary":"ok"}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }).encode()

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["auth"] = req.headers.get("Authorization")
        seen["body"] = json.loads(req.data)
        return _FakeResp()

    binding = resolve("w", env={"WORKER_MODEL_BASE_URL": "http://vllm:8000",
                                "WORKER_MODEL_NAME": "qwen2.5-14b-instruct-awq"})
    call = worker_llm(binding)
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        out = call("sys-text", "user-text", json_schema=schema)
    finally:
        urllib.request.urlopen = orig

    assert out == '{"summary":"ok"}'
    assert seen["url"] == "http://vllm:8000/v1/chat/completions"
    assert seen["timeout"] == 120.0
    assert seen["auth"] == "Bearer vllm"
    b = seen["body"]
    assert b["model"] == "qwen2.5-14b-instruct-awq"
    assert b["temperature"] == 0, "Worker 는 재현성이 우선이다 - temperature 0"
    assert [m["role"] for m in b["messages"]] == ["system", "user"]
    assert b["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "worker_context", "schema": schema},
    }
    print("  요청 payload 계약        OK")


def _check_metadata_has_no_secrets():
    meta = resolve(env={"WORKER_MODEL_BASE_URL": "http://vllm:8000",
                        "WORKER_MODEL_API_KEY": "s3cret"}).as_metadata()
    flat = json.dumps(meta)
    assert "s3cret" not in flat, "metadata 에 자격이 새면 안 된다"
    assert meta["model_version"] and meta["adapter_version"] == "none"
    print("  metadata 무자격          OK")


if __name__ == "__main__":
    import sys
    import tempfile

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크 없음)")
    _check_env_resolution()
    with tempfile.TemporaryDirectory() as td:
        _check_registry(Path(td))
    _check_payload_shape()
    _check_metadata_has_no_secrets()
    print("Worker Model Gateway 4개 영역 통과.")
