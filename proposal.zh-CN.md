# GPUDock：Google Colab 的桌面 GUI（开源方案）

> 暂定名 **GPUDock**（名字里不放 "Google"/"Colab"，原因见 §6.3）。
> 一句话定位：**用自己的 Google 账号登录 → 选显卡 → 一键部署模型 → 跑批量任务（批量配音、批量 TTS、批量转写、LLM 批处理、批量出图）**，全程走 Google 官方 API，不靠隧道和浏览器自动化。
>
> 调研日期：2026-11-25。Colab API 一周前（2026-11-17）才公开，字段和限制可能还会变，§8 列出了需要第一周实测确认的点。

---

## 0. 结论速览

1. **时机正好**：Google 在 2026-11 公开了 **Colab REST API（`colaboratory.googleapis.com/v1beta`）+ 托管 MCP 端点**，还开源了官方 **Colab CLI**（Apache-2.0）。用 API 就能程序化地"选 GPU → 创建运行时 → 连 Jupyter 执行代码 → 传文件 → 删除运行时"。这条路比以前的"notebook + ngrok/cloudflared 隧道"合规，也更稳定。
2. **空白点明确**：官方现在只有 CLI（不支持 Windows）、VS Code 扩展（面向开发者）和 MCP（面向 AI agent），**没有面向普通创作者的 GUI**，也没有"模型库 + 批量工作流"这一层。社区方案大多是单模型 notebook 加隧道，有违反服务条款的风险。
3. **推荐架构**：本地 Python 核心（FastAPI）+ React UI，第一阶段用 `uvx gpudock` 打开本地网页，第二阶段用 Tauri 打成 Win/macOS 安装包。运行时侧不开任何端口，**所有通信走 Colab 官方运行时代理上的 Jupyter 协议**。
4. **最大的不确定项**：`https://www.googleapis.com/auth/colaboratory` 这个 OAuth scope 是否属于 sensitive/restricted。这决定了开源项目能不能提供"一键登录"（共享 OAuth 客户端），还是只能让用户自带 GCP 项目。方案里设计了三种登录模式来兜底。
5. **合规是产品设计的一部分**：不做多账号轮换、不做隧道或公网分享、不做"把我的 GPU 分享给别人"、声音克隆必须勾选授权声明。免费账号且没有计算单元余额时，跑重度内容生成可能被 Colab 终止，UI 里要明确提示。

---

## 1. 调研结论

### 1.1 "程序化控制 Colab"的几条路线

| 路线 | 原理 | 能否选 GPU | 合规性 | 稳定性 | 结论 |
|---|---|---|---|---|---|
| **A. 官方 Colab REST API** | OAuth → `POST /v1beta/runtimes` 创建运行时 → 拿到 `connectionInfo.url + token` → 标准 Jupyter REST/WebSocket | ✅ 通过 `runtimeSpec` 选择 | ✅ 官方授权接口 | 中高（v1beta） | **主路线** |
| B. 官方 Colab CLI (`google-colab-cli`) | 同 A 的封装，`colab new --gpu L4`、`colab exec`、`colab upload` | ✅ | ✅ | 中高 | 可以参考实现，不直接依赖（不支持 Windows，CLI 输出不适合当 IPC） |
| C. colab-mcp 浏览器桥 | 本地起 WebSocket，浏览器打开 `colab.research.google.com/notebooks/empty.ipynb#mcpProxyToken=…&mcpProxyPort=…`，Colab 网页前端连回本地 | ❌ 由用户在网页上选 | ✅ 官方工具 | 中（依赖浏览器标签页） | **备选：免配置模式**（不需要 OAuth 客户端） |
| D. 传统 notebook + 隧道 | 用户手动运行 notebook，Gradio share / ngrok / cloudflared 暴露 Web UI | 手动选 | ⚠️ 免费层明确限制"绕过 notebook UI、主要通过 Web UI 交互"；"连接远程代理"对所有用户禁止 | 低 | 不采用 |
| E. 浏览器自动化（Playwright 模拟点击） | 模拟人在 Colab 网页操作 | 手动选 | ❌ 违反"仅以 Google 授权的方式访问 Colab" | 很低 | 不采用 |
| F. Colab Enterprise（Vertex AI） | GCP 付费产品，运行时模板 + gcloud/REST | ✅（T4/L4/A100/H100 等） | ✅ | 高 | 后期作为"企业 Provider"可选，计费是 GCP 按小时，不是 Colab 计算单元 |

### 1.2 官方 Colab API 关键细节（v1beta）

**端点**（Base：`https://colaboratory.googleapis.com`）

