# Uniform DGX Spark 平台插件

这是一个标准 **stdio MCP server**。同事在自己的电脑上安装后，可以直接用
DeepSeek Harness、Codex、Claude Code 等支持 MCP 的 AI 工具，通过自然语言操作这台
DGX Spark 上的 Uniform 平台：查看模型、部署模型、查看日志、发布服务、开关模型外网入口。

- 插件只调用平台 HTTP API，不执行本地 shell，不直接操作 Docker。
- 只需要 Python 3.10+，不需要安装任何第三方 Python 包。
- 支持主地址 + 备用地址自动切换：例如局域网地址优先，VPN/外网地址兜底。
- 默认允许部署与发布；请只把 API Key 发给可信同事。

## 一、主机管理员：一次性开放平台

平台默认只监听 `127.0.0.1`，同事无法从其他电脑访问。先在本机运行：

```bash
bash host/enable-lan-access.sh --show-token
```

脚本会：

1. 把平台监听地址改为 `0.0.0.0`（只改 systemd drop-in，不改项目源码）；
2. 生成随机 `UNIFORM_API_TOKEN`，写入权限 `0600` 的
   `~/.config/systemd/user/uniform-dgxspark.service.d/50-external-access.conf`；
3. 重启 `uniform-dgxspark.service` 并做健康检查；
4. 打印局域网地址和平台 API Key。

把「局域网地址 + API Key」发给同事。以后查看 Key：

```bash
bash host/enable-lan-access.sh --show-token
```

轮换 Key（旧 Key 立即失效）：

```bash
bash host/enable-lan-access.sh --rotate-token --show-token
```

### 外网访问

平台没有内建控制台公网入口。`18180` 必须通过 **VPN/组网** 或 **HTTPS 隧道/反向代理**
到达，不能把明文 HTTP 直接映射到公网。

推荐顺序：

1. **Tailscale / ZeroTier / WireGuard**：最省事，同事加入同一网络后用组网 IP 访问；
2. **Cloudflare Tunnel / frp / nginx**：反代到 `127.0.0.1:18180`，外面套 HTTPS。

使用隧道或 VPN 域名时，要让平台允许该控制面主机执行发布操作。例如：

```bash
bash host/enable-lan-access.sh \
  --external-host uniform.example.com \
  --show-token
```

`--external-host` 可重复。它只把该主机名加入控制面白名单，不会自动开放防火墙或公网。

Cloudflare Tunnel 的临时验证可以这样启动（示例，URL 每次会变）：

```bash
cloudflared tunnel --url http://127.0.0.1:18180
```

拿到 `https://xxxx.trycloudflare.com` 后，用
`--external-host xxxx.trycloudflare.com` 重新运行主机脚本，再把这个 HTTPS 地址作为
同事安装时的 `--fallback-url`。长期使用建议配置具名 Tunnel 或自有域名。

> 模型发布端口是独立的 `18181`，与平台控制台 `18180` 不是一回事。
> 插件里的 `enable_external_access` 只负责给「已发布的模型」开外网入口。

## 二、同事电脑：安装插件

解压安装包后进入目录。Linux / macOS：

```bash
python3 install.py \
  --url http://10.0.0.5:18180 \
  --key '平台 API Key'
```

Windows PowerShell：

```powershell
python .\install.py --url http://10.0.0.5:18180 --key "平台 API Key"
```

同时配置外网备用地址（主地址不通时自动切换）：

```bash
python3 install.py \
  --url http://10.0.0.5:18180 \
  --fallback-url https://uniform.example.com \
  --key '平台 API Key'
```

不想让 Key 出现在命令历史里，可以用文件或交互输入：

```bash
python3 install.py --url http://10.0.0.5:18180 --key-file ~/uniform.key
python3 install.py --url http://10.0.0.5:18180
```

安装器会：

- 复制标准 MCP server 到用户目录；
- 生成 POSIX、Windows `.cmd` 和 PowerShell 启动器；
- 写出权限 `0600` 的 `mcp.json` 与 `platform.env`（含 Key，不会打印 Key）；
- 运行一次 `platform_status` 自检。

常用选项：

