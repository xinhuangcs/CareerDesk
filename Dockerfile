# Build the frontend first, then install the backend into the runtime image.

FROM node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2 AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS api
COPY --from=ghcr.io/astral-sh/uv:0.9.21@sha256:15f68a476b768083505fe1dbfcc998344d0135f0ca1b8465c4760b323904f05a /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 careerdesk \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/careerdesk \
        --shell /usr/sbin/nologin careerdesk
# Preserve the dependency layer when only application code changes.
COPY backend/pyproject.toml backend/uv.lock backend/hatch_build.py ./backend/
RUN uv sync --project backend --locked --no-dev --no-install-project
COPY backend/src/ ./backend/src/
RUN uv sync --project backend --locked --no-dev
COPY --from=frontend /frontend/dist ./frontend/dist
RUN install -d -m 0700 -o 10001 -g 10001 /app/data /app/logs
# Server startup fails closed unless authentication, hosts, origins, and debug settings are safe.
ENV PATH="/app/backend/.venv/bin:$PATH" \
    CAREERDESK_RESOURCE_ROOT="/app" \
    CAREERDESK_CONFIG_FILE="/app/.env" \
    APP_DATA_DIR="/app/data" \
    APP_LOG_DIR="/app/logs" \
    APP_FRONTEND_DIST_DIR="/app/frontend/dist" \
    APP_RUNTIME_MODE="server"
USER 10001:10001

EXPOSE 8000

# Use an allowed Host so the local health probe passes production host validation.
HEALTHCHECK --interval=5s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import http.client, os, sys; host=os.environ.get('APP_ALLOWED_HOSTS', '').split(',', 1)[0].strip(); host or sys.exit(1); connection=http.client.HTTPConnection('127.0.0.1', 8000, timeout=2); connection.request('GET', '/healthz', headers={'Host': host}); response=connection.getresponse(); body=response.read(); connection.close(); sys.exit(0 if response.status == 200 and body == b'ok' else 1)"]

# One worker is required by the data-root lock and turn-recovery contract.
# Docker Desktop must use a named /app/data volume because host bind mounts lack reliable locking.
# The private container network provides isolation; never publish the backend port directly.
CMD ["python", "-m", "uvicorn", "careerdesk.bootstrap.app:app", "--workers", "1", "--host", "0.0.0.0", "--port", "8000"]
