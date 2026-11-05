# Uniform DGX Spark 使用指南

## 启动

```bash
cd /home/zaki/workspace/uniform_dgxspark
./scripts/run-local.sh
```

打开 `http://127.0.0.1:18180`。首次运行会按 `uv.lock` 检查依赖，并在 `./data` 创建数据库和模型缓存。

## 部署模型

模型中心的在线模型与本地模型卡片统一使用“一键部署”，不再要求用户先判断“AI 部署”还是
“配置部署”。平台会先查找同一模型、来源、revision、本地路径和拓扑对应的已验证启动模板：

- 原服务仍健康时直接复用；
- 受管 vLLM 模板按已验证的结构化参数重新创建任务；
- 单机 Docker/SGLang 服务使用绑定容器完整 ID 与镜像的本地启动/停止脚本；
- 多机、自定义进程或容器已被删除/替换时交给原 AI 会话恢复；
- 模板缺失或失效时自动创建带失败上下文的新部署会话，不因模板校验失败终止部署目标。

首次部署成功后，平台生成脱敏的启动、停止脚本并分别记录 SHA-256。对于可安全识别的单机
容器，平台会执行一次“停止 → 启动 → 健康检查 → 真实推理”复现验证，并确保最终保持运行；
只有全流程通过的脚本才能在以后绕过 AI 直接执行。直接执行失败时，平台先把错误和运行环境
交回原部署 AI 会话，原会话不可用才创建带备份上下文的新会话。历史脚本中的密钥不会写入
模板，升级前已有的旧模板不会自动重启当前模型。模型服务与任务进度仍在“部署中心”查看，
完整 CLI 对话统一在独立“AI 会话”页查看。

### AI 工具

“一键部署”的“AI 工具”支持 Codex、Claude Code、DeepSeek Harness CLI 和
**DSH 原生会话**。平台用 `<CLI> --version` 探测前三个 CLI，并单独探测官方
`deepseek-harness-sdk`；不可用的工具会在页面中显示并置灰。

DSH 原生会话与旧的 `deepseek` CLI 是两条独立接入：

- 原生会话通过 Python SDK 启动受管的 `dsh --profile sdk`，保留持久化 Session；精简对话会实时
  显示原生思考过程，并在正式回答出现后自动折叠。工具调用、命令与错误仍可在原始终端查看。
- 不需要把 `dsh` 加入 PATH，`uv sync` 安装的 SDK 会携带匹配版本的运行时。
- 使用 DeepSeek 官方模型路由，默认模型为 `deepseek-v4-flash`。在平台服务环境中设置
  `DEEPSEEK_API_KEY`；兼容端点可通过 `DEEPSEEK_BASE_URL` 指定。
- 默认模型的思考强度支持 `low`、`high`、`max`。留空时使用模型自身默认值。
- 数据集接入和 AI 精度评测都可以选择 Codex 或 DSH 原生会话；这两类受限任务会把 DSH
  工具权限限制为只读，业务操作仍只能通过平台 MCP 工具完成。

若平台由 systemd 管理，可通过用户级 drop-in 注入凭证，避免把 Key 写进项目文件：

```bash
systemctl --user edit uniform-dgxspark.service
```

在打开的覆盖配置中加入：

```ini
[Service]
Environment=DEEPSEEK_API_KEY=你的_Key
```

保存后执行 `systemctl --user daemon-reload && systemctl --user restart uniform-dgxspark.service`。
缺少凭证时，DSH 精简对话会明确显示 `MISSING_CREDENTIAL`，不会假装产生模型结果。

接入 DeepSeek Harness 时：

1. 安装其 CLI，并确认终端里能直接执行 `deepseek --version`；
2. 如命令名不同，在服务单元中设置 `UNIFORM_DEEPSEEK_BINARY=/绝对路径/deepseek`；
3. 需要固定开关（例如自动批准、沙箱级别）时，把它们写进 `UNIFORM_DEEPSEEK_ARGS`，
   平台会按顺序拼成 `deepseek <ARGS> [--model 模型] <任务说明>`；
4. 修改环境变量后执行 `systemctl --user daemon-reload && systemctl --user restart uniform-dgxspark.service`。

DeepSeek Harness CLI 的参数命名由该工具自身决定，平台只追加 `--model` 和末尾的任务说明；
其他开关请通过 `UNIFORM_DEEPSEEK_ARGS` 显式给出。思考强度对 Codex 和 DSH 原生会话生效。

