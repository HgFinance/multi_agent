#!/usr/bin/env bash
# HF → EBS 모델 다운로드 (최초 1회) + manifest 기록.
#
# 소유: 재일 (Model Plane)
# 근거: FINAL_RUNTIME_ARCHITECTURE.md §8.3-8.4. 정상 경로는 S3 → EBS 지만
#       S3 에 아직 아무것도 없으므로 최초 1회는 HF 에서 받는다. 받은 뒤
#       S3 로 올려 두면(아래 안내 출력) 다음부터는 S3 가 정본이다.
#
# ▶ 호스트 파이썬을 더럽히지 않는다 - vLLM 이미지 안의 huggingface_hub 로
#   받는다(어차피 서빙에 쓸 이미지라 추가 풀 비용이 없다).
#
# 쓰기:
#   scripts/model_plane/fetch_base_model.sh                       # 기본 14B FP8
#   scripts/model_plane/fetch_base_model.sh Qwen/Qwen2.5-1.5B-Instruct  # 스모크용
set -euo pipefail

MODEL_REPO="${1:-RedHatAI/Qwen2.5-14B-Instruct-FP8-dynamic}"
DEST_ROOT="${HGF_MODEL_DIR:-/opt/hgfinance/models}"
DIRNAME="${2:-$(basename "$MODEL_REPO")}"
DEST="$DEST_ROOT/$DIRNAME"
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:latest}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== ${MODEL_REPO} -> ${DEST}"
sudo mkdir -p "$DEST_ROOT"
sudo chown -R "$(id -u):$(id -g)" "$DEST_ROOT"
mkdir -p "$DEST"

# vLLM 이미지의 entrypoint 는 API 서버라 python3 로 바꿔 받는다.
# HF_TOKEN 은 gated repo 일 때만 필요하다(Qwen/RedHatAI 는 공개).
docker run --rm \
  -v "$DEST_ROOT:/models" \
  -e HF_HOME=/models/hf-cache \
  ${HF_TOKEN:+-e HF_TOKEN="$HF_TOKEN"} \
  --entrypoint python3 \
  "$VLLM_IMAGE" \
  -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='$MODEL_REPO', local_dir='/models/$DIRNAME')"

python3 "$SCRIPT_DIR/model_manifest.py" --model-dir "$DEST" --write

cat <<EOF

다음 단계
  1) S3 를 정본으로 만들어 둔다 (버킷이 있으면):
       aws s3 sync "$DEST" "s3://\${HGF_MODEL_BUCKET}/models/$DIRNAME" \\
         --exclude 'hf-cache/*'
  2) 서빙 (repo 루트에서):
       docker compose -f docker-compose.yml -f docker-compose.model.yml up -d vllm
  3) 다른 EC2 에서 EBS 를 다시 만들 때는 HF 가 아니라 S3 에서 받는다:
       aws s3 sync "s3://\${HGF_MODEL_BUCKET}/models/$DIRNAME" "$DEST"
       python3 scripts/model_plane/model_manifest.py --model-dir "$DEST" --verify
EOF
