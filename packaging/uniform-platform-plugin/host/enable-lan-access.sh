#!/usr/bin/env bash
# Enable authenticated LAN access to the Uniform control plane on this DGX Spark.
set -euo pipefail

SERVICE=${UNIFORM_SERVICE_NAME:-uniform-dgxspark.service}
PORT=${UNIFORM_PORT:-18180}
EXTERNAL_HOSTS=()
SHOW_TOKEN=0
ROTATE_TOKEN=0
DRY_RUN=0
NO_RESTART=0

usage() {
  cat <<'EOF'
用法: host/enable-lan-access.sh [选项]

在这台 DGX Spark 上一次性开放平台控制端口，并生成平台 API Key。

  --port PORT             平台控制端口，默认 18180
  --external-host HOST    额外允许的控制面主机名/IP（VPN、隧道或反代地址）
                          可重复；用于让外网/VPN 地址也能执行发布操作
  --show-token            在终端显示平台 API Key（默认只显示保存位置）
  --rotate-token          生成新的 API Key，使旧 Key 立即失效
  --no-restart            只写入配置，不重启服务
  --dry-run               只显示将要写入的配置（API Key 会隐藏）
  -h, --help              显示帮助

说明：本脚本只负责局域网/VPN 可达和鉴权，不会把 18180 直接暴露到公网。
外网请使用 VPN、Cloudflare Tunnel 或 HTTPS 反向代理，并传入对应域名。
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT=${2:?}; shift 2 ;;
    --external-host) EXTERNAL_HOSTS+=("${2:?}"); shift 2 ;;
    --show-token) SHOW_TOKEN=1; shift ;;
    --rotate-token) ROTATE_TOKEN=1; shift ;;
    --no-restart) NO_RESTART=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "端口无效: $PORT" >&2
  exit 2
fi

command -v systemctl >/dev/null 2>&1 || {
  echo "没有找到 systemctl；请在 systemd 主机上运行本脚本。" >&2
  exit 1
}

if ! MERGED=$(systemctl --user cat "$SERVICE" 2>/dev/null); then
  echo "找不到用户服务 $SERVICE。请先安装并启动 Uniform 平台。" >&2
  exit 1
fi

EXEC_START=$(printf '%s\n' "$MERGED" | sed -n 's/^ExecStart=//p' | tail -n 1)
if [ -z "$EXEC_START" ]; then
  echo "无法从 $SERVICE 读取 ExecStart，请手动配置监听地址。" >&2
  exit 1
fi

ENV_TOKEN=$(printf '%s\n' "$MERGED" |
  sed -n 's/^Environment=UNIFORM_API_TOKEN=//p' | tail -n 1 | sed -e 's/^"//' -e 's/"$//')
ENV_HOSTS=$(printf '%s\n' "$MERGED" |
  sed -n 's/^Environment=UNIFORM_PUBLICATION_CONTROL_HOSTS=//p' | tail -n 1 |
  sed -e 's/^"//' -e 's/"$//')
EXEC_PORT=$(printf '%s\n' "$EXEC_START" | sed -n 's/.*--port[= ]\([0-9][0-9]*\).*/\1/p' | tail -n 1)
if [ -n "$EXEC_PORT" ]; then
  PORT="$EXEC_PORT"
fi

CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
DROPIN_DIR="$CONFIG_HOME/systemd/user/$SERVICE.d"
DROPIN="$DROPIN_DIR/50-external-access.conf"

TOKEN="$ENV_TOKEN"
if [ "$ROTATE_TOKEN" -eq 1 ] || [ -z "$TOKEN" ]; then
  TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
fi

if [[ "$EXEC_START" == *run-local.sh* ]]; then
  NEW_EXEC="$EXEC_START"
else
  NEW_EXEC=$(printf '%s\n' "$EXEC_START" |
    sed -E 's/(^|[[:space:]])--host([= ])[^[:space:]]+/\1--host\20.0.0.0/g')
  if [ "$NEW_EXEC" = "$EXEC_START" ]; then
    NEW_EXEC="$EXEC_START --host 0.0.0.0"
  fi
