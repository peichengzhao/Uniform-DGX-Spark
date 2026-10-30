# 飞书机器人私聊告警：开发与验收

日期：2026-10-27。项目：`uniform-dgxspark`。

后续授权修正见 [飞书 PKCE 绑定修正与诊断](feishu-pkce-binding-fix.md)。授权入口已与 v3 凭证端点配对，并新增脱敏诊断；本文下方初次上线记录保留为历史验收，不代表真实飞书授权已经通过。

## 功能边界

系统监控页新增「飞书机器人私聊告警」，覆盖 GPU 温度、统一内存、磁盘、CPU、GPU、Swap 和已添加远端节点的离线状态。告警后台独立运行，关闭浏览器不影响检查；不部署、重启或停止模型。

本版使用飞书自建应用机器人给绑定用户发私聊消息。扫码只绑定收件人，不能自动创建应用，也不会以用户身份发消息。入口打开飞书当前官方授权页，由用户扫码授权；不是平台内嵌二维码，不使用仅支持旧登录流程的二维码 SDK。

未配置、未绑定、测试消息未成功时不允许开启自动告警。安装上线不代表已经连接真实飞书账号。

## 分阶段实现与验证

| 阶段 | 开发内容 | 可验证条件 |
| --- | --- | --- |
| 1：配置与存储 | 配置校验、受限私有文件、数据库版本 25 迁移 | 非法阈值/重复规则被拒绝；旧库记录保留；接口不回传密钥 |
| 2：绑定与发送 | 官方 OAuth、PKCE、一次性 state、浏览器绑定、机器人测试消息 | 过期/复用/跨浏览器授权失败；只申请机器人发送权限；测试成功才能开启 |
| 3：后台规则 | 持续超限、恢复滞回、冷却、持久化发送队列与有限重试 | 短尖峰不告警；重复或过期采样不累计；恢复取消旧告警；重试使用同一消息 UUID |
| 4：平台页面 | 配置指南、七项规则、绑定/测试/解绑、历史记录 | 桌面和窄屏操作通过；刷新不覆盖未保存表单；解绑关闭告警并保留历史 |
| 5：上线 | 数据库备份、确认活动任务、仅重载平台 | 平台与模型健康；通知线程运行、默认关闭；原精度记录和数据集接入保留 |

## 首次配置：没有飞书应用时