### Hugging Face

在部署表单填写 `组织/模型名`，来源选择 Hugging Face，本地目录留空。私有或 gated 模型启动前设置：

```bash
export HF_TOKEN='你的 token'
```

### ModelScope

来源选择 ModelScope。私有模型使用：

```bash
export MODELSCOPE_API_TOKEN='你的 token'
```

### 本地模型

填写绝对路径。目录至少应包含 `config.json`、tokenizer 文件和 safetensors/bin 权重。

## 本地镜像

“本地镜像”页面直接读取 `docker image ls`，可按仓库、标签或镜像 ID 筛选，也可仅显示
名称中包含 vLLM 的候选镜像。点击“用于部署”会将完整 `repository:tag` 写入部署表单；
该操作不会启动或删除镜像。部署表单仍允许填写尚未下载的远程镜像。

## 本地模型

“本地模型”页面支持添加多个权重根目录。目录路径保存在平台数据库，不写死在代码中；也可在
首次启动前通过路径分隔符分隔的 `UNIFORM_LOCAL_MODEL_DIRS` 环境变量提供初始目录。平台只读
扫描每个根目录的直接子目录，解析 `config.json`、tokenizer、权重格式、量化方式、文件数量、
总大小及 safetensors 索引中缺失的分片。

点击“一键部署”会将模型绝对路径和扫描结果带入统一部署流程，不再重新下载已有完整权重。
Git LFS 指针、缺失分片或未完成权重会标为不可部署。移除目录只删除页面配置，不会删除或移动
任何模型文件。

## SSH 双机与 CX-7 监控

“系统监控”的“多机与 CX-7”区域可以登记 SSH 主机名或当前 IP、用户名和端口。平台使用
本机已有 SSH Key 或 ssh-agent，不保存密码，并要求远端主机指纹已经写入 `known_hosts`。

本机和远端每次采样都会重新枚举实际 hostname、IP、网卡、RDMA 设备、速率、MTU、流量、
错误计数和运行容器，因此界面不会假定两台机器使用相同网卡名。SSH 目标只是连接入口；推荐
填写稳定 DNS 名称或 SSH alias。若填写的是动态 IP 且该 IP 发生变化，需要更新节点登记。

同一节点卡片还会同步 CPU 利用率、系统统一内存、GPU 核心/内存控制器利用率、温度、功耗，
以及每个运行容器的 CPU 和 cgroup 内存。GB10 使用 CPU/GPU 共享统一内存，当前驱动不提供
GPU 独占 `memory.used`，因此页面显示共享池可用量并明确标注，不把系统内存使用伪装成显存。

端口状态区分“仅物理链路”“网络已配置”和“链路未建立”。`ACTIVE/LINK_UP`、carrier 和
200 Gb/s 只证明物理层完成协商；没有可用 IP 时不代表能参与双机通信。即使显示“网络已配置”，
页面仍标注“对端未验证”，因为当前只读监控不会主动发送探测流量。

采集器不会读取 Docker 容器命令或环境变量，以免服务 API Key 出现在监控结果。移除 SSH
节点只删除本地监控配置，不会停止远端服务。

## 网络代理

平台下载器继承标准环境变量：

```bash
export HTTP_PROXY='http://proxy:port'
export HTTPS_PROXY='http://proxy:port'
export NO_PROXY='localhost,127.0.0.1'
```

平台访问本地 vLLM 健康接口时会强制绕过代理。

## 数据位置

```text
data/uniform.db       任务、事件、模型目录和性能测试汇总
data/models/          下载后的模型快照
data/ai-sessions/     AI 会话工作目录、参考资料和终端记录
```

可以通过 `UNIFORM_DATA_DIR` 修改。删除缓存前必须先停止相关模型；不要在下载进行中删除模型目录。

## 推理性能测试

运行中的平台部署详情和已验证的 AI 部署结果都提供“性能测试”。平台自动读取本机服务端口并
查询 `/v1/models`，不会接受任意外部 URL 作为压测目标。可以配置 Chat Completions 或
Completions、近似输入 Token、最大输出 Token、并发数、正式请求数、预热请求数和单请求超时。

