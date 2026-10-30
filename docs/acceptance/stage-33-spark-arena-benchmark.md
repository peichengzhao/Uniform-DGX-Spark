# 阶段 33：Spark Arena 对标评测

## 验收目标

使用 `llama-benchy 0.4.0` 对已运行的 OpenAI 兼容服务执行可复现评测，并与
Spark Arena 榜单的相同 `pp`、`tg`、`depth`、并发口径比较。

## 自动化测试

```bash
cd /home/zaki/Downloads/uniform_dgxspark
uv run ruff check src tests
uv run pytest -q tests/test_arena.py
uv run pytest -q
node --check src/uniform_dgxspark/web/app.js
```

预期：Ruff 与 Node 检查无错误，Arena 专项测试 2 项通过，全量测试全部通过。

## 页面验收

1. 打开 <http://127.0.0.1:18180>，进入“Spark Arena 评测”。
2. 确认运行中的托管部署和已验证 AI 部署出现在“本地模型服务”列表。
3. 输入 Tokenizer 路径；搜索模型名并选择 Spark Arena 榜单基线。
4. 首次验收建议使用小参数：`pp=8`、`tg=2`、`depth=0`、`runs=1`、并发 `1`，
   关闭 Prefix Cache。这只验证链路，不作为性能成绩。
5. 点击“开始正式评测”，确认立即出现等待/运行记录，可查看进度和实时日志。
6. 完成后确认展示 Prefill、Decode、峰值 Decode、TTFR、估算 Prefill 时间和 TTFT，
   并能下载原始 JSON。
7. 正式对标时恢复默认：`pp=2048`、`tg=128`、`depth=16384`、`runs=7`、并发 `1`，
   开启 Prefix Cache。跑分期间不要启动资源采样，也不要同时发送其他推理请求。

## 接口验收

```bash
curl -s http://127.0.0.1:18180/api/v1/arena/leaderboard?query=GLM | jq '.total,.items[0]'
curl -s http://127.0.0.1:18180/api/v1/arena/runs | jq '.'
```

运行创建接口需要填写页面实际目标 ID，因此建议从浏览器发起；随后可用下面两个接口观察：

```bash
curl -s http://127.0.0.1:18180/api/v1/arena/runs/<RUN_ID>/logs | jq -r '.logs'
curl -OJ http://127.0.0.1:18180/api/v1/arena/runs/<RUN_ID>/result
```

## 结果解释

- Prefill throughput：输入处理吞吐，越高越好。
- Decode throughput：生成吞吐，越高越好。
- TTFR：收到首个响应块所需时间，越低越好。
- 页面差距为正数表示本地更优；TTFR 的差距已按“越低越好”换算。
- API Key 只保存环境变量名；实际值不会写入数据库、复现命令或评测日志。