fi

ALL_HOSTS=()
add_host() {
  local candidate=${1//[[:space:]]/}
  candidate=${candidate#[}
  candidate=${candidate%]}
  candidate=${candidate%.}
  [ -z "$candidate" ] && return
  case "$candidate" in
    *[!A-Za-z0-9._:-]*)
      echo "控制面主机名无效: $candidate" >&2
      exit 2
      ;;
  esac
  local existing
  for existing in "${ALL_HOSTS[@]:-}"; do
    if [ "${existing,,}" = "${candidate,,}" ]; then
      return
    fi
  done
  ALL_HOSTS+=("$candidate")
}

IFS=',' read -r -a EXISTING_HOSTS <<<"$ENV_HOSTS"
for host in "${EXISTING_HOSTS[@]:-}"; do add_host "$host"; done
for host in "${EXTERNAL_HOSTS[@]:-}"; do add_host "$host"; done

TOKEN_FOR_OUTPUT="<hidden>"
if [ "$SHOW_TOKEN" -eq 1 ]; then
  TOKEN_FOR_OUTPUT="$TOKEN"
fi

BLOCK=$(cat <<EOF
[Service]
Environment=UNIFORM_HOST=0.0.0.0
Environment=UNIFORM_API_TOKEN=$TOKEN
ExecStart=
ExecStart=$NEW_EXEC
EOF
)
if [ ${#ALL_HOSTS[@]} -gt 0 ]; then
  JOINED=$(IFS=,; echo "${ALL_HOSTS[*]}")
  BLOCK+=$'\n'"Environment=UNIFORM_PUBLICATION_CONTROL_HOSTS=$JOINED"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  printf '%s\n' "$BLOCK" | sed "s|$TOKEN|<hidden>|g"
  echo
  echo "目标文件: $DROPIN"
  exit 0
fi

mkdir -p "$DROPIN_DIR"
if [ -f "$DROPIN" ]; then
  cp -p "$DROPIN" "$DROPIN.bak"
  chmod 0600 "$DROPIN.bak"
fi
umask 077
printf '%s\n' "$BLOCK" >"$DROPIN.tmp"
chmod 0600 "$DROPIN.tmp"
mv "$DROPIN.tmp" "$DROPIN"

if [ "$NO_RESTART" -eq 0 ]; then
  systemctl --user daemon-reload
  systemctl --user restart "$SERVICE"
  HEALTHY=0
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 3 -H "X-API-Key: $TOKEN" \
      "http://127.0.0.1:$PORT/api/v1/health" >/dev/null 2>&1; then
      HEALTHY=1
      break
    fi
    sleep 1
  done
  if [ "$HEALTHY" -ne 1 ]; then
    echo "警告：服务已重启，但健康检查未通过。请查看：" >&2
    echo "  systemctl --user status $SERVICE" >&2
    echo "  journalctl --user -u $SERVICE -n 80 --no-pager" >&2
  fi
fi

echo "已写入：$DROPIN（权限 0600）"
echo "平台 API Key：$TOKEN_FOR_OUTPUT"
if [ "$SHOW_TOKEN" -eq 0 ]; then
  echo "查看 Key：bash $0 --show-token"
fi
echo
echo "局域网访问地址："
if command -v hostname >/dev/null 2>&1; then
  for address in $(hostname -I 2>/dev/null || true); do
    case "$address" in
      127.*|::1) continue ;;
      *:*) echo "  http://[$address]:$PORT" ;;
      *) echo "  http://$address:$PORT" ;;
    esac
  done
fi
if [ ${#ALL_HOSTS[@]} -gt 0 ]; then
  echo "已允许的控制面主机：${ALL_HOSTS[*]}"
fi
echo
echo "注意：本脚本没有开放公网。外网请使用 VPN 或 HTTPS 隧道/反向代理；"
echo "      不要把 $PORT 以明文 HTTP 直接映射到公网。"
