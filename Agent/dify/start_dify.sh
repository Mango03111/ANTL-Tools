#!/usr/bin/env bash
set -Eeuo pipefail

COMPOSE_DIR="/home/antl/Dify/docker"
TIMEOUT="${DIFY_TIMEOUT_SECONDS:-300}"

format_duration() {
  local total="$1"
  printf '%02d:%02d' "$((total / 60))" "$((total % 60))"
}

print_startup_status() {
  local elapsed="$1" api_health="$2" http_code="$3" service_summary="$4"
  local api_display="${api_health:-waiting}"
  local http_display="${http_code:-waiting}"
  printf '\r\033[K%-118s [等待 %s / %s]' \
    "Dify 服务正在启动... API: ${api_display}, HTTP: ${http_display} | ${service_summary}" \
    "$(format_duration "$elapsed")" "$(format_duration "$TIMEOUT")"
}

service_summary() {
  local -a ids
  local running=0 healthy=0 health_starting=0 initialized=0
  local state exit_code health

  mapfile -t ids < <(docker compose ps -aq --all 2>/dev/null)
  if ((${#ids[@]} == 0)); then
    printf '服务等待创建'
    return
  fi

  while IFS='|' read -r state exit_code health; do
    [[ "$state" == "running" ]] && ((running += 1))
    [[ "$health" == "healthy" ]] && ((healthy += 1))
    [[ "$health" == "starting" ]] && ((health_starting += 1))
    [[ "$state" == "exited" && "$exit_code" == "0" ]] && ((initialized += 1))
  done < <(docker inspect -f '{{.State.Status}}|{{.State.ExitCode}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${ids[@]}" 2>/dev/null)

  printf '运行 %d，健康 %d，检查中 %d，初始化完成 %d' \
    "$running" "$healthy" "$health_starting" "$initialized"
}

cd "$COMPOSE_DIR"
echo "Starting Dify (data volumes are preserved)..."
# 保留 Compose 的原始逐服务创建、启动和健康检查信息。
docker compose up -d

deadline=$((SECONDS + TIMEOUT))
while (( SECONDS < deadline )); do
  api_health="$(docker compose ps -q api | xargs -r docker inspect -f '{{.State.Health.Status}}' 2>/dev/null || true)"
  http_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1/ 2>/dev/null || true)"
  startup_summary="$(service_summary)"
  if [[ "$api_health" == "healthy" && "$http_code" =~ ^(200|30[127])$ ]]; then
    printf '\r%*s\r' 120 ''
    lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "Dify 已启动并通过健康检查。"
    echo
    echo "访问地址："
    echo "  本机控制台: http://127.0.0.1:80"
    if [[ -n "$lan_ip" ]]; then
      echo "  局域网控制台: http://${lan_ip}:80"
    fi
    echo "  HTTPS 入口: https://<服务器地址>:443（配置证书后使用）"
    echo "  插件调试端口: <服务器地址>:5003"
    echo
    echo "服务状态："
    echo "  HTTP 响应: $http_code"
    echo "  API 健康状态: $api_health"
    echo "  整体服务统计: $startup_summary"
    echo "  Compose 目录: $COMPOSE_DIR"
    echo
    echo "常用命令："
    echo "  查看状态: cd $COMPOSE_DIR && docker compose ps"
    echo "  查看日志: cd $COMPOSE_DIR && docker compose logs -f"
    echo "  停止服务: /home/antl/stop_dify.sh"
    exit 0
  fi
  print_startup_status "$SECONDS" "$api_health" "$http_code" "$startup_summary"
  sleep 2
done

printf '\n'
echo "Dify did not become healthy within ${TIMEOUT}s." >&2
docker compose ps >&2 || true
exit 1
