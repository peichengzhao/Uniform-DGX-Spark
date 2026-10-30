# 阶段 7 验收：ModelScope、安全与本地发布

## 已实现

- ModelScope 官方 Hub 客户端的最新模型目录与权重下载。
- 可选 `UNIFORM_API_TOKEN`，使用恒定时间比较保护全部 API。
- Web UI 在启用 API Key 时提示输入，并改用带认证的轮询。
- `trust_remote_code` 必须同步提交显式风险确认。
- 禁止通过额外参数覆盖平台管理的 host、port、model 等参数。
- 默认只监听 `127.0.0.1`。
- Hugging Face/ModelScope token 只从环境读取，不写数据库。
- 可复现 `uv.lock`、一键启动脚本和可选 user-systemd 安装脚本。

## 自动验收

```bash
./scripts/accept-stage-7.sh
```

脚本会验证无 Key 返回 401、正确 Key 返回 200，并真实刷新 5 个 ModelScope 最新模型。

预期：

```text
PASS: stage 7 acceptance checks succeeded.
```

## 人工验收

启用认证启动：

```bash
UNIFORM_API_TOKEN='自行生成的长随机值' ./scripts/run-local.sh
```

打开页面后应出现 API Key 输入框。错误 Key 不能读取任务。

安装为用户服务是可选的，会修改用户 systemd 配置，因此项目不会自动执行：

```bash
./scripts/install-user-service.sh
```

安装后测试：

```bash
systemctl --user status uniform-dgxspark.service
curl http://127.0.0.1:18180/api/v1/health
```
