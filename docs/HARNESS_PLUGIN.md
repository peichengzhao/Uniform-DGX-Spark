# DeepSeek Harness 插件（平台 MCP）

把 DGX Spark 上的 Uniform 平台包装成一个标准 **DeepSeek Harness 组合包**。安装后，
Harness 会通过内置 MCP 客户端加载平台工具，用自然语言完成模型查询、部署、发布和模型外网入口管理。

插件本身包含：

- `package.json`，声明 `dsh.bundle.patch`；
- `cordis.patch.yml`，挂载官方 `@deepseek-ai/dsh-mcp-client`；
- 标准 stdio MCP server；
- Node 启动器与配置工具；
- 中英文名称、说明和图标。

> 旧版 `uniform-platform-plugin-*.zip` 是便携脚本包，不是 DSH 组合包，不能拖进
> Harness 插件管理器。Harness 必须安装 `uniform-dgxspark-platform-<版本>.tgz`。

## 一、主机：开放控制台访问

### 局域网

平台默认只监听 `127.0.0.1`。在 DGX Spark 上运行：

```bash
bash host/enable-lan-access.sh --show-token
```

脚本会写入权限 `0600` 的 systemd drop-in：

- 把监听地址改为 `0.0.0.0`；
- 生成随机 `UNIFORM_API_TOKEN`；
- 重启 `uniform-dgxspark.service` 并做健康检查。

把脚本打印的「局域网地址 + API Key」发给同事。后续查看或轮换 Key：

```bash
bash host/enable-lan-access.sh --show-token
bash host/enable-lan-access.sh --rotate-token --show-token
```

### 外网

不要直接把明文 `18180` 映射到公网。推荐：

- **VPN / 组网**：Tailscale、ZeroTier、WireGuard；
- **HTTPS 隧道或反向代理**：Cloudflare Tunnel、frp、nginx。

使用 VPN/隧道域名时，把该主机名加入控制面白名单，否则发布类操作会返回
`421 untrusted control-plane host`：

```bash
bash host/enable-lan-access.sh \
  --external-host uniform.example.com \
  --show-token
```

Cloudflare Tunnel 临时验证：

```bash
cloudflared tunnel --url http://127.0.0.1:18180
```

拿到 `https://xxxx.trycloudflare.com` 后，用 `--external-host` 重新运行主机脚本，
再把这个 HTTPS 地址作为同事配置时的 `--fallback-url`。

> 控制台端口 `18180` 与模型发布端口 `18181` 是两个入口。
> `enable_external_access` 只给已发布模型开外网入口，不会暴露控制台。

## 二、同事电脑：安装 DSH 组合包

### 1. 安装包

可以直接在“插件”页面填写 Git 仓库地址：

```text
https://github.com/peichengzhao/Uniform-DGX-Spark.git
```

仓库根目录现在本身就是 DSH 组合包，不会再因为清单只在子目录里而被判定为
`not-bundle`。

也可以下载发布包后选择本地安装：

在 DeepSeek Harness 的“插件”页面选择本地安装，然后选择：

```text
uniform-dgxspark-platform-1.2.3.tgz
```

也可以使用 CLI（路径必须是绝对路径）：

```bash
dsh plugin --profile default install /absolute/path/uniform-dgxspark-platform-1.2.3.tgz
```

> 不要再使用 `.zip` 安装到 DSH。包根缺少 `package.json` 和 `dsh.bundle.patch`
> 时会被明确判定为 `not-bundle`。

### 2. 配置地址和 Key

安装后进入 Harness 终端。Linux / macOS：

```bash
"$DSH_PROFILE_DIR/node_modules/.bin/uniform-dgxspark-configure" \
  --url http://10.21.212.6:18180 \
  --key-file ~/uniform.key
```

Windows PowerShell：

```powershell
& "$env:DSH_PROFILE_DIR\node_modules\.bin\uniform-dgxspark-configure.cmd" `
  --url http://10.21.212.6:18180 `
  --key-file "$HOME\uniform.key"
```

主地址是局域网、备用地址是 VPN/外网：

```bash
"$DSH_PROFILE_DIR/node_modules/.bin/uniform-dgxspark-configure" \
  --url http://10.21.212.6:18180 \
  --fallback-url https://uniform.example.com \
  --key-file ~/uniform.key
