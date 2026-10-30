# 最终验收汇总

- 日期：2026-09-01
- 平台阶段：0～35 全部完成
- 自动化测试：78 passed
- Ruff 静态检查：通过
- Python 编译检查：通过
- Shell 语法检查：通过
- JavaScript 语法检查：通过
- 依赖锁文件检查：通过

## 完整集成结果

```text
Stage 2: 真实 NVIDIA vLLM 容器启动、推理、指标、停止通过
Stage 3: 固定 revision 的 Hugging Face 在线下载与二次缓存命中通过
Stage 4: Web 页面、静态资源和 SSE 通过
Stage 5: GB10、统一内存、CPU、磁盘和历史采样通过
Stage 6: Hugging Face 在线刷新通过
Stage 7: ModelScope 在线刷新、API Key 与安全校验通过
Stage 8: 持久化 PTY、WebSocket 网页终端、重连与审计通过
Stage 9: Codex CLI、外部参考链接安全获取、本地 xterm.js 通过
Stage 10: Claude Code 适配与安装状态检测通过
Stage 11: 命令审批、环境凭据隔离和 sudo 不落盘策略通过
Stage 12: 部署上下文、vLLM 日志与一键 AI 诊断联动通过
Stage 13: Docker 镜像拉取实时输出与取消测试通过
Stage 14: Hugging Face/ModelScope 全站实时搜索、缓存回退与后台自动同步通过
Stage 15: 本地 Docker 镜像清单、筛选与部署表单联动通过
Stage 16: 模型卡片 AI 一键部署、会话级权限、参数校验与推理测试编排通过
Stage 17: 可配置本地模型目录、元数据扫描、完整性检查及本地部署联动通过
Stage 18: AI 自主部署、Auto-review、联网执行与完整权限确认通过
Stage 19: AI 容器、镜像、端口、启动命令、健康检查与推理测试统一结果通过
Stage 20: SSH 双机节点、动态 IP、CX-7/RDMA 和容器只读监控通过
Stage 21: ModelScope 优先联合模型库、跨站去重、HF 备用来源和发布厂商头像通过
Stage 22: 检索结果五种排序、Hugging Face 优先展示和合并信息隐藏通过
Stage 23: 模型卡片大小、量化/精度、模型结构和参数量展示通过
Stage 24: 实际权重分片、仓库总量、GGUF 多变体和 NVFP4 识别修正通过
Stage 25: RDMA 物理链路、网络配置和对端可达性状态分离通过
Stage 26: AI 会话命名、目标模型关联、AI 模型、思考强度、存档和删除通过
Stage 27: AI 限流/断流自动续跑、重试次数和退避配置通过
Stage 28: AI 会话创建即时等待反馈、重复提交保护和幂等创建通过
Stage 29: 托管与 AI 容器实时服务日志、请求摘要和鉴权通过
Stage 30: 同模型历史 AI 部署会话查询、原生恢复和新建选择通过
Stage 31: 双机 CPU、统一内存、GPU 与容器资源同步监控通过
Stage 32: 可视化并发推理测试、持久化指标、取消复跑和 AI 分析通过
Stage 33: Spark Arena 标准口径评测、原始结果和榜单对比通过
Stage 34: 评测导航、实时日志与参数匹配体验通过
Stage 35: 右键 AI UI 批注、隔离修改、自动刷新、冲突保护和撤销通过
PASS: all stage acceptance checks succeeded.
```

阶段 3 的真实测试下载了 11 个文件、约 16 MB，网络环境下耗时约 4 分 24 秒。缓存二次读取没有重新下载。

最终持久化服务已启用 ModelScope 与 Hugging Face 后台同步；缓存条目会随自动刷新动态变化，
联合列表在读取时实时去重，不以两个来源条目数之和作为页面模型数。

本地模型目录 `/home/zaki/model` 已通过页面 API 添加到数据库。只读扫描识别到 3 个完整模型，
合计约 471 GiB：GLM-5.3-Flash-NVFP4、GLM-5.3-Flash-tr3-4bpw 和
Qwen3.8-Flash-Next-NVFP4；safetensors 索引检查未发现缺失分片。

## 用户复验

完整复验：

```bash
./scripts/accept-all.sh
```

也可以按需运行：

```bash
./scripts/accept-stage-2.sh
./scripts/accept-stage-3.sh
./scripts/accept-stage-4.sh
./scripts/accept-stage-5.sh
./scripts/accept-stage-6.sh
./scripts/accept-stage-7.sh
./scripts/accept-stage-8.sh
./scripts/accept-stage-9.sh
./scripts/accept-stage-10.sh
./scripts/accept-stage-11.sh
./scripts/accept-stage-12.sh
./scripts/accept-stage-13.sh
./scripts/accept-stage-14.sh
./scripts/accept-stage-15.sh
./scripts/accept-stage-16.sh
./scripts/accept-stage-17.sh
./scripts/accept-stage-18.sh
./scripts/accept-stage-19.sh
./scripts/accept-stage-20.sh
./scripts/accept-stage-21.sh
./scripts/accept-stage-22.sh
./scripts/accept-stage-23.sh
./scripts/accept-stage-24.sh
./scripts/accept-stage-25.sh
./scripts/accept-stage-26.sh
./scripts/accept-stage-27.sh
./scripts/accept-stage-28.sh
./scripts/accept-stage-29.sh
./scripts/accept-stage-30.sh
./scripts/accept-stage-31.sh
./scripts/accept-stage-32.sh
./scripts/accept-stage-35.sh
```

每个阶段的人工测试步骤见同目录对应报告。

## 生产使用前建议

- 部署大型模型前确认至少保留足够的统一内存和磁盘余量。
- 暴露到局域网前设置 `UNIFORM_API_TOKEN`，并在外部增加 TLS 反向代理。
- `trust_remote_code` 只对可信仓库启用。
- 对关键模型填写明确 revision；ModelScope 默认 `master` 不具备不可变保证。
- 当前是单机单 worker 调度，适合一台 DGX Spark，不是多节点集群调度器。
- Codex 已检测可用；Claude Code 需要单独安装并完成登录。
- sudo 密码不支持保存。必要时只能在受控终端中由用户手工输入并审批操作。
