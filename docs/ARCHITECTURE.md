# 架构说明

```text
Browser UI
  ├─ REST API / SSE / authenticated WebSocket
  ▼
FastAPI control service
  ├─ SQLite task + event store
  ├─ persistent deployment state machine
  ├─ unified model catalog
  │    └─ Hugging Face preferred + ModelScope fallback + publisher avatars
  ├─ resumable model cache
  ├─ Docker vLLM runtime
  ├─ AI session store + PTY/native manager
  │    ├─ DSH native Session through deepseek-harness-sdk (stdio JSON-RPC)
  │    ├─ Codex CLI (workspace-write + untrusted approvals)
  │    ├─ Claude Code CLI (default permission mode)
  │    ├─ DeepSeek Harness CLI (operator-supplied flags)
  │    └─ session-scoped uniformctl capability
  ├─ verified deployment-template store
  │    └─ running service / managed vLLM reuse / AI-validated fallback
  ├─ opt-in publication gateway (:18181)
  │    ├─ hashed API keys + OpenAI endpoint allowlist
  │    └─ managed Cloudflare Quick Tunnel (outbound HTTPS)
  │         └─ public URL → 127.0.0.1:18181
  ├─ visual UI edit manager
  │    └─ isolated frontend copy → Codex patch → validation → atomic apply
  ├─ safe external-reference fetcher
  ├─ persistent concurrent inference benchmark runner
  └─ local/SSH system, GPU, container and vLLM metrics
        ▼
NVIDIA GB10 + unified memory
```

后台 worker 与浏览器生命周期解耦。浏览器关闭不会停止任务。模型先下载并校验到本地缓存，vLLM 容器只挂载完整快照，避免在容器启动阶段混合下载和推理初始化。

模型目录以规范化模型名匹配跨站重复项；仅当每个来源各有一个候选时跨发布者合并，存在同名
歧义时保留独立条目。合并记录完整保留来源引用，主对象优先 Hugging Face；在线联合搜索也先
请求 Hugging Face，失败时回退 ModelScope。合并关系只用于内部选择，不在页面展示。排序在
后端去重之后执行，保证热度、发布时间、下载量、点赞数和名称使用相同语义。发布者图片代理只
允许 Hugging Face 和 ModelScope 的受控 CDN，失败时返回 SVG。

目录规范化层从 Hugging Face `config`/`safetensors` 与 ModelScope 仓库元数据中提取模型架构、
模型类型、参数量和量化信息。实际大小由按需详情 API 读取文件清单：普通权重分片求和，GGUF
多变体保留各文件大小。结果写回目录缓存，最多三个详情请求并发，且不与搜索 API 共用网络锁。
这些请求只读取文件元数据，不下载权重内容。

每个部署状态变化写入事件表。服务启动时将中断的准备阶段重新排队，使用确定性容器名清理残留；标记为运行中的任务会与 Docker 和健康接口对账。

AI 会话运行于 `data/ai-sessions/<session-id>`，只继承必要环境变量，不继承平台 API Key、
Hugging Face 或 ModelScope token。PTY 输出同时发送到网页并持久化，浏览器断线后可以重放。
WebSocket 必须再次提供平台 API Key。外部 URL 拒绝 loopback、私网和 link-local 地址，
限制内容类型和体积，并在提示中明确视为不可信资料。

模型卡片创建 AI 部署会话时，控制服务只在内存中生成一次性 capability，并将目标模型写入
只读上下文。AI 一键部署的受权通道不暴露 Docker 或下载器，而是通过会话工作目录中的
`uniformctl` 请求平台创建该模型的受管任务。平台 worker 继续负责可恢复下载、镜像拉取、
容器生命周期、健康检查和推理 smoke test。capability 不替代平台 API Key，也不允许更换
模型或管理无关容器；AI 进程退出或服务重启后 token 即失效。

AI 成功完成部署后必须写入一份会话级统一结果。受管 vLLM 任务由平台从实际 Docker 容器和
已完成的 smoke test 自动生成；其他引擎由 AI 通过 `uniformctl report` 提交同一 JSON 契约。
SQLite 只保留每个会话的最新结果并记录每次提交审计。总体状态由服务端根据健康检查与推理
测试是否都通过计算，网页不依赖终端自然语言判断部署是否成功。

