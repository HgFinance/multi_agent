FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/apps/api \
    HERMES_HOME=/opt/hermes-cli

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /opt/hermes-cli

# The BFF needs the official Hermes CLI for the Kanban create boundary. It
# does not run the Hermes gateway; CEO chat is sent to the existing ceo-hermes
# API server. Pin the source revision so a rebuild cannot silently change the
# CLI/runtime contract. v2026.8.3 is the published Hermes v0.20.0 release
# (commit 3c27eb6234bf91b8ceee9e9071591b31e9b148cb).
ARG HERMES_AGENT_REF=v2026.8.3

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /tmp/hermes-agent \
    && git -C /tmp/hermes-agent fetch --depth 1 origin "${HERMES_AGENT_REF}" \
    && git -C /tmp/hermes-agent checkout --detach FETCH_HEAD \
    && pip install --no-cache-dir /tmp/hermes-agent \
    && command -v hermes \
    && hermes --version \
    && hermes kanban --help >/dev/null \
    && rm -rf /tmp/hermes-agent /root/.cache/pip /var/lib/apt/lists/*

COPY . .

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