正式结果包含成功率、请求吞吐、输出与总 Token 吞吐、平均延迟、P50/P95/P99 和服务返回的
实际平均 Token 数。预热请求不进入统计。任务在后台运行，页面展示进度并支持取消、复跑和
持久化历史。平台不保存测试 Prompt 或响应正文。

“性能评测”是独立页面，只展示测试配置、记录和结构化结果；评测 AI 的原始 CLI 对话、续跑与
人工干预在“AI 会话”页面查看。自定义测试会把当前服务、部署参数、容器和硬件快照同步给 AI，
由 AI 从项目内置参考代码选择并调整实际脚本。Spark Arena 仍按固定口径执行，保证榜单可比。

## 发布模型到局域网和外网

运行中的托管部署和 AI 自主部署卡片都可以点击“发布到局域网”。首次发布会把模型登记到
`18181` 鉴权网关，并显示两项连接信息：

```text
Base URL: http://<局域网-IP>:18181/<publication-id>/v1
API Key:  udgs_...
```

这里的发布 Key 是首次连通网关的共享凭据，不建议直接发给多人长期使用。平台内置调用方、
独立 API Key、稳定模型范围、总额度、过期时间和请求用量记录；控制台不展示“网关配置”
页面，管理能力通过 `/api/v1/gateway/*` 提供。网关只开放模型列表、Chat/Completions、
Embeddings 和 Responses 等白名单推理端点，不会代理健康检查、指标或任意本机 URL。关闭
发布会立即拒绝新请求，并在流式响应的下一次数据检查时终止旧 Key；最后一个发布关闭后网关
停止监听。为避免绕过鉴权，AI 自主服务只有在原始 API 端口仅监听 loopback 时才能发布；若旧
部署绑定了 `0.0.0.0`，请先重新部署为 `127.0.0.1`。

网关默认对每把 Key 限制每分钟 60 次请求、每分钟 120000 token 和 4 个并发请求，并对全部
Key 限制 16 个并发请求。带 `max_tokens`、`max_completion_tokens` 或 `max_output_tokens`
的请求会在转发前原子预占额度，完成后按上游真实 usage 结算；流式 Chat/Completions 会自动
请求最终 usage。上游没有返回 usage 时按预占量记为估算值，避免额度被绕过。

完成局域网发布后，发布窗口会显示“外网访问”区域。未配置固定 Tunnel 时，点击“开启外网
访问”会启动受管的 Cloudflare Quick Tunnel，并给出类似下面的 HTTPS 地址：

```text
Base URL: https://<随机名称>.trycloudflare.com/<publication-id>/v1
API Key:  udgs_共享发布Key
```

外网流量从 Cloudflare 经出站 HTTPS 隧道进入本机 `127.0.0.1:18181`，不要求路由器开放入站
端口，也不会直接暴露模型监听端口。外网入口仍执行相同的 Bearer Key 校验、调用方权限、
限流、并发和额度检查以及 OpenAI 路径白名单。可在发布窗口中独立关闭外网入口；关闭局域网
发布会同时关闭对应外网入口，并立即停止该模型的全部代理请求。

Quick Tunnel 地址由 Cloudflare 临时分配，`cloudflared` 或平台重启后会变化。长期生产入口
应使用 Cloudflare Named Tunnel、固定域名和访问策略：在 Cloudflare 控制台创建 Tunnel，
把自有域名的服务指向 `http://127.0.0.1:18181`，然后为平台服务设置
`UNIFORM_CLOUDFLARED_TUNNEL_TOKEN` 和 `UNIFORM_EXTERNAL_PUBLIC_HOST`。平台通过环境变量
把 Tunnel token 交给 `cloudflared`，不会把 token 放入进程命令行。平台默认查找 PATH 中的
`cloudflared`，也会查找 `~/.local/bin/cloudflared` 和项目目录的 `.runtime/bin/cloudflared`。
外网地址默认每 15 秒探测一次；连续 2 次失败会重启 `cloudflared`，连续启动失败采用最长 60
秒的指数退避，重连次数、最近健康时间和下次重试时间会显示在发布窗口。

局域网入口仍是明文 HTTP，适合可信内网；外网调用应只使用上述 HTTPS 地址。如果机器有多个
网卡，可通过下面的环境变量指定局域网 Base URL 中显示的主机名或 IP。