| 选项 | 作用 |
| --- | --- |
| `--config PATH` | 自动合并到 Harness 的 `.json` 或 `.toml` MCP 配置 |
| `--print-only` | 只打印脱敏配置片段，不写文件 |
| `--skip-check` | 安装时不联网自检 |
| `--strict` | 自检失败时返回非零退出码 |
| `--install-dir DIR` | 自定义安装目录 |
| `--name NAME` | MCP server 名称，默认 `uniform-platform` |

不知道 Harness 的配置路径时，先看配置片段：

```bash
python3 install.py --url http://10.0.0.5:18180 --print-only
```

再把输出中的 `mcpServers` 条目加入你所用 Harness 的 MCP 配置。不同版本的
DeepSeek Harness 配置位置可能不同，安装器不会猜测并覆盖未知路径。

### JSON 配置

```bash
python3 install.py --url http://10.0.0.5:18180 \
  --config ~/.deepseek/mcp.json
```

### TOML 配置

```bash
python3 install.py --url http://10.0.0.5:18180 \
  --config ~/.config/deepseek/config.toml
```

合并前会在同目录生成 `.bak`；可用 `--no-backup` 关闭。TOML 默认识别已有的
`mcp_servers` 或 `mcpServers` 根表。

## 三、安装后的配置形态

```json
{
  "mcpServers": {
    "uniform-platform": {
      "command": "/usr/bin/python3",
      "args": ["/home/you/.local/share/uniform-harness-mcp/uniform_harness_mcp.py"],
      "env": {
        "UNIFORM_PLATFORM_URL": "http://10.0.0.5:18180",
        "UNIFORM_PLATFORM_API_KEY": "平台 API Key",
        "UNIFORM_PLATFORM_FALLBACK_URL": "https://uniform.example.com",
        "UNIFORM_PLATFORM_FALLBACK_API_KEY": "平台 API Key"
      }
    }
  }
}
```

启动命令使用安装器所在 Python 的绝对路径，不依赖同事机器上的 `PATH`。

## 四、可用的工具

| 工具 | 作用 |
| --- | --- |
| `platform_status` | 平台健康检查，返回实际使用的地址 |
| `open_platform_ui` | 返回平台前端地址，用浏览器打开即可 |
| `list_catalog_models` / `list_local_models` | 查在线模型 / 本机已下载模型 |
| `list_deployments` / `get_deployment` / `deployment_logs` | 查部署状态与容器日志 |
| `deploy_model` | 创建部署 |
| `wait_for_deployment` | 等待 running / failed / cancelled |
| `stop_deployment` | 停止部署并撤销发布 |
| `publish_service` | 发布模型到局域网，返回 Base URL 和模型 Key |
| `publication_status` / `rotate_publication_key` / `unpublish_service` | 发布管理 |
| `enable_external_access` / `disable_external_access` | 开关已发布模型的外网入口 |

## 五、验证与排错

手动自检：

```bash
python3 ~/.local/share/uniform-harness-mcp/uniform_harness_mcp.py --self-test
```

Windows 默认路径为 `%LOCALAPPDATA%\uniform-harness-mcp`。

- `platform unreachable`：地址/端口错、防火墙拦截，或主机仍是 `127.0.0.1`；
- `platform rejected the API key`：Key 与主机 `UNIFORM_API_TOKEN` 不一致；
- `421 untrusted control-plane host`：外网/VPN 域名没有加入
  `UNIFORM_PUBLICATION_CONTROL_HOSTS`，在主机脚本加 `--external-host` 后重启；
- 自检通过但 Harness 看不到工具：确认 MCP 配置已加载，并重启 Harness；
- 重新安装：再运行一次安装器即可覆盖插件与配置。

## 六、安全边界

- 不要把 `18180` 以明文 HTTP 暴露到公网；外网只用 VPN 或 HTTPS。
- API Key 等同平台管理权限，不要提交到 Git、聊天群或工单系统。
- 配置文件与 `platform.env` 在 POSIX 系统上设为 `0600`。
- 不再需要外网时，删除隧道/反代，并运行
  `bash host/enable-lan-access.sh --rotate-token` 使旧 Key 失效。
