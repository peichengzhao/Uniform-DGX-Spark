# 阶段 34：评测入口、会话恢复与实时日志

## 验收目标

- “性能评测”成为唯一导航入口，Spark Arena 是主界面，自定义测试折叠为辅助工具。
- 选中榜单基线后，在运行前说明本地参数与榜单参数是否匹配。
- 停止或已退出的 AI 会话可以从原上下文继续。
- 服务日志每秒更新；WebSocket 异常时自动切换 REST 轮询，并默认隐藏健康检查噪声。

## 自动化测试

```bash
cd /home/zaki/Downloads/uniform_dgxspark
uv run ruff check src tests
node --check src/uniform_dgxspark/web/app.js
uv run pytest -q
```

预期：静态检查通过，全量测试 71 项通过。

## 页面验收

1. 强制刷新 <http://127.0.0.1:18180>。
2. 左侧应只有一个“性能评测”，页面上部是 Spark Arena，页面底部是折叠的
   “自定义并发测试（辅助工具）”。
3. 搜索并选择一个榜单模型；页面应立即列出榜单运行时、节点数、可用参数组合，
   并明确显示“参数完全匹配”或逐项列出 pp、tg、depth、并发差异。
4. 打开“AI 调试”中的 stopped/exited 会话；应出现“继续会话”。点击后状态变为
   running，原终端记录保留，AI 收到“继续执行上次任务”。
5. 从部署中心点击服务“日志”，保持窗口打开并发起一次推理请求；状态时间应每秒刷新，
   页面应出现 `POST /v1/chat/completions`。取消“隐藏健康检查”可查看 `/health` 请求。

## 接口验收

```bash
curl -s http://127.0.0.1:18180/api/v1/arena/leaderboard/sub1788113641803 \
  | jq '.runtime,.cluster_size,.presets[] | select(.depth == 16384 and .concurrency == 1)'

curl -s 'http://127.0.0.1:18180/api/v1/service-logs/ai-session/138e254e-d50e-4466-9726-aaaaae90100e?tail=1000' \
  | jq -r '.logs' | grep 'POST /v1/chat/completions'
```

第二条命令中的会话 ID 应替换为页面当前服务对应的实际 ID。