## 常用环境变量

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `UNIFORM_HOST` | `127.0.0.1` | Web 监听地址 |
| `UNIFORM_PORT` | `18180` | Web 端口 |
| `UNIFORM_DATA_DIR` | `./data` | 数据库和模型缓存 |
| `UNIFORM_API_TOKEN` | 空 | 可选 API Key |
| `UNIFORM_PUBLICATION_HOST` | `0.0.0.0` | 模型发布网关监听地址 |
| `UNIFORM_PUBLICATION_PORT` | `18181` | 模型发布网关端口 |
| `UNIFORM_PUBLICATION_ADVERTISE_HOST` | 自动探测 | Base URL 中展示的局域网主机名或 IP |
| `UNIFORM_PUBLICATION_REQUEST_TIMEOUT_SECONDS` | `1800` | 发布网关单次推理请求超时 |
| `UNIFORM_PUBLICATION_CONTROL_HOSTS` | 自动探测 | 允许执行发布管理操作的额外 Host，逗号分隔 |
| `UNIFORM_EXTERNAL_ACCESS_ENABLED` | `true` | 是否允许创建 Cloudflare 外网入口 |
| `UNIFORM_CLOUDFLARED_BINARY` | `cloudflared` | `cloudflared` 可执行文件或绝对路径 |
| `UNIFORM_EXTERNAL_ACCESS_START_TIMEOUT_SECONDS` | `30` | 等待 Tunnel 就绪的超时 |
| `UNIFORM_CLOUDFLARED_TUNNEL_TOKEN` | 空 | Named Tunnel token；未设置时使用 Quick Tunnel |
| `UNIFORM_EXTERNAL_PUBLIC_HOST` | 空 | Named Tunnel 的固定 HTTPS 域名，例如 `https://models.example.com` |
| `UNIFORM_EXTERNAL_ACCESS_HEALTH_INTERVAL_SECONDS` | `15` | 公网可达性探测周期 |
| `UNIFORM_EXTERNAL_ACCESS_HEALTH_TIMEOUT_SECONDS` | `8` | 单次公网探测超时 |
| `UNIFORM_EXTERNAL_ACCESS_HEALTH_FAILURE_THRESHOLD` | `2` | 连续失败多少次后重启 Tunnel |
| `UNIFORM_GATEWAY_REQUESTS_PER_MINUTE` | `60` | 每把 API Key 每分钟请求上限，`0` 为关闭 |
| `UNIFORM_GATEWAY_TOKENS_PER_MINUTE` | `120000` | 每把 API Key 每分钟 token 上限，`0` 为关闭 |
| `UNIFORM_GATEWAY_KEY_CONCURRENCY` | `4` | 每把 API Key 同时进行的请求上限，`0` 为关闭 |
| `UNIFORM_GATEWAY_TOTAL_CONCURRENCY` | `16` | 网关全局同时进行的请求上限，`0` 为关闭 |
| `UNIFORM_GATEWAY_QUOTA_RESERVATION_TOKENS` | `1024` | 未声明最大输出时的请求前额度预占量 |
| `HF_TOKEN` | 空 | Hugging Face 凭证 |
| `MODELSCOPE_API_TOKEN` | 空 | ModelScope 凭证 |
| `UNIFORM_DOWNLOAD_WORKERS` | `4` | 文件下载并发 |
| `UNIFORM_STARTUP_TIMEOUT_SECONDS` | `600` | vLLM 启动超时 |
| `UNIFORM_CODEX_BINARY` | `codex` | Codex CLI 路径 |
| `UNIFORM_CLAUDE_BINARY` | `claude` | Claude Code CLI 路径 |
| `UNIFORM_DEEPSEEK_BINARY` | `deepseek` | DeepSeek Harness CLI 路径 |
| `UNIFORM_DEEPSEEK_ARGS` | 空 | 启动 DeepSeek Harness 时追加的参数，例如 `--auto-approve` |
| `DEEPSEEK_API_KEY` | 空 | DSH 原生会话访问 DeepSeek 官方模型使用的凭证 |
| `DEEPSEEK_BASE_URL` | DeepSeek 官方端点 | DSH 原生会话可选的兼容 API 基础地址 |
| `UNIFORM_AI_SESSIONS_ENABLED` | `true` | 是否允许启动 AI 终端 |
| `UNIFORM_AI_FULL_ACCESS_ENABLED` | `false` | 是否允许交互式 AI 会话使用完整宿主机权限 |
| `UNIFORM_UI_EDITING_ENABLED` | `true` | 是否允许右键 AI 修改前端界面 |
| `UNIFORM_BENCHMARK_HISTORY_CSV` | `~/Downloads/模型性能测试 - Sheet1.csv` | AI 自定义评测完成后追加的历史表格；仅接受现有表头格式 |
| `UNIFORM_ARENA_UVX_BINARY` | `uvx` | Spark Arena 使用的 `uvx` 可执行文件或绝对路径 |
| `UNIFORM_ARENA_UV_CACHE_DIR` | `~/.cache/uv` | Spark Arena 的 uv 下载缓存目录 |
| `UNIFORM_ARENA_UV_TOOL_DIR` | `~/.local/share/uv/tools` | Spark Arena 的 uv 工具安装目录 |
| `UNIFORM_AI_REFERENCE_MAX_BYTES` | `1000000` | 单个参考网页大小上限 |
| `UNIFORM_CATALOG_AUTO_REFRESH` | `true` | 是否在后台自动同步最新模型 |
| `UNIFORM_CATALOG_REFRESH_SECONDS` | `3600` | 自动同步周期（秒） |
| `UNIFORM_CATALOG_REFRESH_LIMIT` | `50` | 每个来源每次同步数量 |
| `UNIFORM_LOCAL_MODEL_DIRS` | 空 | 可选初始本地模型根目录，使用系统路径分隔符分隔 |
| `UNIFORM_SSH_MONITOR_SECONDS` | `5` | SSH 节点与 CX-7 指标刷新周期 |

