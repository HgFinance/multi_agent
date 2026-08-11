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
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-installer.sh \
    && UV_UNMANAGED_INSTALL=/usr/local/bin sh /tmp/uv-installer.sh \
    && git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent \
    && git -C /opt/hermes-agent fetch --depth 1 origin "${HERMES_AGENT_REF}" \
    && git -C /opt/hermes-agent checkout --detach FETCH_HEAD \
    && UV_NO_CONFIG=1 uv pip install --system --no-cache -e "/opt/hermes-agent[all]" \
    && command -v hermes \
    && test -x /usr/local/bin/hermes \
    && test "$(command -v hermes)" = /usr/local/bin/hermes \
    && hermes --version \
    && hermes kanban --help >/dev/null \
    && rm -rf /tmp/uv-installer.sh /root/.cache/uv /var/lib/apt/lists/*

COPY . .

# Keep this check after the application copy so it validates the final image
# state, including the editable Hermes source retained at /opt/hermes-agent.
RUN python -c 'import hermes_cli; print(hermes_cli, getattr(hermes_cli, "__file__", None), getattr(hermes_cli, "__path__", None))' \
    && python -c 'import sys; print("\n".join(sys.path))' \
    && test -f /opt/hermes-agent/hermes_cli/main.py \
    && python -c 'import hermes_cli.main; print(hermes_cli.main.__file__)' \
    && test "$(python -c 'import hermes_cli.main; print(hermes_cli.main.__file__)')" = /opt/hermes-agent/hermes_cli/main.py \
    && command -v hermes \
    && test -x /usr/local/bin/hermes \
    && test "$(command -v hermes)" = /usr/local/bin/hermes \
    && hermes --version \
    && hermes kanban --help >/dev/null

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
