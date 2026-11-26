# 参与贡献 · Contributing

欢迎提 Issue 和 PR！下面是本地开发需要知道的事。

## 开发环境

- Python：[uv](https://docs.astral.sh/uv/)（会自动准备 Python 3.12）
- 前端：Node.js 20+
- 测试和配音流水线需要 `ffmpeg`

```bash
cd apps/web && npm install && npm run build
```

```bash
cd packages/core && uv sync && uv run pytest -q
```

前端热更新：先运行 `uv run gpudock --dev`，再在 `apps/web` 里 `npm run dev`。

登录需要一个 Google OAuth "桌面应用"客户端，见 [docs/m0.md](docs/m0.md) 的“维护者一次性配置”，配置后运行一次 `uv run gpudock-m0 setup-client <json>`。

## 代码结构

| 位置 | 作用 |
|---|---|
| `packages/core/src/gpudock_core/colab_api.py` | 官方 Colab API 客户端 |
| `packages/core/src/gpudock_core/jupyter.py` | 经运行时代理的 Jupyter REST + 内核 WebSocket |
| `packages/core/src/gpudock_core/agent/gpudock_agent.py` | 上传到 Colab 运行时里执行的 agent：模型部署、转写、分离、合成、翻译、混音 |
| `packages/core/src/gpudock_core/server/` | 本地 FastAPI 服务：登录、运行时、任务编排 |
| `apps/web/` | React + TypeScript 界面 |

## 界面图片

界面里的产品图、功能插图（`apps/web/public/img/*.webp`）由 `scripts/gen_images.py` 调用 OpenAI 图像接口生成，统一使用“暖白纸背景、无品牌硬件、不含文字”的风格。缺少图片时，界面会自动退回线稿图标。

```bash
uv run --project packages/core python scripts/gen_images.py
```

Key 从 `OPENAI_API_KEY` 或 `~/.config/gpudock/openai.key` 读取；加 `--force 名称` 可以重新生成单张。

## 新增一个模型（配方）

1. 在 `gpudock_agent.py` 的 `RECIPES` 里加一条元数据（名称、类别、许可证、是否可商用、显存、下载大小）。
2. 写 `_install_<id>()`：系统 Python 能装就用 `pip_install`；依赖不支持 Colab 当前 Python（2026 年起是 3.13）时，用 `ensure_env("tts", ...)` 装进隔离的 Python 3.12 环境。
3. 在对应命令里接入（例如 `_tts_engine`），重活都在 agent 子进程里跑，结束后显存自动释放。
4. 在 `GPUDOCK_AGENT_FAKE=1` 下给它一个替身实现，让 `tests/test_tasks.py` 不依赖 GPU 也能覆盖流程。

请只接入许可证清楚、来源官方的模型，并在 `RECIPES` 里如实标注是否可商用。

## 原则

- 只通过官方 Colab API 和运行时代理访问 Colab：不加隧道、不做多账号轮换、不做公网分享。
- 声音克隆必须保留授权确认。
- 令牌、密钥不写日志、不上传到运行时。
