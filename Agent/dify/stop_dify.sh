#!/usr/bin/env bash
set -Eeuo pipefail

COMPOSE_DIR="/home/antl/Dify/docker"
TIMEOUT="${DIFY_TIMEOUT_SECONDS:-180}"

cd "$COMPOSE_DIR"
echo "Stopping Dify (data volumes will be kept)..."
# Deliberately omit --volumes/-v: PostgreSQL, Weaviate and file storage persist.
docker compose down

deadline=$((SECONDS + TIMEOUT))
while (( SECONDS < deadline )); do
  remaining="$(docker compose ps -q 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "$remaining" == "0" ]]; then
    echo "Dify is stopped; persistent data volumes were retained."
    exit 0
  fi
  sleep 2
done

echo "Dify containers are still present after ${TIMEOUT}s." >&2
docker compose ps >&2 || true
exit 1
