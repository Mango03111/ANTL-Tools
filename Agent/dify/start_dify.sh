#!/usr/bin/env bash
set -Eeuo pipefail

COMPOSE_DIR="/home/antl/Dify/docker"
TIMEOUT="${DIFY_TIMEOUT_SECONDS:-300}"

cd "$COMPOSE_DIR"
echo "Starting Dify (data volumes are preserved)..."
docker compose up -d

deadline=$((SECONDS + TIMEOUT))
while (( SECONDS < deadline )); do
  api_health="$(docker compose ps -q api | xargs -r docker inspect -f '{{.State.Health.Status}}' 2>/dev/null || true)"
  http_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1/ 2>/dev/null || true)"
  if [[ "$api_health" == "healthy" && "$http_code" =~ ^(200|30[127])$ ]]; then
    echo "Dify is running: http://127.0.0.1 (HTTP $http_code, API healthy)"
    exit 0
  fi
  sleep 2
done

echo "Dify did not become healthy within ${TIMEOUT}s." >&2
docker compose ps >&2 || true
exit 1
