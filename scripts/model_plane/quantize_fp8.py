#!/usr/bin/env python3
"""FP8_DYNAMIC 양자화 - BF16 체크포인트를 FP8 아티팩트로 만든다.

소유: 재일 (Model Plane)
근거: FINAL_RUNTIME_ARCHITECTURE.md §12 Phase 4 "Qwen2.5-14B FP8".
      llm-compressor 의 FP8_DYNAMIC 은 캘리브레이션 데이터가 필요 없다 -
      weights 는 정적 per-channel FP8, activations 는 실행 시 per-token 동적.
      lm_head 는 BF16 으로 남긴다(RedHatAI 공식 체크포인트와 같은 레시피).

▶ 이 스크립트가 도는 곳
  run_quantize_fp8.sh 가 vLLM 컨테이너 안(pip install llmcompressor 후)에서
  돌린다. 호스트 파이썬에는 torch 를 깔지 않는다.

▶ 크기 주의
  기본 모델은 Qwen2.5-1.5B-Instruct 다 - **양자화 파이프라인 검증용**이다.
  14B 는 BF16 로드에만 ~28GB 가 필요해서 g6.xlarge(RAM 16GB + VRAM 24GB)
  에서는 오프로딩으로도 빠듯하다. 14B 서빙은 사전 양자화 체크포인트
  (RedHatAI/Qwen2.5-14B-Instruct-FP8-dynamic, fetch_base_model.sh)를 쓰고,
  14B 를 직접 양자화하려면 RAM 64GB+ 인스턴스에서 이 스크립트를 돌린다.
  같은 레시피이므로 파이프라인 검증은 작은 모델로 충분하다.

쓰기(컨테이너 안): python3 quantize_fp8.py --model Qwen/Qwen2.5-1.5B-Instruct \
                     --output-dir /models/Qwen2.5-1.5B-Instruct-FP8-dynamic
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

RECIPE_VERSION = "fp8-dynamic-v1"


def _oneshot():
    """llm-compressor 버전에 따라 oneshot 위치가 다르다 - 둘 다 받는다."""
    try:
        from llmcompressor import oneshot
        return oneshot
    except ImportError:
        from llmcompressor.transformers import oneshot  # 구판
        return oneshot


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct",
                    help="HF repo id 또는 로컬 BF16 체크포인트 경로")
    ap.add_argument("--output-dir", required=True)
    args = ap.parse_args()

    from transformers import AutoModelForCausalLM, AutoTokenizer
    from llmcompressor.modifiers.quantization import QuantizationModifier

    print(f"== load: {args.model}", flush=True)
    model = AutoModelForCausalLM.from_pretrained(
        args.model, torch_dtype="auto", device_map="auto")
    tokenizer = AutoTokenizer.from_pretrained(args.model)

    print("== quantize: FP8_DYNAMIC (lm_head 제외)", flush=True)
    recipe = QuantizationModifier(
        targets="Linear", scheme="FP8_DYNAMIC", ignore=["lm_head"])
    _oneshot()(model=model, recipe=recipe)

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    print(f"== save: {out}", flush=True)
    model.save_pretrained(str(out))
    tokenizer.save_pretrained(str(out))

    # 양자화 계보 - 어떤 소스에서 어떤 레시피로 만들었는지를 아티팩트에 남긴다.
    # (OLLAMA_DEPARTMENT_MODELFILE_GUIDE §9 재현성 기록의 축소판)
    (out / "quantization_record.json").write_text(json.dumps({
        "recipe_version": RECIPE_VERSION,
        "source_model": args.model,
        "scheme": "FP8_DYNAMIC",
        "ignored_modules": ["lm_head"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool": "llm-compressor",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("== 완료. manifest 는 호출측(run_quantize_fp8.sh)이 기록한다")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