已验证结果会派生出不含明文凭证的部署参考，以及分别带 SHA-256 的启动、停止工件。单机
Docker 服务的工件绑定完整容器 ID、镜像和名称；平台只有在完成一次停止、启动、健康检查与
真实推理后才允许直接执行。模型中心再次请求同一模型时先核验并复用仍健康的服务；受管 vLLM
可重放结构化 `DeploymentParameters`。容器被替换、工件篡改、复杂多机或其他环境变化会优先
交回原 AI 会话，原会话不可用时再创建携带持久化备份和失败原因的新会话。

模型发布与控制面分离。只有用户显式发布服务后，独立网关才监听局域网端口。发布记录负责
公开模型与 Base URL；访问权限由“网关配置”中的调用方和独立 Token 管理。每把 Token 绑定
允许访问的稳定模型身份，而不是单次 publication ID，因此同一模型停止并重新部署后权限仍
可复用；Token 还可设置总额度与过期时间。Token 明文只在创建时返回，SQLite 只保存 SHA-256、
掩码和累计用量。网关按 publication ID 解析本机已登记且实时运行的目标，
只允许有限 OpenAI 推理路径，不转发外部 Authorization 到上游，也不接受任意目标 URL。
AI 自主服务发布前还会验证原始 API 端口只绑定 loopback，避免绕过网关鉴权；请求体采用有界
流式读取，流式响应期间会复核 Token、调用方和发布状态。每次请求记录调用方、模型、端点、
状态码、延迟及上游真实返回的 token usage；未返回 usage 时标记为不可用，不做伪造估算。
旧共享发布 Key 在迁移期仍可作为兼容凭据，并可在每个发布上单独停用。调用方可软停用，也可
永久删除；永久删除会级联移除其 Token，但请求用量审计记录保留。关闭最后一个发布后网关
随即停止。

外网访问不要求开放入站端口：平台按需启动一个受管 `cloudflared` Quick Tunnel，通过出站
HTTPS 反向连接到 `127.0.0.1:18181`。公网请求仍经过同一 publication ID、调用方独立 Key 和
OpenAI 路径白名单，因此内外网不需要各自维护一套凭据。外网入口与局域网入口可独立开关；
撤销局域网发布会同时关闭对应外网入口。Quick Tunnel 域名是临时的，进程重启后会变化；
固定域名应改用 Cloudflare Named Tunnel 和用户自有域名。

系统监控的远端节点使用持久化的 SSH 连接配置，但采集结果不依赖固定 hostname、IP、网卡、
RDMA 设备或容器名称。控制服务每次通过固定 Python 只读探针重新枚举实际资源，并并发刷新
所有节点。SSH 强制公钥批处理模式和 `known_hosts` 校验；探针不读取 Docker command/env，
避免将推理服务 API Key 带入监控结果。节点离线只影响该节点，不阻塞本机和其他节点。
网络探针将 RDMA ACTIVE、物理 LINK_UP、Linux operstate 和非 link-local IP 分层建模；物理载波
与网络配置分别呈现。`peer_reachable` 在未执行主动探测时保持 `null`，避免把未知误报为可达。

性能测试器只从已运行的托管部署或已验证 AI 结果解析本机端口，拒绝任意 URL，从而避免把
控制面变成 SSRF/通用压测器。后台 daemon worker 按固定参数并发调用 OpenAI-compatible 接口，
SQLite 只保存参数、进度、错误摘要和聚合指标，不保存 Prompt 或响应正文。AI 分析接收同一份
聚合结果，不参与实际负载生成，因此不同上下文长度和并发测试保持可比较。

可视化 UI 编辑器与部署 AI 会话相互独立。浏览器只提交选中元素的稳定 UI 标识、DOM 路径、
有限 HTML/文本、计算样式和用户批注；页面数据明确按不可信输入处理。编辑管理器把三个现有
前端文件复制到任务专属目录，并在嵌套 Git 工作区内运行只读、非交互 Codex。Codex 只返回
结构化的精确文本替换，由平台应用到候选副本；修改只能落到 `index.html`、`app.css` 和
`app.js`。候选文件通过 UTF-8、文件大小、应用外壳和 JavaScript 语法检查后，
还必须确认源文件哈希未变化，才以原子替换方式应用。原始文件随任务保留，撤销操作只有在当前
文件仍与该任务应用结果一致时才执行，避免覆盖后续人工修改。
