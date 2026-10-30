# 阶段 12：部署关联诊断

自动测试：`./scripts/accept-stage-12.sh`

人工测试：打开一个失败部署，点击“使用 AI 诊断此部署”。确认表单自动选中部署，启动后
Codex 能读取 `deployment-context.json`、`vllm.log` 和安全策略，并在执行修改前请求确认。
