# 阶段 6 验收：Hugging Face 模型目录

## 已实现

- 手动刷新最近更新的 Hugging Face 模型。
- 目录写入 SQLite，页面刷新后无需重新抓取。
- 模型 ID 搜索、来源和兼容级别筛选 API。
- 下载量、任务、库、tag、gated/private 等元数据。
- `likely`、`experimental`、`incompatible` 初步分类。
- 从目录卡片直接预填部署表单。

目录分类只是快速筛选，部署前仍由本地 `config.json`、权重和内存检查决定是否继续。

## 自动验收

```bash
./scripts/accept-stage-6.sh
```

该脚本会真实刷新 10 个 Hugging Face 最新模型。

预期：

```text
PASS: stage 6 acceptance checks succeeded.
```

## 人工验收

1. 打开“模型目录”，点击“刷新目录”。
2. 应同时看到来源、兼容标签、下载量和模型 ID。
3. 输入模型名称进行搜索。
4. 点击“配置部署”，部署表单的模型 ID 应自动填写。

