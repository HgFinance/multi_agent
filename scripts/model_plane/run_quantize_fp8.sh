#!/usr/bin/env bash
# FP8 양자화를 vLLM 컨테이너 안에서 돌린다 (호스트 파이썬 오염 없음).
#
# 소유: 재일 (Model Plane)
# 근거: quantize_fp8.py 머리말 참고. 기본은 1.5B 스모크 - 양자화 파이프라인
#       (BF16 로드 → FP8_DYNAMIC → 저장 → manifest → 서빙 가능)을 AWS 에서
#       검증하는 용도다. 14B 는 사전 양자화 체크포인트를 쓴다.
#
# 쓰기:
#   scripts/model_plane/run_quantize_fp8.sh                          # 1.5B 스모크
#   scripts/model_plane/run_quantize_fp8.sh Qwen/Qwen2.5-7B-Instruct # 다른 모델
#   QUANTIZE_GPU=0 scripts/model_plane/run_quantize_fp8.sh           # CPU 강제
set -euo pipefail

SRC_MODEL="${1:-Qwen/Qwen2.5-1.5B-Instruct}"
OUT_DIRNAME="${2:-$(basename "$SRC_MODEL")-FP8-dynamic}"
DEST_ROOT="${HGF_MODEL_DIR:-/opt/hgfinance/models}"
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:latest}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GPU_ARGS=(--gpus all)
if [ "${QUANTIZE_GPU:-1}" = "0" ]; then GPU_ARGS=(); fi

echo "== ${SRC_MODEL} -> ${DEST_ROOT}/${OUT_DIRNAME} (FP8_DYNAMIC)"
sudo mkdir -p "$DEST_ROOT"
sudo chown -R "$(id -u):$(id -g)" "$DEST_ROOT"

docker run --rm "${GPU_ARGS[@]}" \
  -v "$DEST_ROOT:/models" \
  -v "$SCRIPT_DIR:/work:ro" \
  -e HF_HOME=/models/hf-cache \
  ${HF_TOKEN:+-e HF_TOKEN="$HF_TOKEN"} \
  --entrypoint bash \
  "$VLLM_IMAGE" \
  -c "pip install --no-cache-dir -q llmcompressor && \
      python3 /work/quantize_fp8.py --model '$SRC_MODEL' --output-dir '/models/$OUT_DIRNAME'"

# 컨테이너가 root 로 썼다 - 호스트 소유로 되돌린다
sudo chown -R "$(id -u):$(id -g)" "$DEST_ROOT/$OUT_DIRNAME"

python3 "$SCRIPT_DIR/model_manifest.py" --model-dir "$DEST_ROOT/$OUT_DIRNAME" --write

cat <<EOF

양자화 아티팩트 검증(서빙까지 해봐야 완료다):
  WORKER_BASE_MODEL_DIRNAME=$OUT_DIRNAME \\
  WORKER_MODEL_NAME=$(echo "$OUT_DIRNAME" | tr '[:upper:]' '[:lower:]') \\
  docker compose -f docker-compose.yml -f docker-compose.model.yml up -d vllm
  curl -s http://127.0.0.1:8000/v1/models
S3 업로드(정본화):
  aws s3 sync "$DEST_ROOT/$OUT_DIRNAME" "s3://\${HGF_MODEL_BUCKET}/models/$OUT_DIRNAME"
EOF