| 方法 | 路径 | 用途 |
|---|---|---|
| `runtimespecs.list` | `GET /v1beta/runtimespecs` | 列出规格，并告诉你**当前账号是否 eligible** |
| `runtimes.create` | `POST /v1beta/runtimes` | 创建运行时（长操作，返回 operation） |
| `runtimes.list` / `get` / `delete` | `GET/DELETE /v1beta/runtimes[/{id}]` | 复用、刷新连接 token、释放 |
| `getSubscription` | `GET /v1beta/subscription` | 订阅等级：Free / Pro / Pro+ |
| `operations.get` / `wait` | `GET /v1/operations/*[:wait]` | 等待创建完成 |

**规格 `RuntimeSpec.Key`**：

- `variant`：`VARIANT_CPU` / `VARIANT_GPU` / `VARIANT_TPU`
- `accelerator`：字符串。实测出现过 `NONE`、`T4`、`L4`、`A100`、`G4`、`H100`、`V5E1`、`V6E1`
- `shape`：`SHAPE_STANDARD` / `SHAPE_HIGHMEM`（A100 80GB 走 HIGHMEM，需要付费）

**连接运行时**：`connectionInfo = { url, token, expireTime }`。请求头带 `X-Colab-Runtime-Proxy-Token: <token>`，然后就是**标准 Jupyter Server**（`/api/kernels`、`/api/sessions`、`/api/contents`、kernel WebSocket）。token 约 1 小时过期，过期前调 `runtimes.get` 刷新。

**文件**：上传用 `PUT {url}/api/contents/{path}`（base64，单请求 ≤ 50MB，更大的文件用 `chunk` 分片，最后一片 `chunk: -1`）；下载用 `GET {url}/files/{path}`（支持 Range）。

**错误码要在 UI 里翻译成人话**：

- `FAILED_PRECONDITION`：没容量、配额用完或活跃运行时太多 → "该显卡暂时没货 / 额度不足"
- `RESOURCE_EXHAUSTED`：限流，指数退避重试
- `TOO_MANY_ACTIVE_RUNTIMES`、`QUOTA_EXCEEDED_USAGE_TIME`、`DENYLISTED`
- 创建时 `DEADLINE_EXCEEDED`：仍在创建中，用同一 `requestId` 重试即可，不会重复创建

**认证**：scope 是 `https://www.googleapis.com/auth/colaboratory`（只读用 `colaboratory.readonly`），另外需要 `openid email profile`。官方 VS Code 扩展用 PKCE + 本地 loopback 回调，失败时退回"中转重定向"。社区实测还需要 quota project（请求头 `x-goog-user-project`），否则会 403。

**非公开接口（只能当可选增强，默认关闭）**：官方 CLI/扩展还调用了 `colab.research.google.com/tun/m/ccu-info`（计算单元余额、每小时消耗速率）和 `…/v1/user-info?get_ccu_consumption_info=true`。这些接口没有文档，随时可能变，只用来显示余额。

### 1.3 GPU 规格与计费

| 规格 | 显存 | 架构 | CU/小时* | 约 $/小时* | 免费账号可申请** | 适合 |
|---|---|---|---|---|---|---|
| T4 | 16GB（可用约 15GB） | Turing（不支持 bf16） | 1.19 | 0.12 | ✅ | Whisper、TTS、7–8B 量化 LLM、SDXL |
| L4 | 22.5GB | Ada（支持 bf16/fp8） | 1.71 | 0.17 | ✅ | **性价比首选**：配音全流程、8B LLM、Flux fp8 |
| A100 40GB | 40GB | Ampere | 5.40 | 0.54 | ✅ | 14–32B LLM、视频生成 |
| A100 80GB（HIGHMEM） | 80GB | Ampere | 7.52 | 0.75 | ❌（需付费） | 70B 量化 |
| G4 | 约 96GB（RTX PRO 6000） | Blackwell | 8.71 | 0.87 | ✅ | 大模型、视频、批量出图 |
| H100 | 80GB | Hopper | 未知 | — | ❌ | 大模型训练/推理 |
| TPU v5e-1 / v6e-1 | — | TPU | — | — | ✅ | 只能跑 JAX/XLA，**大部分配方不支持** |

\* 社区 2026-03 实测（$0.10/CU），会随时间变化。程序里做成可更新的配置文件，不写死。
\** 一个免费账号 2026-11 的快照。**可申请 ≠ 有容量**，真正能不能拿到要看创建时有没有货。

运行时长：免费最多约 12 小时（视可用性），Pro/Pro+ 也是约 12 小时，Pro+ 在计算单元充足时最长 24 小时。代码执行结束后空闲才会触发空闲超时（官方 FAQ："Idle timeouts only apply if code execution terminates"）。

### 1.4 服务条款红线 → 产品设计约束

