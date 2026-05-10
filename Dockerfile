# Multi-stage build for the agent platform.
#   Stage 1 (frontend-build): node:20 builds the React/Vite dashboard.
#   Stage 2 (runtime):        python:3.13-slim runs uvicorn and serves
#                             the compiled dashboard as static files.
#
# Local dev still uses `npm run dev` (vite + uvicorn --reload) — this image
# is the production runtime only.

# ---------------------------------------------------------------------------
# Stage 1 — build the frontend bundle
# ---------------------------------------------------------------------------
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend

# Cache npm install layer separately from source so we don't reinstall
# every time a .tsx file changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/tsconfig.json frontend/vite.config.ts frontend/index.html ./
COPY frontend/src ./src

RUN npm run build
# Produces /app/frontend/dist


# ---------------------------------------------------------------------------
# Stage 2 — runtime image
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

# Build-time tooling for native deps (chromadb / numpy / pgvector clients).
# Removed at the end of the install layer to keep the image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps. Cache layer keyed on requirements.txt so application
# code changes don't force a reinstall.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Application source.
COPY app ./app
COPY mcp_server ./mcp_server
COPY scripts ./scripts
COPY targets ./targets
COPY config ./config
COPY pyproject.toml ./

# Compiled dashboard from Stage 1 (served at "/" by FastAPI's StaticFiles).
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Strip the build toolchain to keep the image lean.
RUN apt-get purge -y --auto-remove build-essential gcc \
    && rm -rf /root/.cache

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    HARNESS_DOCS_PATH=targets/target-app

# Healthcheck — ALB target group hits /health every N seconds.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

EXPOSE 8000

# Single-process uvicorn. ECS handles multi-task fan-out via task count;
# putting workers > 1 inside a 1 vCPU task wastes memory.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
