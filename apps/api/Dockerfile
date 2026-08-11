FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/apps/api \
    HERMES_HOME=/opt/hermes-cli

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# The BFF needs the official Hermes CLI for the Kanban create boundary. It
# does not run the Hermes gateway; CEO chat is sent to the existing ceo-hermes
# API server. Pin the source revision so a rebuild cannot silently change the
# CLI/runtime contract.
ARG HERMES_AGENT_REF=33f8e96a72945afb29f3bc9ef9991940f0bedcf

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /tmp/hermes-agent \
    && git -C /tmp/hermes-agent fetch --depth 1 origin "${HERMES_AGENT_REF}" \
    && git -C /tmp/hermes-agent checkout --detach FETCH_HEAD \
    && pip install --no-cache-dir /tmp/hermes-agent \
    && rm -rf /tmp/hermes-agent /root/.cache/pip /var/lib/apt/lists/*

RUN mkdir -p /opt/hermes-cli

COPY . .

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
