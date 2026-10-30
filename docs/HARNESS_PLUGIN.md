# DeepSeek Harness 插件（平台 MCP）

把「部署 / 发布模型」这套平台能力做成标准 **stdio MCP server**。同事在自己的电脑上装好后，
DeepSeek Harness、Codex、Claude Code 等支持 MCP 的 AI 工具都能通过自然语言操作这台
DGX Spark 上的 Uniform 平台。

插件只调用平台 HTTP API（`X-API-Key`），不执行 shell、不直连 Docker，也不要求同事拿到项目源码。
它只需要 Python 3.10+，第三方依赖为零。

## 一、主机：开放控制台访问

### 局域网

平台默认只监听 `127.0.0.1`。在本机运行：

```bash
bash host/enable-lan-access.sh --show-token
```

脚本会写入一个权限 `0600` 的 systemd drop-in，完成三件事：

- 把监听地址改为 `0.0.0.0`，并同步改写实际 `ExecStart`（不只设置一个无效的环境变量）；
- 生成随机 `UNIFORM_API_TOKEN`；
- 重启 `uniform-dgxspark.service` 并做健康检查。

把脚本打印的「局域网地址 + API Key」发给同事。以后查看 Key：

```bash
bash host/enable-lan-access.sh --show-token
```

轮换 Key：

```bash
bash host/enable-lan-access.sh --rotate-token --show-token
```

### 外网

平台没有内建控制台公网入口。`18180` 需要由主机侧打通，任选其一：

- **VPN / 组网**：Tailscale、ZeroTier、WireGuard，最省事、最安全；
- **HTTPS 隧道或反向代理**：Cloudflare Tunnel、frp、nginx 反代到 `127.0.0.1:18180`。

使用 VPN/隧道域名时，把该主机名加入控制面白名单，否则发布类操作会返回
`421 untrusted control-plane host`：

```bash
bash host/enable-lan-access.sh \
  --external-host uniform.example.com \
  --show-token
```

`--external-host` 可重复，只加入白名单，不会自动开放防火墙或公网。

Cloudflare Tunnel 临时验证示例（URL 每次会变）：

```bash
cloudflared tunnel --url http://127.0.0.1:18180
```

拿到 `https://xxxx.trycloudflare.com` 后，用
`--external-host xxxx.trycloudflare.com` 重跑主机脚本，再把这个 HTTPS 地址作为同事安装时的
`--fallback-url`。长期使用应配置具名 Tunnel 或自有域名。

> 平台控制台 `18180` 与模型发布端口 `18181` 是两个入口。
> 插件里的 `enable_external_access` 只给「已发布的模型」开外网入口，不会暴露控制台。

## 二、同事电脑：一条命令安装

解压发布包后进入目录。Linux / macOS：

```bash
python3 install.py \
  --url http://10.21.212.6:18180 \
  --key '平台 API Key'
```

Windows PowerShell：

```powershell
python .\install.py --url http://10.21.212.6:18180 --key "平台 API Key"
```

主地址是局域网、备用地址是外网时：

```bash
python3 install.py \
  --url http://10.21.212.6:18180 \
  --fallback-url https://uniform.example.com \
  --key '平台 API Key'
```

不想把 Key 写进命令历史：

```bash
python3 install.py --url http://10.21.212.6:18180 --key-file ~/uniform.key
python3 install.py --url http://10.21.212.6:18180   # 交互输入
```

安装器会：

1. 复制标准 MCP server 和跨平台启动器；
2. 写出权限 `0600` 的 `mcp.json` 与 `platform.env`；
3. 调用一次 `platform_status` 自检；主地址不通时自动尝试备用地址；
4. 给出 Harness 配置片段，或在指定 `--config` 时自动合并 JSON/TOML。

### 配置 Harness

不同版本 DeepSeek Harness 的配置路径可能不同，安装器不会猜测并覆盖未知文件。
不知道路径时先打印脱敏片段：

```bash
python3 install.py --url http://10.21.212.6:18180 --print-only
```

已知路径时自动合并：

```bash
python3 install.py --url http://10.21.212.6:18180 --config ~/.deepseek/mcp.json
python3 install.py --url http://10.21.212.6:18180 --config ~/.config/deepseek/config.toml
```

合并前会生成 `.bak`（`--no-backup` 可关闭）。TOML 会识别已有的 `mcp_servers` 或
`mcpServers` 根表。

配置形态：

```json
{
  "mcpServers": {
    "uniform-platform": {
      "command": "/usr/bin/python3",
      "args": ["/home/you/.local/share/uniform-harness-mcp/uniform_harness_mcp.py"],
      "env": {
        "UNIFORM_PLATFORM_URL": "http://10.21.212.6:18180",
        "UNIFORM_PLATFORM_API_KEY": "平台 API Key",
        "UNIFORM_PLATFORM_FALLBACK_URL": "https://uniform.example.com",
        "UNIFORM_PLATFORM_FALLBACK_API_KEY": "平台 API Key"
      }
    }
  }
}
```

`command` 使用安装时 Python 的绝对路径，不依赖同事机器上的 `PATH`。

## 三、插件提供的工具

| 工具 | 作用 |
| --- | --- |
| `platform_status` | 平台健康检查，返回实际使用的地址 |
| `open_platform_ui` | 返回平台前端地址（页面就是平台自己的前端） |
| `list_catalog_models` / `list_local_models` | 查在线模型 / 本机已下载模型 |
| `deploy_model` | 创建部署（模型、端口、镜像、显存比例、本地路径等） |
| `list_deployments` / `get_deployment` / `deployment_logs` | 查部署状态与容器日志 |
| `wait_for_deployment` | 轮询到 running / failed / cancelled |
| `stop_deployment` | 停止部署并撤销发布 |
| `publish_service` | 发布到局域网，返回 Base URL 与模型 API Key |
| `publication_status` / `rotate_publication_key` / `unpublish_service` | 发布状态、换 Key、撤销 |
| `enable_external_access` / `disable_external_access` | 开关该服务的 Cloudflare 外网入口 |

## 四、验证与排错

```bash
python3 ~/.local/share/uniform-harness-mcp/uniform_harness_mcp.py --self-test
```

Windows 默认目录是 `%LOCALAPPDATA%\uniform-harness-mcp`。

- `platform unreachable`：地址/端口错、防火墙拦截，或主机仍只监听 `127.0.0.1`；
- `platform rejected the API key`：Key 与主机 `UNIFORM_API_TOKEN` 不一致；
- `421 untrusted control-plane host`：外网/VPN 域名未加入
  `UNIFORM_PUBLICATION_CONTROL_HOSTS`，在主机脚本加 `--external-host`；
- Harness 看不到工具：确认 MCP 配置已加载并重启 Harness；
- 重新安装：再运行一次安装器即可覆盖插件与配置。

## 五、开发与打包

发布包由源码自动生成，`uniform_harness_mcp.py` 不手工维护第二份：

```bash
python3 scripts/build-harness-plugin.py --out dist
sha256sum -c dist/uniform-platform-plugin-1.1.0.sha256
```

产物包含 `manifest.json`、`install.py`、`install.sh`、`install.ps1`、主机脚本、README 和
MCP 入口文件。构建过程会拒绝把 `.venv`、`.runtime`、`data/`、数据库或缓存打进去。
