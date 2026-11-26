# gpudock-core

GPUDock 的核心库：通过官方 Colab API（`colaboratory.googleapis.com/v1beta`）创建/释放运行时，并经由运行时代理使用标准 Jupyter 协议执行代码、传输文件。

当前处于 M0 技术验证阶段，命令行工具是 `gpudock-m0`，使用说明见 [../../docs/m0.md](../../docs/m0.md)。

| 模块 | 作用 |
|---|---|
| `auth.py` | Google 登录：BYO OAuth 桌面客户端（PKCE + 本地回调）或 gcloud ADC；令牌存系统钥匙串 |
| `colab_api.py` | runtimespecs / subscription / runtimes / operations |
| `jupyter.py` | 运行时 Jupyter REST + kernel WebSocket（JSON 协议） |
| `files.py` | contents API 分片上传、`/files/` 断点续传下载 |
| `m0/` | 技术验证实验与结果记录 |
