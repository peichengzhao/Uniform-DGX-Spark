# 阶段 9：Codex 与参考链接

自动测试：`./scripts/accept-stage-9.sh`

人工测试：新建 Codex 会话，添加一个公开 GitHub README 链接。确认会话目录的
`references/` 和 `REFERENCES.md` 已生成，并在终端询问 Codex 总结资料。填写
`http://127.0.0.1` 应被拒绝并在审计中记录失败，不能访问本机服务。