## 可视化 UI 编辑

页面右下角点击“编辑 UI”进入编辑模式。移动鼠标会高亮当前元素，右键后可以填写自然语言批注，
并选择“当前元素、同一组件、当前页面或全局设计”作为修改范围。提交后不需要另开 AI 终端：
平台会创建隔离 Git 工作区并启动一个可续写的 Codex 会话。Codex 可以直接修改前端文件、执行
检查并根据结果继续修复；批注窗口会以对话时间线展示说明、命令、输出和最终结果。执行期间可以
随时补充要求、纠正方向或停止任务。

右下角“执行记录”会列出当前及历史 UI 修改。每条记录对应独立的隔离工作区和 Codex session；
新修改不会继承其他任务的完整对话，只会获得明确保留的回滚反馈。同一次修改中的自动修复、测试
迭代和人工追加要求则会通过 `resume` 延续同一个 session。选择任意历史记录后，可以查看可读的
CLI 风格对话时间线，也可以展开脱敏后的原始 Codex CLI JSONL 输出用于排查细节。

编辑工具栏适用于平台中所有用户可见页面、抽屉和弹窗。打开任意弹窗后，工具栏会自动进入最上层
弹窗：点击“编辑 UI”可右键选择弹窗内部组件，点击“截图批注”可捕获包含当前弹窗的完整画面并
圈选局部。正在填写修改要求的编辑表单会避免递归右键嵌套，但仍可通过截图批注修改。

对于仅靠 DOM 元素不容易表达的视觉问题，点击右下角“截图批注”，在浏览器授权窗口中共享当前
标签页，然后在完整页面截图上拖拽圈出关注区域并填写修改意见。平台会把原始全图、带圈选标注
的全图、圈选区域的高清局部图、区域坐标和附近 DOM 路径一起交给 Codex。局部框只用于强调关注
位置，AI 仍能看到完整页面上下文，从而兼顾局部修改与整体布局。截图保存在该次隔离编辑工作区，
不会写入平台数据库。

浏览器截屏 API 需要安全上下文；直接使用 `http://127.0.0.1:18180` 或 `localhost` 即可。通过
其他主机名或局域网 IP 访问时应配置 HTTPS。建议在共享选择器中选“当前标签页”；如果拒绝授权、
取消共享或选择了其他窗口，页面会显示截屏失败原因，可以重新发起截图。

检查结果是提供给 AI 的修复反馈，不会因为普通语法或样式警告阻止应用；警告会随结果一起展示。
页面入口保护、项目目录边界、源码并发冲突和密钥保护仍属于底线。一次用户编辑中的所有 AI 内部
迭代最终只生成一个 Git 提交。右下角“撤销上次修改”会整体恢复到这次编辑之前，并要求填写回滚
原因；原因会作为后续 Codex 修改的可信反馈上下文。

