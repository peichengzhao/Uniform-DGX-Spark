#!/usr/bin/env bash
# One-command installer for the Uniform platform MCP plugin (DeepSeek Harness & friends).
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SOURCE_MODULE="$PROJECT_DIR/src/uniform_dgxspark/harness_mcp.py"
INSTALL_DIR=${UNIFORM_HARNESS_HOME:-"$HOME/.local/share/uniform-harness-mcp"}
BIN_DIR=${UNIFORM_HARNESS_BIN:-"$HOME/.local/bin"}
SERVER_NAME=${UNIFORM_HARNESS_SERVER_NAME:-uniform-platform}
PLATFORM_URL=${UNIFORM_PLATFORM_URL:-}
PLATFORM_KEY=${UNIFORM_PLATFORM_API_KEY:-}
CONFIG_PATH=${UNIFORM_HARNESS_CONFIG:-}
SKIP_CHECK=0
PRINT_ONLY=0

usage() {
  cat <<'EOF'
用法: install-deepseek-harness-mcp.sh [选项]

  --url URL         平台地址，例如 http://10.21.212.6:18180 或 https://uniform.example.com
  --key KEY         平台 API Key（X-API-Key）
  --config PATH     DeepSeek Harness 的 MCP 配置文件；给定时自动合并 JSON/TOML
  --name NAME       MCP server 名称，默认 uniform-platform
  --install-dir DIR 安装目录，默认 ~/.local/share/uniform-harness-mcp
  --print-only      只输出配置片段，不写入 Harness 配置
  --skip-check      跳过安装后的连通性自检
  -h, --help        显示帮助

也可以用环境变量：UNIFORM_PLATFORM_URL、UNIFORM_PLATFORM_API_KEY、UNIFORM_HARNESS_CONFIG。
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --url) PLATFORM_URL=${2:?}; shift 2 ;;
    --key) PLATFORM_KEY=${2:?}; shift 2 ;;
    --config) CONFIG_PATH=${2:?}; shift 2 ;;
    --name) SERVER_NAME=${2:?}; shift 2 ;;
    --install-dir) INSTALL_DIR=${2:?}; shift 2 ;;
    --print-only) PRINT_ONLY=1; shift ;;
    --skip-check) SKIP_CHECK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

if [ ! -f "$SOURCE_MODULE" ]; then
  echo "找不到插件源码: $SOURCE_MODULE" >&2
  echo "请在项目目录内运行本脚本（scripts/install-deepseek-harness-mcp.sh）。" >&2
  exit 1
fi

if [ -z "$PLATFORM_URL" ]; then
  printf '平台地址（例如 http://10.21.212.6:18180）: '
  read -r PLATFORM_URL
fi
PLATFORM_URL=${PLATFORM_URL%/}

if [ -z "$PLATFORM_KEY" ]; then
  printf '平台 API Key（留空表示平台未启用鉴权）: '
  read -r -s PLATFORM_KEY
  printf '\n'
fi

PYTHON=${PYTHON:-$(command -v python3 || true)}
if [ -z "$PYTHON" ]; then
  echo "需要 python3（3.10 或以上）。" >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR" "$BIN_DIR"
RUNNER_PYTHON=""
has_httpx() {
  [ -n "$1" ] && "$1" -c 'import httpx' >/dev/null 2>&1
}
if [ "$PRINT_ONLY" -eq 0 ]; then
  if has_httpx "$INSTALL_DIR/venv/bin/python"; then
    RUNNER_PYTHON="$INSTALL_DIR/venv/bin/python"
  else
    if "$PYTHON" -m venv "$INSTALL_DIR/venv" >/dev/null 2>&1 &&
      "$INSTALL_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check \
        'httpx>=0.28,<0.29' >/dev/null 2>&1; then
      RUNNER_PYTHON="$INSTALL_DIR/venv/bin/python"
    else
      for candidate in "$PROJECT_DIR/.venv/bin/python" "$PYTHON"; do
        if has_httpx "$candidate"; then
          RUNNER_PYTHON="$candidate"
          break
        fi
      done
    fi
  fi
  if [ -z "$RUNNER_PYTHON" ]; then
    echo "无法准备 httpx 运行环境：离线安装 python3 -m venv 失败，且本机没有带 httpx 的 Python。" >&2
    exit 1
  fi
  cp "$SOURCE_MODULE" "$INSTALL_DIR/uniform_harness_mcp.py"
