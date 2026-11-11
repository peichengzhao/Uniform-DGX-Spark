# Uniform DGX Spark 平台插件

这是一个标准的 **DeepSeek Harness 组合包**，安装后会通过内置 MCP 客户端连接 DGX Spark 上的 Uniform 平台。安装包根目录包含 `package.json`，并声明了 `dsh.bundle.patch`；不会再出现“这个包没有声明组合包”的错误。

## 同事电脑安装

1. 在 DeepSeek Harness 的“插件”页面选择本地安装。
2. 选择 `uniform-dgxspark-platform-1.2.3.tgz`。
3. 安装完成后，在 Harness 终端配置平台地址和 API Key：

Linux / macOS：

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

主地址使用局域网、备用地址使用 VPN 或 HTTPS 隧道时：

```bash
"$DSH_PROFILE_DIR/node_modules/.bin/uniform-dgxspark-configure" \
  --url http://10.21.212.6:18180 \
  --fallback-url https://uniform.example.com \
  --key-file ~/uniform.key
```

配置会保存到用户目录，不会写进 npm 包：

- Linux / macOS：`~/.config/uniform-dgxspark/platform.env`
- Windows：`%APPDATA%\uniform-dgxspark\platform.env`

保存后重启 DeepSeek Harness，或重新加载插件。工具会以
`mcp__uniform_dgxspark__*` 的形式出现。

## 运行要求

- DeepSeek Harness 自带 Node.js，不需要另装 Node。
- 需要本机安装 Python 3.10 或更高版本。找不到时可用 `UNIFORM_PYTHON` 指定 Python 可执行文件。
- 插件只调用平台 HTTP API，不需要 Docker 权限。

## 可用能力

- 查询平台健康状态、在线模型、本机模型、部署状态和日志；
- 部署、停止模型，发布到局域网，轮换或撤销发布 Key；
- 为已发布模型开启或关闭外网入口；
- 主地址不可达时自动尝试备用地址。

## 主机管理员

平台默认只监听 `127.0.0.1`。要让其他电脑访问，请在 DGX Spark 上运行包内脚本：

```bash
bash host/enable-lan-access.sh --show-token
```

外网不要直接映射明文 HTTP。请使用 VPN、Tailscale、Cloudflare Tunnel 或 HTTPS 反向代理。使用 VPN/隧道域名执行发布操作时，需要把该域名加入控制面白名单：

```bash
bash host/enable-lan-access.sh --external-host uniform.example.com --show-token
```

## 排错

查看当前配置（Key 会隐藏）：

```bash
"$DSH_PROFILE_DIR/node_modules/.bin/uniform-dgxspark-configure" --show
```

不联网检查、只打印脱敏配置：

```bash
"$DSH_PROFILE_DIR/node_modules/.bin/uniform-dgxspark-configure" \
  --url http://10.21.212.6:18180 --key-file ~/uniform.key --skip-check --print-only
```

- `platform unreachable`：端口、防火墙或监听地址不正确；
- `platform rejected the API key`：Key 与主机的 `UNIFORM_API_TOKEN` 不一致；
- `421 untrusted control-plane host`：外网/VPN 域名尚未加入控制面白名单；
- 工具不出现：先完成配置，再重启 Harness；确认本机 Python 3.10+ 可运行。