该功能要求 Codex CLI 已安装并登录。设置 `UNIFORM_UI_EDITING_ENABLED=false` 可以完全关闭入口
对应的写入 API；若平台暴露到局域网，应同时设置 `UNIFORM_API_TOKEN`。

## 模型目录与在线搜索

模型目录有两条数据路径：不输入关键词时读取本地缓存；输入关键词并点击“在线搜索”时，
服务端优先查询 Hugging Face，失败时自动回退到 ModelScope。也可以在来源下拉框中明确只
查询一个站点。

两站中模型名相同且没有同名歧义的条目会合并成一张卡片。卡片保留每个来源的模型 ID、revision、
下载量和点赞数，并优先采用 Hugging Face 条目进行卡片展示。AWQ、GPTQ、GGUF 等名称不同的
量化版本不会合并。这些合并细节不会显示在页面上。卡片头像来自模型发布组织；远端头像不可用
时自动显示发布者缩写。为保证国内下载速度，部署时若存在对应 ModelScope 条目仍优先从
ModelScope 拉取权重；只有 Hugging Face 条目的模型则直接使用 Hugging Face。

检索结果可按近期热度、发布时间、下载量、点赞数或模型名称排序。发布时间优先使用站点提供的
创建时间；旧缓存没有创建时间时使用最后更新时间，仍缺失则显示“时间未知”。

每张模型卡片显示权重下载大小、仓库总量、量化/精度、模型结构和参数量。卡片进入可视区域后
会读取实际文件清单：标准分片权重求和；多个 GGUF 量化文件显示单文件大小范围，不错误合计为
一个模型。详情读取前可以短暂显示参数估算值并标记“约”，随后替换为实际文件统计。接口确实
没有提供的信息显示“未知”，平台不会猜测参数量。

后台默认每 60 分钟自动同步每个站点热度最高的 50 个模型，页面显示上次同步时间。需要调整时
设置 `UNIFORM_CATALOG_REFRESH_SECONDS` 和 `UNIFORM_CATALOG_REFRESH_LIMIT`；“立即同步”
仅用于希望马上更新热门排行的场景。Hugging Face 使用官方 `trending_score`，ModelScope
目前没有近期趋势指标，因此使用官方 `downloads` 作为其热度依据。同步只采集适合本平台部署的
文本生成与图文生成模型，避免语音、OCR 等全站下载榜条目占用同步名额后又被默认筛选隐藏。
手动同步完成后页面会强制读取最新缓存，并分别提示每个来源的成功情况。

## AI 部署与调试

### 从模型卡片一键部署

进入“模型中心”，在目标卡片点击“一键部署”。平台优先复用该模型最近一次已验证模板；没有
模板时创建新的部署 AI 会话，并读取目标模型、本地镜像清单和参考资料。教程明确推荐的推理
引擎、镜像和版本在通过安全与兼容性检查后具有最高优先级；系统不会强制使用 vLLM、固定版本
或本地镜像。模型中心默认按单机部署，不会仅因系统登记了其他节点就核验 SSH/RDMA/NCCL；
只有明确选择多机拓扑时才加载多机教程。

每个 AI 会话都可以填写独立名称、AI 模型和思考强度。模型卡片入口默认使用目标模型的短名
作为会话名称；会话列表同时保留完整目标模型 ID，便于区分多个部署。AI 模型留空时沿用 CLI
默认配置，也可以从常用候选中选择或直接填写模型 ID。思考强度仅对 Codex 生效，并以会话级
参数传入，不修改用户的全局 Codex 配置。

AI 会话窗口提供“精简对话”和“原始终端”两种视图。精简视图呈现用户消息、AI 最终答复和
等待状态，并隐藏工具调用、ANSI 控制符和运行日志；DSH 原生会话还会实时显示可折叠的思考卡片，
正式回答出现后自动收起。你可以直接在输入框中继续追问，需要观察命令执行、处理审批或排查底层输出时，
可随时切回原始终端。精度评测页面的数据集接入
对话使用同一套视图切换和输入方式，确认方案、真实试跑与启用仍由平台按钮控制。