来源：[Colab FAQ](https://research.google.com/colaboratory/faq.html)、[Colab 附加条款 v6（2026-08-13）](https://research.google.com/colaboratory/tos_v6.html)

| 条款 | 适用范围 | 产品怎么做 |
|---|---|---|
| 与 Colab 交互式计算无关的文件托管、媒体服务、Web 服务 | 所有用户 | 不提供公网分享链接，不做"托管服务" |
| 连接远程代理 | 所有用户 | **不内置 ngrok/cloudflared/localtunnel** |
| 用多个账号绕过限制 | 所有用户 | **不做多账号轮换**，一次只用一个账号 |
| 生成 deepfake | 所有用户 | 声音克隆必须勾选授权声明，不做唇形替换 |
| 仅以 Google 授权的方式访问；不得向第三方出售或提供 Colab 访问 | 所有用户 | 只走官方 API；产品是**单用户本地工具**，不做多租户 SaaS |
| SSH、远程桌面；绕过 notebook UI 主要用 Web UI 交互；分布式 worker | **免费且无计算单元余额** | 检测订阅等级和余额，免费模式显示"实验性，可能被终止"的提示；SSH 功能只对付费用户开放 |

> 灰区说明：GPUDock 通过官方 API 驱动运行时，与官方 CLI、VS Code 扩展、MCP 用的是同一条通道。但 FAQ 对"免费层 + Web UI 做内容生成"有明确的终止风险提示，所以 README 和 UI 里都要写清楚：**重度批量生成建议在有计算单元余额的账号上进行。**

### 1.5 可部署模型与配音生态

| 类别 | 推荐组件 | 许可（以仓库为准） | 备注 |
|---|---|---|---|
| LLM 服务 | vLLM、Ollama、llama.cpp | Apache-2.0 / MIT / MIT | 在运行时内部只监听 localhost |
| LLM 模型 | Qwen3 系列等 | Apache-2.0 | 翻译、改写、批量推理 |
| ASR | faster-whisper、WhisperX；中文用 FunASR/SenseVoice | MIT / BSD-2 / 需核实 | WhisperX 带词级时间戳 |
| 说话人分离 | pyannote | 代码 MIT，模型需在 HF 同意条款 | 需要用户提供 HF token |
| 人声/伴奏分离 | Demucs | MIT | 保留背景音乐 |
| TTS/克隆 | **IndexTTS2**（时长控制 + 情感，适合配音） | 商用需联系 B 站（需核实） | 社区经验：约 6–12GB 显存 |
| | **CosyVoice3**（Fun-CosyVoice3-0.5B） | Apache-2.0 | 多语种，0.5B 显存友好 |
| | F5-TTS / Fish Speech / XTTS-v2 | 权重多为非商用（需核实） | UI 要标"非商用" |
| 图像 | ComfyUI + Flux/SDXL | ComfyUI 是 GPL-3.0 | 作为独立进程运行，不并入本项目代码 |
| 视频 | Wan 2.x 等 | 需核实 | A100/G4 级别显存 |
| 配音参考项目 | VideoLingo、pyVideoTrans、open-dubbing、Linly-Dubbing | 各异 | 借鉴流程设计，不直接复制代码 |

### 1.6 同类项目与空白

- **Google 官方**：[colab-vscode](https://github.com/googlecolab/colab-vscode)（VS Code 扩展）、[google-colab-cli](https://github.com/googlecolab/google-colab-cli)（命令行，仅 Linux/macOS）、[colab-mcp](https://github.com/googlecolab/colab-mcp)（浏览器桥 MCP）、托管 MCP `https://colaboratory.googleapis.com/mcp`。都是 Apache-2.0，可以参考实现。
- **社区**：[fastcolab](https://github.com/AnswerDotAI/fastcolab)（Python 库）、`colab-gpu-platform`（Railway 上的作业平台），以及大量"单模型 notebook + 隧道"（ComfyUI、Ollama 等）。
- **空白**：图形界面、Windows 支持、模型库（配方）、批量工作流、成本控制、合规默认值。这几项加在一起就是 GPUDock 的差异化。

---

## 2. 产品定义

### 2.1 目标用户

1. **内容创作者**（主力）：做视频翻译配音、有声书、批量出图，没有本地好显卡，也不想碰代码。
2. **独立开发者/学生**：想临时用 L4/A100 跑一下模型、做 LLM 批处理，嫌 notebook 来回复制粘贴麻烦。
3. **AI agent 用户**（后期）：通过 GPUDock 暴露的 MCP 工具，让 Claude/Gemini CLI 调用"批量配音"之类的高层能力。

### 2.2 功能清单

| 模块 | MVP（M1） | v1（M2） | v2（M3+） |
|---|---|---|---|
| 账号 | Google 登录、订阅等级 | 计算单元余额与消耗速率（可选增强） | 多 Provider 账号 |
| 运行时 | 规格卡片（显存/单价/可申请）、创建/复用/删除、资源监控 | 自动关机、预算上限、高内存开关 | 运行时快照/预热缓存 |
| 模型库 | 配方引擎 + 3 个官方配方 | 10+ 配方、许可证徽章 | 社区配方仓库、签名校验 |
| 工作流 | 批量 TTS、批量转写/字幕 | **批量配音**、LLM 批处理（JSONL） | 批量出图（ComfyUI 工作流）、视频生成 |
| 任务 | 队列、日志、取消 | 断点续跑、失败重试、成本统计 | 定时任务 |
| 文件 | 上传/下载 | Google Drive 中转（`drive.file`） | Hugging Face 缓存加速 |
| 集成 | — | 本地 OpenAI 兼容端点（仅 127.0.0.1，默认关闭） | MCP Server |
| 分发 | `uvx gpudock`（本地网页） | Win/macOS 安装包（Tauri） | 自动更新、Linux 包 |

### 2.3 界面结构

```
┌ 侧边栏 ─────────┐┌ 主区域 ───────────────────────────────────────────┐
│ ① 概览          ││ 账号: xxx@gmail.com · Pro · 余额 87.3 CU · 1.71 CU/h │
│ ② 运行时        ││ 当前运行时: L4 · 显存 7.2/22.5GB · 已运行 0:42      │
│ ③ 模型库        ││ ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐           │
│ ④ 工作流        ││ │  T4   │ │  L4 ★ │ │ A100  │ │  G4   │  [创建]   │
│   · 批量配音    ││ │ 16GB  │ │22.5GB │ │ 40GB  │ │ 96GB  │           │
│   · 批量 TTS    ││ │1.19/h │ │1.71/h │ │5.40/h │ │8.71/h │           │
│   · 批量转写    ││ └───────┘ └───────┘ └───────┘ └───────┘           │
│   · LLM 批处理  ││ 空闲 [15] 分钟后自动释放 · 本次预算上限 [20] CU      │
│ ⑤ 任务队列      ││                                                    │
│ ⑥ 文件          ││                                                    │
│ ⑦ 设置          ││                                                    │
└─────────────────┘└────────────────────────────────────────────────────┘
```

- **运行时页**：GPU 卡片显示显存、单价、是否可申请、"适合哪些配方"。选了配方会自动推荐最低可用的显卡。
- **模型库页**：按 LLM / ASR / TTS / 图像 / 视频 / 音频处理分类，每张卡片带显存需求、许可证徽章（可商用/非商用/需核实）和一键部署按钮。
- **批量配音页**：拖入视频 → 选源语言和目标语言 → 选音色（预置 / 上传参考音 / 克隆原声，后两项需勾选授权）→ 选翻译引擎 → 可选"翻译后暂停人工校对" → 开始。

---

## 3. 技术架构

### 3.1 总体架构

```
┌──────────────────── 用户电脑 ─────────────────────┐          ┌──────────── Google ────────────┐
│  Tauri 壳 / 浏览器                                 │          │ OAuth 2.0 (PKCE + loopback)    │
│    └─ React UI ── HTTP/WS (127.0.0.1 + 随机令牌) ─┐ │          │ colaboratory.googleapis.com    │
│                                                   ▼ │  HTTPS   │   runtimes / runtimespecs /   │
│  gpudock-core（Python 3.12, FastAPI）          ──────────────▶ │   subscription / operations   │
│   ├─ auth        令牌存在系统钥匙串               │ │          └────────────────────────────────┘
│   ├─ colab_api   由 discovery 文档生成的客户端     │ │          ┌──────── Colab 运行时 (VM) ──────┐
│   ├─ channel     Jupyter REST + kernel WebSocket ─────HTTPS/WSS▶ Jupyter Server（官方运行时代理）│
│   ├─ recipes     配方引擎（YAML + Python worker）  │ │ X-Colab-  │  └ kernel = gpudock-agent      │
│   ├─ jobs        队列 + 状态机（SQLite）           │ │ Runtime-  │      ├ 已加载的模型（常驻内存） │
│   ├─ pipelines   批量配音等工作流                  │ │ Proxy-    │      ├ vLLM/Ollama 子进程       │
│   └─ store       本地产物目录                      │ │ Token     │      │   （只监听 localhost）    │
└────────────────────────────────────────────────────┘ │           │      └ /content/gpudock/jobs/*  │
                                                       │           └─────────────────────────────────┘
```

**核心原则**：运行时**不对外开放任何端口**，不用隧道。所有控制、数据、日志都走官方运行时代理上的 Jupyter 协议。

### 3.2 认证设计（三种模式）

**原则：最终用户只点"用 Google 登录"一次，任何 GCP 配置都不让用户做。**（已在 M0 代码中实现，见 `packages/core/src/gpudock_core/auth.py`）

| 模式 | 用户需要做的事 | 适用阶段 | 说明 |
|---|---|---|---|
| **① 项目官方客户端**（默认） | 点"用 Google 登录"，在浏览器里点"允许" | **从 M0 起就是默认** | 维护者一次性注册 OAuth 桌面客户端；发布版由 CI 内置到安装包（`scripts/embed_oauth_client.py`），git 仓库里不放。API 调用配额计到项目的 GCP 项目，GPU 计算仍花用户自己的 Colab 额度 |
| ② 自带客户端（BYO） | 在自己的 GCP 项目里建客户端，`login --client-secrets` | 高级用户、自行编译的 fork | 不再是默认，只作为逃生通道 |
| ③ gcloud ADC | `gcloud auth application-default login --scopes=…colaboratory` + quota project | 开发者 | 适合 CLI 用户 |
| 备选 ④ 浏览器桥（路线 C） | 打开 Colab 网页并在网页上选 GPU | 官方客户端审核期间或审核不过时 | 复用 colab-mcp 机制，无需 OAuth 客户端，但不能程序化选 GPU |

✅ **M0 已确认（2026-11-25）：`colaboratory` 属于非敏感范围。** 模式 ① 不需要 Google 审核，把同意屏幕从"测试"切到"正式"即可对所有人开放（若要在授权页显示 logo，需另做品牌验证）。原先的预案保留如下，供参考：

模式 ① 公开发布前的门槛取决于 `colaboratory` scope 的分类（M0 问题 ①）：非敏感，直接切到"正式"即可；敏感，需要过 OAuth 验证（主页、隐私政策、域名验证、演示视频，可以放在 GitHub Pages）。审核期间最多约 100 个用户，并会看到"未验证"提示，可以作为内测名额使用；受限，还需要年度安全评估，届时以模式 ④ 兜底。

实现要点：

- PKCE + `http://127.0.0.1:<随机端口>` 回调；令牌存系统钥匙串（keyring），不落明文。
- 客户端查找顺序：命令行参数 → 环境变量 → 本机 `~/.config/gpudock/oauth_client.json` → 安装包内置。fork 用自己的客户端，不会冒用上游项目的配额。
- 所有用户共用项目的"每项目每分钟请求数"配额。用户量上来后要在 Cloud Console 申请提额，并在客户端做好退避重试（已实现）。
- **绝不**借用 Google 自家工具内置的 OAuth client id（例如 Cloud SDK 的），那样属于冒用。
- 在中国大陆访问 Google 需要代理：core 读取 `HTTPS_PROXY` 和系统代理，设置页提供代理配置和连通性测试。

### 3.3 运行时生命周期

```
选规格 ─▶ runtimespecs.list 校验 eligible ─▶ runtimes.create(requestId=UUID)
      ─▶ operations.wait ─▶ get → connectionInfo ─▶ 建 kernel session ─▶ 安装/启动 agent
      ─▶ [执行任务…] ─▶ 队列空闲 N 分钟 或 达到预算上限 ─▶ runtimes.delete
```

- **复用优先**：启动时先 `runtimes.list`，有活着的就直接接上（创建慢，而且有配额）。
- **token 刷新**：后台在过期前 5 分钟调用 `runtimes.get`；WebSocket 断开后自动重连并重新挂上任务。
- **自动关机**：队列空了 N 分钟就删除运行时（省计算单元）；预算上限按"CU/小时 × 已运行时长"在本地估算，接近上限时先暂停新任务再释放。
- **不做"假活跃"保活**：官方 CLI 已经移除了 keep-alive 守护进程。长任务以 kernel 前台执行的方式运行（流式输出进度），代码在执行时本来就不会触发空闲超时；真正空闲就该释放。
- **容量不足**：给出"换 L4/T4 重试""稍后自动重试"两个选项，不做多账号切换。

### 3.4 通信通道：kernel 即 agent

- 每个运行时开一个专用 kernel session，执行一次引导代码：`pip install gpudock-agent` 后 `import gpudock_agent as A; A.boot()`。
- **RPC**：core 发 `execute_request`，代码是 `A.call("tts.synthesize", {...})`，结果以 JSON 行写到 stdout 或 iopub，core 解析。模型加载后常驻在 kernel 内存里，下次调用不用重新加载。
- **流式进度**：任务运行时按行输出 `{"type":"progress","job":…,"pct":…}`，UI 实时显示。
- **取消**：`POST /api/kernels/{id}/interrupt`。
- **断线恢复**：任务状态同时写到 `/content/gpudock/jobs/{id}/state.json`；core 重连后用 contents API 读取状态，再决定重新挂上还是续跑。
- **为什么不用 MCP 端点跑长任务**：托管 MCP 的 `execute_code` 单次 600 秒就超时（社区实测 300 秒断流），只返回文本，不适合批量任务。直接用 Jupyter WebSocket 没有这个限制。

### 3.5 配方（Recipe）规范

配方 = "安装 + 下载权重 + 加载 + 暴露哪些任务"的声明，社区可以用 PR 贡献。以下为示意：

```yaml
id: indextts2
name: IndexTTS2
category: tts
homepage: https://github.com/index-tts/index-tts
license:
  code: "见仓库 LICENSE"
  weights: "商用需联系权利方（需核实）"
  commercial: unknown          # yes / no / unknown → UI 徽章
hardware:
  min_vram_gb: 8
  recommended: [L4, A100]
  supported: [T4, L4, A100, G4, H100]   # 不含 TPU
  notes: "T4 无 bf16，用 fp16 推理"
install:                        # 在运行时执行，可缓存到 Drive
  - git clone --depth 1 https://github.com/index-tts/index-tts /content/apps/index-tts
  - pip install -e /content/apps/index-tts
weights:
  - hf: IndexTeam/IndexTTS-2
    to: /content/models/indextts2
worker: gpudock_recipes.indextts2:Worker   # load()/unload()/run(task, args)
tasks:
  tts.synthesize: { input: [text, voice_ref?, duration_ms?, emotion?], output: wav }
consent_required: [voice_ref]   # 使用参考音色时强制授权确认
```

- `worker` 统一接口：`load()`、`unload()`、`run(task, args, progress_cb)`，这样调度器能按显存装载或卸载模型。
- 配方仓库分"官方（CI 实测过）"和"社区（显示风险提示）"两级。配方本质是会执行的代码，所以要审核，后期加签名。

### 3.6 任务与断点续跑

- 任务状态机：`queued → uploading → running(stage i/n) → downloading → done | failed | cancelled`。
- 每个阶段的产物都落到运行时 `/content/gpudock/jobs/{id}/stage_k/`，并**及时回传本地**。运行时被回收后，下次从最后完成的阶段继续。
- 本地 SQLite 记录任务、阶段、产物哈希、耗时和估算成本。

### 3.7 文件传输

| 场景 | 方案 |
|---|---|
| 小文件（≤ 50MB） | contents API 直接 PUT/GET |
| 大文件 | contents API 分片上传；下载用 `/files/` + Range 断点续传 |
| 大批量视频 | 可选 Google Drive 中转：本地上传到 Drive 的应用专属文件夹（`drive.file` scope），运行时用短期令牌拉取。下载速度通常更好 |
| 模型权重 | 运行时直接从 Hugging Face 下载（走 Google 网络，速度快）；可选缓存到 Drive |

### 3.8 批量配音流水线（旗舰工作流）

```
视频/音频
  ① 抽音轨 ffmpeg
  ② 人声/伴奏分离 Demucs ............................ 保留 BGM
  ③ 转写 + 词级时间戳 WhisperX / SenseVoice(中文)
  ④ 说话人分离 pyannote（可选，多角色时）
  ⑤ 断句与合并（按停顿/语义，生成配音片段）
  ⑥ 翻译：本地 LLM(vLLM+Qwen3) 或 外部 API ........... 带"等时约束"
  ⑦ [可选暂停] 人工校对：GUI 内编辑字幕与译文
  ⑧ TTS：IndexTTS2（时长控制）/ CosyVoice3 .......... 音色：预置 / 参考音 / 原声克隆*
  ⑨ 时长对齐：优先 TTS 时长控制，其次 atempo/rubberband 微调（≤ ±15%），超出就回到 ⑥ 重译缩写
  ⑩ 混音：配音 + 伴奏，loudnorm 响度归一
  ⑪ 封装：mp4 + SRT/ASS（单语/双语）+ 工程文件（可再次编辑）
* 参考音和原声克隆必须勾选"我拥有该声音的使用授权"，并写入输出元数据
```

关键设计：

- **按阶段批处理**：先把所有视频的 ②③ 跑完，卸载 ASR，再加载 TTS 跑所有片段。T4/L4 上可以避免模型反复换入换出，显存峰值只等于单个模型。
- **等时翻译**：翻译提示里给出每段时长和目标字数（时长 × 目标语言语速），尽量减少后续变速。
- **翻译 API key 不进运行时**：外部翻译在本地 core 调用，运行时只拿到结果文本。
- **片段级重试**：单句失败只重做这一句；校对时改了一句也只重合成这一句。
- 不做唇形同步：技术上可行，但 Colab 明确禁止 deepfake，风险太高。

### 3.9 显存调度

- 调度器维护"当前已加载模型 + 显存占用"，按配方的 `min_vram_gb` 和实测峰值决定是否需要先卸载。
- 默认一次只常驻一个大模型；L4 及以上允许"ASR + TTS"同时常驻。
- 提供"显存预估器"：选好工作流后显示每个阶段的峰值显存和推荐显卡。

### 3.10 技术选型

| 层 | 选择 | 理由 |
|---|---|---|
| 核心 | Python 3.12 + FastAPI + `httpx` + `websockets` + `jupyter_client` 协议 + `google-auth` | 与 ML 生态同语言，agent 和 worker 可以共享代码；官方 CLI 也是 Python |
| Colab API 客户端 | 由 `$discovery/rest?version=v1beta` 生成，外面包一层适配器 | v1beta 字段可能变，变化只影响一层 |
| UI | React + TypeScript + Vite + shadcn/ui + TanStack Query | 生态成熟，容易吸引贡献者 |
| 桌面壳 | Tauri 2，Python core 用 PyInstaller 打成 sidecar | 体积小，支持 Windows（官方 CLI 不支持） |
| 本地存储 | SQLite + 产物目录 | 零依赖 |
| 运行时 agent | `gpudock-agent`（纯 Python，依赖少） | `pip install` 几秒完成 |
| 被否决的方案 | Gradio 全栈（界面难以打磨）、Electron（体积大）、纯 TS（ML worker 仍需要 Python） | |

本地服务安全：只绑定 `127.0.0.1`，每次启动生成随机访问令牌，校验 `Origin`/`Host`，防止其他网页通过 DNS rebinding 或 CSRF 调用本地接口。

---

## 4. 合规与安全清单

- [ ] 只调用官方 API；非公开接口（余额显示）放在功能开关后，默认关闭
- [ ] 不内置任何隧道或公网分享；"本地 OpenAI 兼容端点"只绑定 127.0.0.1，默认关闭
- [ ] 不做多账号轮换和多租户；README 明确写"每个人用自己的账号"
- [ ] 检测订阅等级和余额：免费且无余额时显示条款风险提示
- [ ] 声音克隆需授权确认；输出写入"AI 合成"元数据；不做唇形替换
- [ ] 每个配方展示许可证，非商用权重在导出时再提示一次
- [ ] 令牌存系统钥匙串；不把 Google 令牌送进运行时（Drive 模式只传短期、`drive.file` 范围的令牌）
- [ ] 配方代码审核；社区配方显示风险提示

---

## 5. 仓库结构

```
gpudock/
├─ apps/
│  ├─ web/              # React UI
│  └─ desktop/          # Tauri 2 壳
├─ packages/
│  ├─ core/             # gpudock-core：auth / colab_api / channel / jobs / pipelines / FastAPI
│  └─ agent/            # gpudock-agent：运行时侧引导、RPC 分发、显存调度
├─ recipes/             # 官方配方（YAML + worker），每个配方带冒烟测试
├─ pipelines/           # 批量配音、批量 TTS、转写、LLM 批处理
├─ docs/                # 中英文档站（安装、BYO OAuth 向导、配方开发指南）
└─ .github/workflows/   # lint、单测、打包发布、配方冒烟测试（手动触发，消耗 CU）
```

---

## 6. 开源运营

### 6.1 许可证

- 项目代码用 **Apache-2.0**：与 Google 官方 colab-vscode、google-colab-cli、colab-mcp 一致，可以合法参考或复用其代码（保留版权声明，附上 NOTICE）。
- **不把 GPL 项目的代码并入**（ComfyUI、部分配音项目），只在运行时里作为独立进程调用。
- 不分发任何模型权重，配方只指向官方下载源。

### 6.2 文档与增长

- README 首屏放 GIF：登录 → 选 L4 → 拖入 3 个视频 → 得到配音成品。中英双语。
- "5 分钟上手"和"BYO OAuth 图文向导"是转化关键。
- 配方贡献指南 + 配方模板，把社区贡献引导到配方仓库。
- 渠道：GitHub Discussions、Discord/微信群；B 站/小红书演示视频（配音场景天然适合视频传播）。

### 6.3 命名与商标

- 项目名不含 "Google"，避免把 "Colab" 当主品牌。描述里用"for Google Colab"这种说明性用法。
- 不使用 Google/Colab 的 logo。README 加声明："Not affiliated with or endorsed by Google."

---

## 7. 路线图

| 里程碑 | 周期 | 交付 |
|---|---|---|
| **M0 技术验证** | 1 周 | ① 在 Cloud Console 确认 `colaboratory` scope 的分类（决定认证路线）② Python 脚本跑通 specs → create → Jupyter 执行 → 上传/下载 → delete ③ 实测 token 过期后 WebSocket 的行为、长任务执行期间的空闲超时、免费账号实际能拿到哪些 GPU ④ 记录实测结论 |
| **M1 MVP** | 3–4 周 | 一键 Google 登录（内置项目客户端）、GPU 卡片、运行时生命周期与自动关机、配方引擎 + 3 个配方（faster-whisper、CosyVoice3、vLLM+Qwen3）、批量 TTS、批量转写、任务队列与日志；发布 `uvx gpudock` |
| **M2 v1** | 4 周 | **批量配音全流程**、断点续跑、人工校对、Drive 中转、IndexTTS2 配方、成本统计、Win/macOS 安装包；如验证通过，上线项目官方 OAuth 客户端 |
| **M3** | 持续 | ComfyUI 批量出图、视频生成、LLM 批处理、社区配方仓库、MCP Server、浏览器桥备选模式 |
| **M4** | 视需求 | 多 Provider：本地 GPU（复用 agent）、Colab Enterprise、Modal/RunPod 等按量云 |

---

## 8. 风险与待验证

| 风险/未知 | 影响 | 应对 |
|---|---|---|
| ~~`colaboratory` scope 是 sensitive/restricted~~ | **已排除**：M0 确认为非敏感 | 切到"正式"即可一键登录；只需在 API 配额上关注"每项目每分钟"上限 |
| API 是 v1beta，可能变动 | 接口失效 | discovery 生成客户端 + 适配层 + 契约测试；关注 Google 发布说明 |
| 是否有 allowlist（只有第三方说法，未证实） | 部分账号 401/403 | M0 用多个账号实测；遇到时给出明确报错和退路（模式 ④） |
| 免费层容量紧张、条款限制 | 体验差、可能被终止 | 显示容量和条款提示；推荐 L4 加少量计算单元 |
| 非公开接口（余额）变动 | 余额显示失效 | 功能开关，失效时自动隐藏 |
| 模型许可复杂 | 用户误商用 | 配方元数据 + UI 徽章 + 导出时提醒 |
| 声音克隆滥用 | 法律和平台风险 | 授权确认、元数据标记、不做唇形替换 |
| 国内网络 | 登录和传输失败 | 代理设置 + 连通性诊断；大文件走 Drive |

**M0 必须实测确认的问题**：

1. ~~scope 分类~~ ✅ 非敏感（2026-11-25）
2. 免费账号通过 API 实际能创建哪些 GPU、排队情况如何
3. token 过期（约 1 小时）后，已建立的 kernel WebSocket 是否继续可用
4. kernel 持续执行时运行时的最长存活时间，以及空闲多久会被回收
5. contents API 大文件分片上传的吞吐
6. A100 80GB、H100 在 Pro/Pro+ 账号上的规格字符串和可申请情况

---

## 9. 参考资料

- Colab REST API：<https://developers.google.com/colab/api/reference/rest>（runtimes：<https://developers.google.com/colab/api/reference/rest/v1beta/runtimes>）
- Colab 托管 MCP：<https://developers.google.com/colab/api/reference/mcp>
- 官方 CLI：<https://github.com/googlecolab/google-colab-cli>、<https://pypi.org/project/google-colab-cli/>
- 官方 VS Code 扩展：<https://github.com/googlecolab/colab-vscode>
- 官方 colab-mcp（浏览器桥）：<https://github.com/googlecolab/colab-mcp>，发布博文：<https://developers.googleblog.com/announcing-the-colab-mcp-server-connect-any-ai-agent-to-google-colab/>
- 社区实测（API + MCP + Free 账号 specs）：<https://dev.to/gde/the-colab-mcp-endpoint-from-claude-code-set-it-up-test-it-run-a-notebook-on-a-tpu-4ck9>
- fastcolab：<https://github.com/AnswerDotAI/fastcolab>
- Colab FAQ：<https://research.google.com/colaboratory/faq.html>；附加条款 v6：<https://research.google.com/colaboratory/tos_v6.html>
- GPU 计费实测（2026-03）：<https://mccormickml.com/2024/04/23/colab-gpus-features-and-pricing/>
- IndexTTS：<https://github.com/index-tts/index-tts>；CosyVoice：<https://github.com/QwenAudio/CosyVoice>
- 配音参考：VideoLingo、pyVideoTrans、[open-dubbing](https://github.com/Softcatala/open-dubbing)
- Google OAuth scope 验证：<https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification>
