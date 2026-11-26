# GPUDock

**把 Colab 的显卡装进你的桌面。** 用自己的 Google 账号登录，一键启动 T4 / L4 / A100 / H100，在本地界面里部署开源模型、生图生视频、批量转写、批量配音、批量朗读。

> A local GUI for Google Colab runtimes, built on Google's official Colab API: pick a GPU, deploy open models, generate images and video, and run batch jobs (transcription, dubbing, text-to-speech) from your desktop. Not affiliated with or endorsed by Google.

## 能做什么

| | |
|---|---|
| **一键登录** | 点“用 Google 登录”，在浏览器里点“允许”即可，不用配置任何 GCP 项目 |
| **选显卡** | 卡片式展示 T4 / L4 / A100（含 80GB）/ RTX PRO 6000 / H100 / TPU：显存、估算单价、你的账号能不能用 |
| **控制台** | 直接执行 Python 和 `!shell`，实时输出、可中断，GPU 利用率 / 显存 / 内存 / 磁盘实时监控 |
| **模型库** | Whisper、Demucs、Kokoro、VoxCPM2，Qwen3 / Qwen3.8 等大模型，一键部署与试用，标注许可证和是否可商用 |
| **创作** | 生视频：Wan2.2（5B / A14B）、LTX-2.5、MiniMax H3；生图：Z-Image Turbo、Qwen-Image 2.1、FLUX 3 Klein、MiniMax H3 Image。基于 ComfyUI 官方工作流模板，自动部署、按需下载模型 |
| **批量转写** | 音视频 → SRT / VTT / TXT / JSON，自动切句、中文全角标点 |
| **批量配音** | 视频 → 人声分离 → 转写 → 翻译（运行时内 Qwen3 或外部 API）→ **人工校对** → 配音 → 时长对齐 → 混回背景音乐 → 输出 MP4 + 单语 / 双语字幕 |
| **批量朗读** | 一行一个音频，可合并；Kokoro 预置音色或 VoxCPM2 声音克隆 |
| **没货自动排队** | 显卡暂时没货时每 45 秒自动重试，最多 20 分钟，可随时取消 |
| **省钱** | 空闲自动释放（只释放 GPUDock 自己创建的运行时），实时显示估算花费 |

## 快速开始

需要 [uv](https://docs.astral.sh/uv/) 和 Node.js 20+。

```bash
cd apps/web && npm install && npm run build
```

```bash
cd packages/core && uv run gpudock
```

浏览器会自动打开 GPUDock。想要独立窗口：`uv run --extra desktop gpudock --window`。

登录时，在 Google 授权页上**点“全选”**，一定要勾上 Colab 权限。

> 从源码运行时，维护者需要先配置一次项目的 OAuth 客户端（见 [docs/m0.md](docs/m0.md)）。正式发布版会内置，用户直接登录。

产出文件保存在 `~/GPUDock/outputs/`，释放运行时也不会丢。

## 工作原理

```
你的电脑                                         Google
┌──────────────────────────────┐   HTTPS   ┌─────────────────────────────┐
│ 浏览器 / 桌面窗口（React）     │           │ colaboratory.googleapis.com │
│        │ 127.0.0.1 + 令牌       │ ────────▶ │ 规格 / 订阅 / 运行时         │
│ gpudock 本地服务（FastAPI）    │           └─────────────────────────────┘
│  登录 · 任务编排 · 产物下载    │  WSS      ┌─────────────────────────────┐
│                              │ ────────▶ │ Colab 运行时（官方运行时代理）│
└──────────────────────────────┘ Jupyter   │  gpudock_agent：部署模型、    │
                                           │  转写、分离、合成、翻译、混音  │
                                           └─────────────────────────────┘
```

- 运行时不开放任何端口，也不用 ngrok 之类的隧道。所有通信都走 Colab 官方运行时代理上的 Jupyter 协议。
- 每个重活都在运行时里的独立子进程中执行，结束后显存自动释放。
- Colab 在 2026 年换成了 Python 3.13，部分语音模型还不支持。这些模型会装进 uv 管理的独立 Python 3.12 环境，不影响 Colab 自带的环境。
- 登录令牌和翻译 API 密钥存在系统钥匙串里，不会上传到运行时。

## 合规与边界

- 只通过官方 API 访问 Colab；不做多账号轮换，不做公网分享，不把你的 GPU 提供给别人。
- 计算消耗的是**你自己账号**的 Colab 额度。免费账号用 Web 界面做重度内容生成，可能被 Colab 终止（见 [Colab FAQ](https://research.google.com/colaboratory/faq.html)），建议在有计算单元余额的账号上批量跑。
- 声音克隆只能用于你有权使用的声音。界面会要求确认授权，Colab 也明令禁止生成 deepfake。
- 每个模型的许可证都在模型库里标出，商用前请自行确认。部分模型有地区或收入限制（例如 MiniMax H3 的许可不覆盖欧盟、英国、美国、韩国），界面上会写明。
- 生成模型的权重直接从 Hugging Face 下载到你的运行时；需要授权的模型（LTX-2.5、FLUX 3）要用你自己的 HF Token，Token 只存在本机钥匙串里，不会写进运行时磁盘。

## 项目结构

```
apps/web/            React + TypeScript 界面（构建到 packages/core/.../web/dist）
packages/core/       Python 核心与本地服务
  gpudock_core/
    auth.py          Google 登录（PKCE + 本地回调）
    colab_api.py     官方 Colab API
    jupyter.py       运行时 Jupyter REST + 内核 WebSocket
    files.py         分片上传、断点续传下载
    agent/           上传到运行时执行的 agent
    server/          本地服务：运行时、任务、模型库
    m0/              技术验证实验（gpudock-m0）
docs/                文档
proposal.zh-CN.md    方案与路线图
```

## 开发

```bash
cd packages/core && uv run pytest -q
```

测试用本地 Jupyter Server 模拟 Colab 运行时，并用替身模型跑完整的转写、朗读、配音流水线（ffmpeg 混音和视频合成都是真实执行的）。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 路线图

- [x] 一键登录、显卡选择、运行时管理、控制台、资源监控、空闲自动释放
- [x] 模型库：Whisper、Demucs、Kokoro、VoxCPM2、Qwen3 / Qwen3.8 / Inkling-2
- [x] 批量转写、批量配音（含人工校对）、批量朗读
- [ ] 多说话人配音（说话人分离）
- [x] 生图、生视频（ComfyUI 官方工作流：Wan2.2、LTX-2.5、MiniMax H3、FLUX 3、Qwen-Image 等）
- [ ] 批量出图（一次多条提示词）
- [ ] Windows / macOS 安装包
- [ ] MCP Server：让 AI agent 调用 GPUDock 的批量能力

## 许可证

[Apache-2.0](LICENSE)
