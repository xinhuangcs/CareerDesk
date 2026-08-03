#!/usr/bin/env bash

# Build the public Dockerfile, then exercise the contracts that unit tests cannot
# prove: immutable image metadata, non-root named-volume ownership, HTTP trust,
# static/API same-origin serving, single-owner locking, and crash/stop takeover.
set -Eeuo pipefail

readonly PREFIX="${PREFIX:-jp-smoke-$$}"
if [[ ! "$PREFIX" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$ ]]; then
  echo "PREFIX must be a short Docker-safe name" >&2
  exit 64
fi
readonly IMAGE="${IMAGE:-careerdesk-runtime-smoke:${PREFIX}}"

readonly VOLUME="${PREFIX}-data"
readonly BAD="${PREFIX}-bad"
readonly OWNER="${PREFIX}-owner"
readonly CONTENDER="${PREFIX}-contender"
readonly AFTER_KILL="${PREFIX}-after-kill"
readonly AFTER_STOP="${PREFIX}-after-stop"
readonly TEMP_ROOT="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"

umask 077
ENV_FILE="$(mktemp "${TEMP_ROOT%/}/${PREFIX}.env.XXXXXX")"
readonly ENV_FILE
CREATED_CONTAINERS=()
VOLUME_CREATED=0

logs() {
  docker logs "$1" 2>&1 || true
}

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  if ((${#CREATED_CONTAINERS[@]})); then
    docker rm -f "${CREATED_CONTAINERS[@]}" >/dev/null 2>&1
  fi
  if ((VOLUME_CREATED)); then
    docker volume rm -f "$VOLUME" >/dev/null 2>&1
  fi
  rm -f "$ENV_FILE"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

wait_stopped() {
  local name="$1"
  local state
  for _ in {1..30}; do
    state="$(docker inspect --format '{{.State.Status}}' "$name" 2>/dev/null || true)"
    if [[ "$state" == "exited" || "$state" == "dead" ]]; then
      return 0
    fi
    sleep 1
  done
  echo "${name} did not stop within 30 seconds" >&2
  logs "$name" >&2
  return 1
}

wait_healthy() {
  local name="$1"
  local running
  local health
  for _ in {1..60}; do
    running="$(docker inspect --format '{{.State.Running}}' "$name" 2>/dev/null || true)"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$name" 2>/dev/null || true)"
    if [[ "$running" == "true" && "$health" == "healthy" ]]; then
      return 0
    fi
    if [[ "$running" == "false" ]]; then
      break
    fi
    sleep 1
  done
  echo "${name} did not become healthy" >&2
  logs "$name" >&2
  return 1
}

run_app() {
  docker run -d \
    --name "$1" \
    --mount "type=volume,source=${VOLUME},target=/app/data" \
    --env-file "$ENV_FILE" \
    "$IMAGE" >/dev/null
  CREATED_CONTAINERS+=("$1")
}

echo "Building ${IMAGE}"
docker build --tag "$IMAGE" .

expected_cmd='["python","-m","uvicorn","careerdesk.bootstrap.app:app","--workers","1","--host","0.0.0.0","--port","8000"]'
actual_cmd="$(docker image inspect --format '{{json .Config.Cmd}}' "$IMAGE")"
[[ "$actual_cmd" == "$expected_cmd" ]] || {
  echo "Unexpected image CMD: ${actual_cmd}" >&2
  exit 1
}
[[ "$(docker image inspect --format '{{.Config.User}}' "$IMAGE")" == "10001:10001" ]] || {
  echo "The runtime image must use fixed non-root UID:GID 10001:10001" >&2
  exit 1
}
image_env="$(docker image inspect --format '{{json .Config.Env}}' "$IMAGE")"
[[ "$image_env" == *'APP_RUNTIME_MODE=server'* ]] || {
  echo "The runtime image must default to fail-closed server mode" >&2
  exit 1
}
health_test="$(docker image inspect --format '{{json .Config.Healthcheck.Test}}' "$IMAGE")"
[[ "$health_test" == *'http.client'* && "$health_test" == *'APP_ALLOWED_HOSTS'* ]] || {
  echo "The runtime image is missing the trusted-Host healthcheck" >&2
  exit 1
}

# A developer's dotenv or credential files must not cross the build boundary.
docker run --rm --entrypoint sh "$IMAGE" -c \
  'test ! -e /app/.env && test ! -e /app/.npmrc && test ! -e /app/.netrc && test ! -e /app/.pypirc'

# The default server image must fail closed when the gateway credential is absent.
docker run -d \
  --name "$BAD" \
  --env APP_DEBUG=false \
  --env APP_DEV_FAKE_USER= \
  --env APP_ALLOWED_HOSTS=jobs.example.test \
  --env APP_ALLOWED_ORIGINS=https://jobs.example.test \
  "$IMAGE" >/dev/null
CREATED_CONTAINERS+=("$BAD")
wait_stopped "$BAD"
[[ "$(docker inspect --format '{{.State.ExitCode}}' "$BAD")" -ne 0 ]]
bad_logs="$(logs "$BAD")"
if [[ "$bad_logs" != *APP_GATEWAY_AUTH_SECRET* ]]; then
  printf '%s\n' "$bad_logs" >&2
  echo "Server failed for an unexpected reason instead of the missing gateway credential" >&2
  exit 1
fi
credential="$(openssl rand -hex 32)"
if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
  printf '::add-mask::%s\n' "$credential"
fi
{
  printf '%s\n' \
    'APP_DATA_DIR=/app/data' \
    'APP_DEBUG=false' \
    'APP_DEV_FAKE_USER=' \
    'APP_ALLOWED_HOSTS=jobs.example.test' \
    'APP_ALLOWED_ORIGINS=https://jobs.example.test'
  printf 'APP_GATEWAY_AUTH_SECRET=%s\n' "$credential"
} > "$ENV_FILE"
unset credential

if docker volume inspect "$VOLUME" >/dev/null 2>&1; then
  echo "Refusing to reuse pre-existing Docker volume ${VOLUME}" >&2
  exit 1
fi
docker volume create "$VOLUME" >/dev/null
VOLUME_CREATED=1
run_app "$OWNER"
wait_healthy "$OWNER"

# EXPOSE is documentation; the backend must not be published to the host by this contract.
[[ -z "$(docker port "$OWNER" 2>/dev/null || true)" ]]

# A fresh named volume must inherit the fixed application identity without a root entrypoint.
docker exec -i "$OWNER" python - <<'PY'
import os
import stat
from pathlib import Path

assert os.getuid() == 10001
assert os.getgid() == 10001
for path in (
    Path("/app/data"),
    Path("/app/data/careerdesk.db"),
    Path("/app/data/.careerdesk.instance.lock"),
):
    info = path.stat()
    assert info.st_uid == 10001, (path, info.st_uid)
    assert info.st_gid == 10001, (path, info.st_gid)
assert stat.S_IMODE(Path("/app/data").stat().st_mode) == 0o700
assert stat.S_IMODE(Path("/app/data/careerdesk.db").stat().st_mode) == 0o600
assert stat.S_IMODE(Path("/app/data/.careerdesk.instance.lock").stat().st_mode) == 0o600

argv = Path("/proc/1/cmdline").read_bytes().split(b"\0")
assert argv[:3] == [b"python", b"-m", b"uvicorn"], argv
PY

# Static content and API traffic are served by the same FastAPI process.
docker exec -i "$OWNER" python - <<'PY'
import http.client
import re

HOST = "jobs.example.test"


def request(method, path, headers=None, body=None):
    merged = {"Host": HOST, **(headers or {})}
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=3)
    connection.request(method, path, body=body, headers=merged)
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


assert request("GET", "/healthz") == (200, b"ok")
index_status, index = request("GET", "/")
assert index_status == 200 and b'<div id="root"></div>' in index
asset_match = re.search(rb'(?:src|href)="(/assets/[^"?]+\.(?:js|css))', index)
assert asset_match, index[:500]
asset_status, asset = request("GET", asset_match.group(1).decode("ascii"))
assert asset_status == 200 and asset
assert request("GET", "/settings") == (200, index)
assert request("GET", "/api/not-a-real-route")[0] == 404

assert request("GET", "/api/settings")[0] == 401
# Debug routes are absent.  The SPA catch-all intentionally turns non-/api
# paths into index.html, so prove that these paths do not expose a schema/UI.
assert request("GET", "/openapi.json") == (200, index)
assert request("GET", "/docs") == (200, index)
assert request("GET", "/healthz", {"Host": "evil.example"})[0] == 400

write_path = "/api/maintenance/reconcile"
write_body = b"{}"
content_headers = {"Content-Type": "application/json"}
assert request(
    "POST",
    write_path,
    {**content_headers, "Origin": "https://jobs.example.test"},
    write_body,
)[0] == 403
assert request(
    "POST",
    write_path,
    {
        **content_headers,
        "Origin": "https://evil.example",
        "X-CareerDesk-Request": "1",
    },
    write_body,
)[0] == 403
assert request(
    "POST",
    write_path,
    {
        **content_headers,
        "Origin": "https://jobs.example.test",
        "X-CareerDesk-Request": "1",
    },
    write_body,
)[0] == 401
PY

docker exec -i "$OWNER" python - <<'PY'
from pathlib import Path

marker = Path("/app/data/.ci-volume-marker")
marker.write_text("persistent\n", encoding="utf-8")
marker.chmod(0o600)
PY

# A second process sharing the same data root must fail specifically on the owner lock.
run_app "$CONTENDER"
wait_stopped "$CONTENDER"
[[ "$(docker inspect --format '{{.State.ExitCode}}' "$CONTENDER")" -ne 0 ]]
contender_logs="$(logs "$CONTENDER")"
if [[ "$contender_logs" != *InstanceAlreadyRunningError* ]]; then
  printf '%s\n' "$contender_logs" >&2
  echo "Contender failed for an unexpected reason instead of the data-root owner lock" >&2
  exit 1
fi
wait_healthy "$OWNER"

# SIGKILL skips lifespan cleanup; the OS lock still has to release with process death.
docker kill --signal KILL "$OWNER" >/dev/null
wait_stopped "$OWNER"
[[ "$(docker inspect --format '{{.State.ExitCode}}' "$OWNER")" -eq 137 ]]

run_app "$AFTER_KILL"
wait_healthy "$AFTER_KILL"
docker exec -i "$AFTER_KILL" python - <<'PY'
from pathlib import Path

assert Path("/app/data/.ci-volume-marker").read_text(encoding="utf-8") == "persistent\n"
PY

# The exec-form PID 1 must also terminate cleanly and release the lock on SIGTERM.
docker stop --time 15 "$AFTER_KILL" >/dev/null
wait_stopped "$AFTER_KILL"
[[ "$(docker inspect --format '{{.State.ExitCode}}' "$AFTER_KILL")" -eq 0 ]]

run_app "$AFTER_STOP"
wait_healthy "$AFTER_STOP"
docker exec -i "$AFTER_STOP" python - <<'PY'
from pathlib import Path

assert Path("/app/data/.ci-volume-marker").read_text(encoding="utf-8") == "persistent\n"
PY

echo "Docker runtime contract passed"