1. 进入 [飞书开发者后台](https://open.feishu.cn/app)，在自己的飞书组织中创建企业自建应用，例如「DGX Spark 资源告警」。如果组织限制创建应用，需要管理员协助；平台不能绕过管理员要求。
2. 添加「机器人」能力。在权限管理中开启应用身份权限 `im:message:send_as_bot`。本功能不需要读取聊天、手机号、邮箱或以用户身份发消息的权限。
3. 在应用「安全设置 → 重定向 URL」中添加完整回调地址。本机使用：

   `http://127.0.0.1:18180/integrations/feishu/callback`

4. 创建并发布应用版本，确认管理员批准/启用（组织要求时），并确保应用可用范围包含你。
5. 从 `http://127.0.0.1:18180/` 打开平台的「系统监控」，填写 App ID、App Secret、上面的回调地址并保存。不要把 App Secret 发给 AI 对话。
6. 点击「扫码绑定我的飞书」，在新窗口的飞书官方授权页扫码并同意。成功后返回平台，确认显示自己的收件人信息。
7. 打开与机器人的私聊，确认未屏蔽机器人。点击「发送测试消息」并确认，实际收到消息后勾选「开启自动告警」、调整规则并保存。

打开平台的地址必须与回调地址同源：不要在 `localhost` 页面使用 `127.0.0.1` 回调，或在远程地址页面使用本机回调。回调由扫码的桌面浏览器完成，不是给飞书事件推送设置公网地址；本地配置无需擅自把平台暴露公网。远程访问及配置应用密钥必须使用 HTTPS，并按实际可信域名配置平台。

修改应用凭证或回调地址会清除旧绑定、测试验证、待发送通知，需要重新绑定并测试。解绑会关闭自动告警，保留历史。测试消息至少间隔 30 秒。

## 告警规则与可靠性

- GPU 温度默认示例：达到 85°C 持续 60 秒告警，降至 80°C 恢复；持续超限每 600 秒提醒。
- 内存默认示例：90% / 85%，持续 60 秒；磁盘：90% / 85%，持续 120 秒；远端离线：持续 60 秒。
- CPU、GPU 高占用以及 Swap 规则默认关闭，避免正常评测期间刷屏。阈值是可编辑示例，不是硬件厂商的安全上限。
- 后台每 5 秒检查现有采样。采样需在 30 秒内有效；同一采样不重复累计持续时间。缺失、过期、NaN、Inf 或布尔数值不会视为 0，也不会触发假恢复。
- 正常恢复时取消尚未发出的旧超限告警；移除节点/规则、改配置、解绑也取消相应旧待发送通知。
- 发送队列和告警状态持久化。可重试的网络超时、限流、服务器错误或凭证过期最多尝试 3 次，使用同一消息 UUID；权限错误直接失败并显示安全错误说明。平台不保证飞书或网络故障期间必达。

## 安全与数据

- 私有配置路径：`data/feishu-private/config.json`，目录权限 700、文件权限 600，原子替换；本版是权限保护的明文存储，**不是加密存储**。
- API 不回传 App Secret，校验失败不回显输入，授权回调访问日志去除 query，避免授权码和 state 入日志。若另加反向代理，也需避免该路径的 query 入代理日志。
- 收件人仅保存应用对应的 `open_id`、显示名和绑定/验证时间；不持久化用户 access token 或 refresh token。机器人 token 仅在内存缓存。
- OAuth 使用 5 分钟一次性 state、PKCE S256 和 HttpOnly 浏览器 cookie。写接口要求可信 Host、同源及专用操作标识；AI capability 不允许访问通知配置接口。
- 数据库迁移至版本 25，新增 `feishu_recipient`、`system_alert_state`、`system_alert_events`，不改变已有评测结果口径。

## 验证记录

开发期使用隔离数据库、模拟飞书接口和真实 Chromium，不调用真实飞书账号、不发送真实消息、不人为升高硬件温度。

- 原监控、集群、UI 与存储回归：22 项通过。
- 通知 API/规则与真实浏览器初轮：24 项通过；随后补充无效响应码和节点移除的回归用例。
- 上轮全量回归：301 项通过、5 项失败，飞书相关 29 项全部通过。4 项失败为测试用 Codex 初始化所需目录只读；另 1 项为目录页面测试期待旧文案。目录权限已恢复，旧文案断言已更新；本轮相关 7 项复验通过（7.22 秒）。
- 本轮完整回归：**306 项全部通过，365.13 秒，无跳过**。覆盖真实 Chromium 和安装的 Codex 审批回归；飞书相关 29 项包含 API、规则、持久化、安全门槛及桌面/窄屏操作。
- Ruff 全项目检查、新增五个 Python 文件格式检查、`app.js` 与 `feishu.js` 语法检查均通过。

复验命令（项目根目录，使用既有环境，不安装依赖）：

```bash
PATH="$PWD/.runtime/node-v22.23.3-linux-arm64/bin:$PATH" \
PYTHONDONTWRITEBYTECODE=1 UNIFORM_REQUIRE_BROWSER=1 UNIFORM_REQUIRE_CODEX=1 \
timeout 600 .venv/bin/python -m pytest -q -o cache_dir=/tmp/uniform-feishu-full tests

.venv/bin/ruff check --no-cache src tests scripts/export-accuracy-datasets.py
.runtime/node-v22.23.3-linux-arm64/bin/node --check src/uniform_dgxspark/web/app.js
.runtime/node-v22.23.3-linux-arm64/bin/node --check src/uniform_dgxspark/web/feishu.js
```

真实 Codex 回归需要其安装标识和临时目录可写；本轮仅恢复初始化权限，不改变平台 AI 工具审批策略或全局 Codex 配置。浏览器和 Codex 测试均设置为必需，不以跳过替代验收。

上线前一致性备份：

`data/backups/uniform-before-feishu-alerts-20261027-2a912q5g.db`

本轮重载前新增一致性备份（仅当前用户可读写）：

`data/backups/uniform-before-feishu-reload-20261027-7bzyv7o_.db`

备份库版本 24，保留 4 条精度记录、7 条 AI 会话及 1 条已验证接入数据集。用户允许停止既有 MMLU 接入 AI 对话并重载平台，明确要求模型继续运行；此授权不覆盖新增活动任务。

### 初次生产上线检查（历史验收）

- 已停止用户批准的 MMLU 接入 AI 对话 `cac93a25-e1d4-48dc-9e58-f9d4088359a6`，保留对话历史，未执行模型停止接口。重载前确认没有新增活动评测、部署、页面编辑或待自动续跑的 AI 任务。
- 仅重载 `uniform-dgxspark.service`，服务恢复为 `active`，平台进程由 `2728357` 更换为 `2988281`。
- 平台 `/api/v1/health`、模型 `http://127.0.0.1:8000/health` 均为 200；重载等待期间也检查了模型健康为 200。Qwen3.8-27B 保持运行。
- 库版本已从 24 迁移至 25，`PRAGMA quick_check` 为 `ok`。与本轮备份逐表内容校验一致：4 条精度运行、3 条规范判分指标、1 条已验证数据集接入、4 条对比记录、0 条性能测试记录和 4 条部署记录未变；7 条 AI 会话 ID 全部保留。
- MMLU 仍在平台允许执行的数据集中，验证证据仍引用原运行 `46453f75-f919-4a03-a174-3a3549e99147`；已接入数据集仍为 4 项。
- 飞书状态 API 为 200：`configured=false`、`enabled=false`、`worker_running=true`、7 条规则、无收件人、无错误、无消息事件。生产未填入模拟应用凭证，未创建私有配置文件，也未发送真实消息。
- 生产页面只读验收通过：1440×1000 桌面与 390×844 窄屏均显示「等待配置飞书应用」、7 条规则、正确的本机回调地址和首次配置指南，无横向溢出、无 JavaScript 异常；未配置时绑定/测试/开启告警按钮不可用。验收先等待部署卡片渲染完成，再点击系统监控，避免在页面事件绑定之前点击造成误判；未提交配置或触发登录。

生产页面截图：

- [桌面页面](/home/dgx-spark/Documents/Codex/2026-10-20/name-base-url-https-aiserver-sunrise/outputs/feishu-notifications-desktop-20261027.png)
- [窄屏页面](/home/dgx-spark/Documents/Codex/2026-10-20/name-base-url-https-aiserver-sunrise/outputs/feishu-notifications-mobile-20261027.png)

重载注意：旧平台进程没有在既有 90 秒退出窗口内结束，由服务管理器终止后启动新平台。这也发生于当天早前的重载，不能认定是本次新增功能导致。本次未修改平台退出策略；初始 45 秒探活窗口因此超时，随后只读复核确认平台正常恢复、模型正常、数据完整。该退出超时是后续可单独排查的运维问题，不是飞书配置失败。

备份是数据库一致性备份，不是源码备份。任何恢复操作需另行确认，避免覆盖上线后新增的数据。

真实飞书验收仍需用户完成创建应用、扫码授权、收到测试消息以及开启后的真实通知检查。

## 官方接口依据

- [获取授权码](https://open.feishu.cn/document/common-capabilities/sso/api/obtain-oauth-code)：官方授权页 `https://accounts.feishu.cn/open-apis/authen/v1/authorize`。
- [获取用户 access token v3](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token-v3)：表单授权码及 PKCE 换取 token。
- [获取 tenant access token](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/tenant_access_token)：`https://accounts.feishu.cn/oauth/v3/token` 的 client_credentials 流程，限定机器人发送 scope。
- [获取用户信息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/authen-v1/user_info/get)：绑定应用对应收件人。
- [发送消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create)：机器人身份、`receive_id_type=open_id`、消息 UUID 去重。
- [旧扫码登录 SDK](https://open.feishu.cn/document/common-capabilities/sso/web-application-sso/qr-sdk-documentation)：仅支持旧登录流程，本版不使用。
