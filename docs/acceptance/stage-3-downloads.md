# 阶段 3 验收：可靠下载与缓存

## 已实现

- Hugging Face `snapshot_download` 与 Xet/HTTP 官方恢复机制。
- ModelScope 官方 `modelscope-hub` 下载器。
- 下载目录文件锁，防止相同模型并发重复下载。
- 未完成文件保留，任务重试时继续下载。
- Hugging Face branch/tag 自动解析为不可变 commit SHA。
- 完成后校验 `config.json` 与权重文件，并原子写入完成标记。
- 再次部署相同 revision 直接命中缓存。
- 下载任务独立于浏览器连接，关闭页面不会中断。

## 自动验收

```bash
./scripts/accept-stage-3.sh
```

脚本先模拟不完整目录恢复，再真实下载固定 revision 的约 16 MB 测试模型两次；第二次必须命中缓存。

预期：

```text
PASS: stage 3 acceptance checks succeeded.
```

## 人工断网测试

1. 从 Web 页面提交一个未缓存的 Hugging Face 模型。
2. 状态进入“下载模型”后短暂断开网络。
3. 恢复网络；若官方下载器仍在重试，任务会继续。若重试已耗尽，任务会进入失败。
4. 对失败任务点击“重试”，已下载文件不得从零重新下载。
5. 下载完成目录中应存在 `.uniform-download-complete.json`。

ModelScope 在未填写 revision 时使用 `master`；平台会记录完成标记，但 ModelScope 当前公开元数据不提供等价于 Hugging Face commit SHA 的不可变版本。需要严格复现时应显式填写 tag/revision。

