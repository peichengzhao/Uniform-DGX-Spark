# Uniform DGX Spark 平台

把 DeepSeek Harness 连接到运行在 DGX Spark 上的 Uniform 模型平台。安装后可以查询模型与部署、启动或停止部署、发布局域网服务，以及管理模型外网入口。

插件只调用平台 HTTP API，不直接执行 Docker 命令，也不会把平台 API Key 写进插件包。