```

配置写入用户目录，权限 `0600`，不会进入 npm 包：

- Linux / macOS：`~/.config/uniform-dgxspark/platform.env`
- Windows：`%APPDATA%\uniform-dgxspark\platform.env`

保存后重启 Harness 或重新加载插件。工具名称形式为
`mcp__uniform_dgxspark__<tool>`。

### 3. 运行要求

- DSH 自带 Node.js，不需要另装 Node；
- 同事电脑需要 Python 3.10+；
- 如自动找不到 Python，可设置环境变量 `UNIFORM_PYTHON` 指向解释器；
- 插件只调用平台 HTTP API，不需要 Docker 权限。

## 三、插件工具

| 工具 | 作用 |
| --- | --- |
| `platform_status` | 平台健康检查，返回实际使用的地址 |
| `open_platform_ui` | 返回平台前端地址，并重新打开浏览器 |
| `list_catalog_models` / `list_local_models` | 查在线模型 / 本机已下载模型 |
| `deploy_model` | 创建部署 |
| `list_deployments` / `get_deployment` / `deployment_logs` | 查部署状态与容器日志 |
| `wait_for_deployment` | 轮询到 running / failed / cancelled |
| `stop_deployment` | 停止部署并撤销发布 |
| `publish_service` | 发布到局域网，返回 Base URL 与模型 API Key |
| `publication_status` / `rotate_publication_key` / `unpublish_service` | 发布状态、换 Key、撤销 |
| `enable_external_access` / `disable_external_access` | 开关该服务的 Cloudflare 外网入口 |

### 前端页面自动打开

Agent 第一次调用任意工具时，插件会用默认浏览器打开平台前端，方便你一边看部署进度、一边
在页面上补齐工具没覆盖的操作。同一个 MCP 进程内只打开一次；调用 `open_platform_ui` 则每次
都会重新打开。没有桌面会话时（无头服务器、纯 SSH）静默跳过，只在 stderr 留一行记录，
不会影响工具结果。

- `UNIFORM_AUTO_OPEN_UI=0`：关闭自动打开，`open_platform_ui` 仍然可用；
- `UNIFORM_UI_URL`：需要打开其它地址（例如带域名的外网入口）时覆盖默认值。

## 四、验证与排错

查看脱敏后的当前配置：

```bash
"$DSH_PROFILE_DIR/node_modules/.bin/uniform-dgxspark-configure" --show
```

仅检查配置、不写文件：

```bash
"$DSH_PROFILE_DIR/node_modules/.bin/uniform-dgxspark-configure" \
  --url http://10.21.212.6:18180 --key-file ~/uniform.key --print-only
```

- `not-bundle`：装成了旧 ZIP；改用新版 `.tgz`；
- `platform unreachable`：地址/端口错、防火墙拦截，或主机仍只监听 `127.0.0.1`；
- `platform rejected the API key`：Key 与主机 `UNIFORM_API_TOKEN` 不一致；
- `421 untrusted control-plane host`：外网/VPN 域名未加入
  `UNIFORM_PUBLICATION_CONTROL_HOSTS`；
- 工具不出现：完成配置后重启 Harness，并确认 Python 3.10+ 可运行。

## 五、开发与打包

发布包由源码自动生成，MCP server 不维护第二份：

```bash
NODE=.runtime/node/bin/node \
  python3 scripts/build-dsh-plugin.py --out dist --node "$NODE"
sha256sum -c dist/uniform-dgxspark-platform-1.2.3.sha256
```

主要产物：

```text
dist/uniform-dgxspark-platform-1.2.3.tgz          # 安装到 DeepSeek Harness
dist/uniform-dgxspark-platform-1.2.3-source.zip   # 源码审阅，不用于插件管理
dist/uniform-dgxspark-platform-1.2.3.sha256
```

构建会校验 `dsh.bundle.patch`、`files` 清单、Python/JavaScript 语法和禁止目录，
并拒绝把 `.venv`、`.runtime`、`data/`、数据库、缓存或 API Key 打进包里。