已结束或已停止的会话可以存档，存档会话默认隐藏，勾选“显示已存档”后可以查看和恢复。
删除会永久移除该会话的终端记录、参考资料和独立工作目录，但不会删除关联部署、外部模型
目录或 Docker 容器。运行中的会话必须先停止，不能直接存档或删除。

选择 vLLM 时，AI 通过会话目录中的 `uniformctl` 提交完整受管任务：

```text
uniformctl deploy → 权重下载 → 镜像拉取 → vLLM 启动
                  → 健康检查 → OpenAI-compatible 推理测试
```

终端会显示 AI 的分析和 `uniformctl wait` 进度；部署中心同时显示任务阶段，详情页显示事件、
日志和 CPU/GPU/统一内存指标。失败时 AI 可执行 `uniformctl status`、`uniformctl logs`，说明
原因后调整镜像或参数并重试。

选择其他推理引擎时，AI 按已验证的教程在审批终端中执行，镜像拉取、构建、启动日志和测试
结果都显示在终端，系统页仍显示主机 CPU/GPU/统一内存。由于不同引擎没有统一状态接口，
当前非 vLLM 部署不会获得部署中心的标准阶段卡片；AI 必须执行该引擎对应的健康检查和真实
推理测试后才能报告成功。

无论使用哪个引擎，AI 终端下方都会显示同一份“部署结果”：容器名/ID、镜像、端口、服务
地址、启动命令、健康检查和推理测试。vLLM 的 `uniformctl wait` 会在受管任务运行后自动登记；
其他引擎由 AI 按会话目录中的 `RESULT_SCHEMA.md` 执行
`./uniformctl report --file deployment-result.json`。只有健康检查与推理测试都通过，平台才将
结果标记为“已验证”，该状态由服务端计算，不接受 AI 直接指定。

该能力需要本机已安装并登录 Codex CLI 或 Claude Code CLI。真实部署会消耗对应 AI 额度，
也会占用下载流量、磁盘和 GPU 资源。

### AI 执行模式

- “自主部署”是模型卡片的默认模式。Codex 可以联网，并用 Auto-review 自动处理需要越过
  workspace 沙箱的常规请求；任务说明要求它持续调试到真实推理成功或遇到真正阻塞。
- “交互审批”保留逐条确认，适合不熟悉的教程。
- “完整宿主机权限”关闭 Codex 命令沙箱和逐条审批，必须明确勾选风险确认。它仅受当前操作
  系统用户权限限制，可能访问或修改该用户可访问的所有数据。该模式默认由服务端禁用；只有
  在可信宿主机上显式设置 `UNIFORM_AI_FULL_ACCESS_ENABLED=true` 并重启服务后才可选择。

即使使用自主模式，宿主机 sudo、删除既有权重、操作无关容器、安全配置和凭证访问仍应由
用户决定。平台不会存储 sudo 密码。

### 安全与普通诊断

打开“AI 调试”，选择 Codex 或 Claude Code，可以关联某个部署并填写最多五个
GitHub/网页参考链接。平台会把部署参数、最近的 vLLM 日志和参考资料写入独立会话目录，
然后启动网页终端。关闭浏览器不会立即停止 CLI；重新打开同一会话可恢复完整终端记录。

Codex 默认使用 `workspace-write` 沙箱和 `untrusted` 审批策略；Claude Code 使用默认权限
审批模式。参考网页、模型卡和日志都按不可信数据处理。Codex 已安装时自动显示可用；
Claude Code 需要用户按其官方方式安装并完成登录，刷新页面后即可使用。

平台不接受、不保存 sudo 密码。若某个已审批命令确实需要 sudo，只能在网页终端的
sudo 提示中由用户手工输入。平台审计不记录终端按键；sudo 默认也关闭密码回显。
但终端输出会持久化，所以不要让命令主动打印密码或 token。

部署详情中的“使用 AI 诊断此部署”会自动附带失败状态、配置和日志。模型卡片的一键部署
会获得只对该模型有效的临时 capability；AI 退出或服务重启后立即失效。外部教程不会直接
获得执行权，也不能借此更换目标模型或读取平台下载 token。

## 完整验收

```bash
./scripts/accept-all.sh
```

该命令会真实启动测试容器和访问模型站点，耗时取决于网络。最终应显示：

```text
PASS: all stage acceptance checks succeeded.
```
