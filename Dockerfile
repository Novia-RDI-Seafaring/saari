# saari — hosted image. See docs/hosting.md for deployment (Azure Container
# Apps + Entra ID Easy Auth) and README.md "Hosted mode" for the header
# contract.
#
# One image, two roles (pick the command):
#   HTTP API + web UI (default):  saari serve --host 0.0.0.0 --no-open
#   MCP streamable HTTP:          saari-mcp --http --host 0.0.0.0

# ---- stage 1: build the SPA -------------------------------------------------
FROM node:22-slim AS ui
WORKDIR /build
RUN corepack enable
COPY ui/package.json ui/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY ui/ ./
RUN pnpm build

# ---- stage 2: python runtime ------------------------------------------------
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
# Bundle the built SPA where the packaged server looks for it.
COPY --from=ui /build/dist src/saari/_ui

RUN uv sync --frozen --no-dev --extra serve --no-editable \
    && rm -rf /root/.cache

ENV PATH="/app/.venv/bin:$PATH" \
    # Per-user projects live here; mount an Azure Files share (or volume) at /data.
    SAARI_DATA_ROOT=/data \
    # Cache the fastembed model on the share so it downloads once, not per replica.
    FASTEMBED_CACHE_PATH=/data/_cache/fastembed

VOLUME /data
EXPOSE 3044

# Container Apps health probe target: GET /api/health (exempt from identity).
CMD ["saari", "serve", "--host", "0.0.0.0", "--port", "3044", "--no-open"]