fi

COMMAND_PATH="uniform-harness-mcp"
if [ "$PRINT_ONLY" -eq 0 ]; then
  cat > "$BIN_DIR/uniform-harness-mcp" <<EOF
#!/usr/bin/env bash
exec "${RUNNER_PYTHON:-python3}" "$INSTALL_DIR/uniform_harness_mcp.py" "\$@"
EOF
  chmod 0755 "$BIN_DIR/uniform-harness-mcp"
  COMMAND_PATH="$BIN_DIR/uniform-harness-mcp"
fi
cat > "$INSTALL_DIR/mcp.json" <<EOF
{
  "mcpServers": {
    "$SERVER_NAME": {
      "command": "$COMMAND_PATH",
      "args": [],
      "env": {
        "UNIFORM_PLATFORM_URL": "$PLATFORM_URL",
        "UNIFORM_PLATFORM_API_KEY": "$PLATFORM_KEY"
      }
    }
  }
}
EOF
chmod 0600 "$INSTALL_DIR/mcp.json"

if [ -n "$CONFIG_PATH" ] && [ "$PRINT_ONLY" -eq 0 ]; then
  "$RUNNER_PYTHON" - "$CONFIG_PATH" "$INSTALL_DIR/mcp.json" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1]).expanduser()
snippet_path = Path(sys.argv[2])
snippet = json.loads(snippet_path.read_text(encoding="utf-8"))
entry = snippet["mcpServers"]

if config_path.suffix.lower() == ".toml":
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    name, server = next(iter(entry.items()))
    block = [f"[mcp_servers.{name}]", f'command = "{server["command"]}"', "args = []", ""]
    block.append(f"[mcp_servers.{name}.env]")
    for key, value in server["env"].items():
        block.append(f'{key} = "{value}"')
    new_block = "\n".join(block)
    marker = f"[mcp_servers.{name}]"
    if marker in text:
        head = text.split(marker, 1)[0].rstrip("\n")
        text = f"{head}\n\n{new_block}\n"
    else:
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + new_block + "\n"
    config_path.write_text(text, encoding="utf-8")
else:
    existing = {}
    if config_path.exists():
        existing = json.loads(config_path.read_text(encoding="utf-8") or "{}")
    servers = existing.setdefault("mcpServers", {})
    servers.update(entry)
    config_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"已合并 MCP 配置: {config_path}")
PY
fi

if [ "$SKIP_CHECK" -eq 0 ] && [ "$PRINT_ONLY" -eq 0 ]; then
  printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"platform_status","arguments":{}}}' \
    | UNIFORM_PLATFORM_URL="$PLATFORM_URL" UNIFORM_PLATFORM_API_KEY="$PLATFORM_KEY" \
      "$BIN_DIR/uniform-harness-mcp" > "$INSTALL_DIR/selftest.json" 2>"$INSTALL_DIR/selftest.err" || true
  if grep -q '"isError": false' "$INSTALL_DIR/selftest.json"; then
    echo "自检通过：插件已能访问平台。"
  else
    echo "自检未通过，请检查地址/Key/网络。详情：" >&2
    head -c 400 "$INSTALL_DIR/selftest.json" >&2 || true
    head -c 400 "$INSTALL_DIR/selftest.err" >&2 || true
    echo >&2
  fi
fi

if [ "$PRINT_ONLY" -eq 1 ]; then
  cat <<EOF
仅输出配置（未安装插件）：$INSTALL_DIR/mcp.json

把其中的 mcpServers 条目粘贴到 DeepSeek Harness 的 MCP 配置；
如要真正安装，去掉 --print-only 重新运行。
EOF
  exit 0
fi

cat <<EOF

安装完成。
  MCP 可执行文件 : $COMMAND_PATH
  配置片段       : $INSTALL_DIR/mcp.json
  平台地址       : $PLATFORM_URL

如果还没有写进 DeepSeek Harness，请二选一：
  1) 重新运行并加 --config <harness 的 MCP 配置文件路径>，自动合并；
  2) 把 $INSTALL_DIR/mcp.json 里的 mcpServers 条目粘贴到 Harness 的 MCP 配置。
EOF
