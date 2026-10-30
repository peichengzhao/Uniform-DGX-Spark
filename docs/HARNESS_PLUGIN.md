# DeepSeek Harness 插件（平台 MCP）

把「部署 / 发布模型」这套平台能力做成标准 **MCP server**，任何支持 MCP 的 AI 工具
（DeepSeek Harness、Codex、Claude Code 等）装上后就能用自然语言操作这台 DGX Spark 上的平台。

- 插件本身不带界面：需要看页面时调用 `open_platform_ui`，它会返回平台前端地址，用浏览器打开即可。
- 插件不直连 Docker、不执行 shell，只调用平台 HTTP API（`X-API-Key`）。
- 默认允许直接部署与发布，不做二次确认。

## 一、平台侧准备

插件在**别人的电脑**上时，那台电脑必须能访问平台的控制端口（默认 `18180`）。

### 局域网

平台默认只监听 `127.0.0.1`，需要改成监听局域网地址，并**务必设置 API Key**：

```bash
systemctl --user edit uniform-dgxspark.service
```

写入：

```ini
[Service]
Environment=UNIFORM_HOST=0.0.0.0
Environment=UNIFORM_API_TOKEN=请换成长随机串
```

```bash
systemctl --user daemon-reload
systemctl --user restart uniform-dgxspark.service
```

别人电脑访问 `http://<这台机器的局域网 IP>:18180`。

### 外网

平台没有内建「控制台外网入口」，`18180` 需要由你来打通，任选其一：

- **VPN / 组网**：Tailscale、ZeroTier、WireGuard 等，对方用组网 IP 访问（最省事、最安全）；
- **反向代理 / 隧道**：Cloudflare Tunnel、frp、nginx 反代到 `127.0.0.1:18180`，并把地址发给对方；
  该地址要启用 HTTPS，且**必须**设置 `UNIFORM_API_TOKEN`，否则公网等于裸奔。

> 平台页面里的「外网访问」开关只针对模型发布端口 `18181`，不包含控制台 `18180`。

## 二、对方电脑安装插件

```bash
cd uniform_dgxspark-plugin
bash scripts/install-deepseek-harness-mcp.sh \
  --url http://10.21.212.6:18180 \
  --key '你的平台 API Key' \
  --config ~/.deepseek/mcp.json
```

脚本会：

1. 准备一个只含 `httpx` 的轻量运行环境（优先 venv，离线时回退到本机已有的 Python）；
2. 复制插件入口并生成 `~/.local/bin/uniform-harness-mcp`；
3. 写出 MCP 配置（`~/.local/share/uniform-harness-mcp/mcp.json`），`--config` 指定时会自动合并
   JSON 或 TOML 配置；
4. 调用一次 `platform_status` 做连通性自检。

不确定 Harness 的配置路径时，去掉 `--config`，脚本会把可直接粘贴的配置片段打出来；
也可以加 `--print-only` 只打印、不安装。

配置片段形如：

```json
{
  "mcpServers": {
    "uniform-platform": {
      "command": "/home/you/.local/bin/uniform-harness-mcp",
      "args": [],
      "env": {
        "UNIFORM_PLATFORM_URL": "http://10.21.212.6:18180",
        "UNIFORM_PLATFORM_API_KEY": "你的平台 API Key"
      }
    }
  }
}
```

## 三、插件提供的工具

| 工具 | 作用 |
| --- | --- |
| `platform_status` | 平台健康检查 |
| `open_platform_ui` | 返回平台前端地址（页面就是平台自己的前端） |
| `list_catalog_models` / `list_local_models` | 查在线模型 / 本机已下载模型 |
| `deploy_model` | 创建部署（模型、端口、镜像、显存比例、本地路径等） |
| `list_deployments` / `get_deployment` / `deployment_logs` | 查部署状态与容器日志 |
| `wait_for_deployment` | 轮询到 running / failed / cancelled |
| `stop_deployment` | 停止部署并撤销发布 |
| `publish_service` | 发布到局域网，返回 Base URL 与模型 API Key |
| `publication_status` / `rotate_publication_key` / `unpublish_service` | 发布状态、换 Key、撤销 |
| `enable_external_access` / `disable_external_access` | 开关该服务的 Cloudflare 外网入口 |

## 四、示例对话

- 「看看本机已经下载了哪些模型，挑一个 27B 的部署到 8000 端口，跑起来后告诉我健康状态。」
- 「把刚部署的模型发布到局域网，给我 Base URL 和 Key，并顺便开外网访问。」
- 「日志最后 200 行贴出来，判断为什么起不来。」
- 「把 `<deployment-id>` 停掉并撤销发布。」

## 五、排错

- 自检报 `platform rejected the API key`：`--key` 与平台 `UNIFORM_API_TOKEN` 不一致；
- 自检报 `platform unreachable`：地址/端口错、防火墙拦了、平台仍只监听 `127.0.0.1`；
- 发布返回 `service is not running`：先 `wait_for_deployment` 等到 `running` 再发布；
- 重新安装只需再跑一次脚本，会覆盖插件与配置（Key 会更新）。
