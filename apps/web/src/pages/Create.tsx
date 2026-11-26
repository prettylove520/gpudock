import { useCallback, useEffect, useMemo, useState } from "react";
import { api, put } from "../api";
import { useApp, type Target } from "../ctx";
import type { AgentStatus, GenModel, TaskSummary } from "../types";
import { Card, Field, FileDrop, PageHead, RuntimePicker, Seg, Toggle, type PickedFile } from "../components/common";
import { ChipArt, Pic } from "../components/Logo";
import TaskPanel from "../components/TaskPanel";
import { useStoredTask } from "./shared";
import { ago, cu, duration, usePoll } from "../util";

type Kind = "all" | "video" | "image";

const ASPECTS: { id: string; label: string; ratio: number }[] = [
  { id: "16:9", label: "16:9 横屏", ratio: 16 / 9 },
  { id: "9:16", label: "9:16 竖屏", ratio: 9 / 16 },
  { id: "1:1", label: "1:1 方形", ratio: 1 },
  { id: "4:3", label: "4:3", ratio: 4 / 3 },
];

const IMAGE_SLOTS: Record<string, { min: number; max: number; labels: string[]; hint: string }> = {
  i2v: { min: 1, max: 1, labels: ["首帧图片"], hint: "视频会从这张图开始动起来" },
  flf2v: { min: 2, max: 2, labels: ["首帧", "尾帧"], hint: "按顺序上传：第 1 张是开头，第 2 张是结尾" },
  r2v: { min: 1, max: 9, labels: ["参考图"], hint: "1–9 张；提示词里用 <Picture 1>、<Picture 2> 按顺序指代" },
  edit: { min: 1, max: 1, labels: ["原图"], hint: "要编辑的图片，输出尺寸跟随原图" },
};

const EXAMPLES: Record<"video" | "image", string> = {
  video: "清晨的海边小镇，一只橘猫沿着石阶慢慢走下来，镜头缓缓跟随，电影感，柔和逆光",
  image: "清晨的海边小镇，一只橘猫坐在石阶上，柔和逆光，胶片质感，画面右下角有手写的“GPUDock”字样",
};

const COMMERCIAL: Record<string, string> = { yes: "可商用", conditional: "有条件商用", unknown: "商用条款见仓库", no: "非商用" };
/** Observed Hugging Face → Colab download speed (MB/s), for a rough first-run estimate. */
const DOWNLOAD_MBPS = 160;

/** Pick width/height near the model's native pixel count, multiples of 32. */
function sizeFor(model: GenModel, aspect: string, hd: boolean): [number, number] {
  const [w0, h0] = hd && model.hd_size ? model.hd_size : model.size;
  const pixels = w0 * h0;
  const ratio = ASPECTS.find((a) => a.id === aspect)?.ratio ?? w0 / h0;
  const w = Math.round(Math.sqrt(pixels * ratio) / 32) * 32;
  const h = Math.round(Math.sqrt(pixels / ratio) / 32) * 32;
  return [w, h];
}

function closestAspect(w: number, h: number): string {
  const r = w / h;
  return ASPECTS.reduce((best, a) => (Math.abs(Math.log(a.ratio / r)) < Math.abs(Math.log(best.ratio / r)) ? a : best)).id;
}

export default function Create() {
  const { catalog, handle, toast, runtimes, submitTask, retryTask, specs } = useApp();
  const [kind, setKind] = useState<Kind>("all");
  const [modelId, setModelId] = useState(() => {
    const picked = sessionStorage.getItem("gpudock.create.model");
    sessionStorage.removeItem("gpudock.create.model");
    return picked || "wan22_5b";
  });
  const [mode, setMode] = useState("t2v");
  const [prompt, setPrompt] = useState("");
  const [aspect, setAspect] = useState("16:9");
  const [seconds, setSeconds] = useState(5);
  const [seed, setSeed] = useState<string>("");
  const [hd, setHd] = useState(false);
  const [fast, setFast] = useState(true);
  const [count, setCount] = useState<"1" | "2" | "4">("1");
  const [perLine, setPerLine] = useState(false);
  const [images, setImages] = useState<PickedFile[]>([]);
  const [target, setTarget] = useState<Target>({ runtime: null, release: false });
  const [hasHf, setHasHf] = useState<boolean | null>(null);
  const [hfInput, setHfInput] = useState("");
  const [openId, setOpenId] = useStoredTask("create");
  const [works, setWorks] = useState<TaskSummary[] | null>(null);
  const [shownWorks, setShownWorks] = useState(24);
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [prefetchId, setPrefetchId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const models = catalog?.gen_models ?? {};
  const model = models[modelId];
  const modes = model ? Object.keys(model.templates) : [];

  useEffect(() => {
    if (model && !modes.includes(mode)) setMode(modes[0]);
    if (model?.kind === "image" && aspect === "16:9" && model.size[0] === model.size[1]) setAspect("1:1");
    if (model?.max_seconds && seconds > model.max_seconds) setSeconds(model.max_seconds);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelId]);

  useEffect(() => {
    api<{ has_token: boolean }>("/api/hf-token").then((r) => setHasHf(r.has_token)).catch(handle);
  }, [handle]);

  const loadWorks = useCallback(async () => {
    try {
      setWorks(await api<TaskSummary[]>("/api/tasks?kind=generate"));
    } catch {
      /* the gallery is best-effort */
    }
  }, []);
  usePoll(loadWorks, works?.some((w) => ["queued", "running"].includes(w.status)) ? 2500 : 8000);

  // what is already downloaded on the chosen runtime (for the first-run estimate)
  const realRuntime = target.runtime && target.runtime !== "auto" ? target.runtime : null;
  useEffect(() => {
    setStatus(null);
    if (!realRuntime) return;
    api<AgentStatus>(`/api/runtimes/${realRuntime}/agent`).then(setStatus).catch(() => {});
  }, [realRuntime, prefetchId]);

  const slots = IMAGE_SLOTS[mode];
  const ready = images.filter((f) => f.status === "done");
  const uploading = images.some((f) => f.status === "uploading");
  const [w, h] = model ? sizeFor(model, aspect, hd) : [0, 0];
  const isEdit = mode === "edit";
  const suggest = model?.gpus[0] ?? "L4";
  const current = runtimes.find((r) => r.id === target.runtime);
  const gpuOk = !model || !current || model.gpus.includes(current.accelerator);
  const needsHf = !!model?.gated && hasHf === false;
  const imagesOk = !slots || (ready.length >= slots.min && ready.length <= slots.max);
  const prompts = (perLine ? prompt.split("\n") : [prompt]).map((p) => p.trim()).filter(Boolean);
  const total = prompts.length * Number(count);
  const canStart = !!target.runtime && !!model && prompts.length > 0 && imagesOk && !uploading && !needsHf && !submitting;

  const template = model?.templates[mode];
  const downloaded = status?.installed?.comfyui?.templates?.includes(template ?? "");
  const lastRun = useMemo(
    () => works?.find((t) => t.status === "done" && t.gen?.model === modelId && t.result?.gen_seconds != null && (!current || t.accelerator === current.accelerator)),
    [works, modelId, current],
  );
  const rate = (accel: string | null | undefined) => specs?.find((c) => c.accelerator === accel)?.options[0]?.cu_per_hour ?? null;

  const list = useMemo(() => Object.entries(models).filter(([, m]) => kind === "all" || m.kind === kind), [models, kind]);

  async function saveHf() {
    try {
      const r = await put<{ has_token: boolean }>("/api/hf-token", { token: hfInput });
      setHasHf(r.has_token);
      setHfInput("");
      toast("success", "Hugging Face Token 已保存到系统钥匙串。");
    } catch (e) {
      handle(e);
    }
  }

  async function start() {
    if (!target.runtime || !model) return;
    setSubmitting(true);
    let first: string | null = null;
    let made = 0;
    try {
      for (const p of prompts) {
        for (let i = 0; i < Number(count); i++) {
          const t = await submitTask({
            kind: "generate",
            target,
            accelerator: suggest,
            params: {
              model: modelId,
              mode,
              prompt: p,
              width: w,
              height: h,
              seconds: model.kind === "video" ? seconds : undefined,
              seed: seed.trim() ? Number(seed) + i : undefined,
              fast: model.fast ? fast : undefined,
            },
            uploads: slots ? ready.flatMap((f) => (f.id ? [f.id] : [])) : [],
          });
          if (!t) return;
          first = first ?? t.id;
          made++;
        }
      }
    } finally {
      setSubmitting(false);
      if (first) setOpenId(first);
      if (made > 1) toast("info", `已提交 ${made} 个生成任务，会在同一张卡上依次进行。`);
      loadWorks();
    }
  }

  async function prefetch() {
    if (!realRuntime) return;
    const t = await submitTask({ kind: "install", target: { runtime: realRuntime, release: false }, accelerator: suggest, params: { recipe: "comfyui", gen_models: [modelId] } });
    if (t) setPrefetchId(t.id);
  }

  function reuse(t: TaskSummary) {
    const g = t.gen;
    if (!g) return;
    setModelId(g.model);
    window.setTimeout(() => {
      const m = models[g.model];
      setMode(g.mode);
      setPrompt(g.prompt);
      setPerLine(false);
      if (m) {
        setAspect(closestAspect(g.width, g.height));
        setHd(!!m.hd_size && g.width * g.height > m.size[0] * m.size[1] * 1.2);
      }
      if (g.seconds) setSeconds(g.seconds);
      if (g.fast != null) setFast(g.fast);
      setSeed(t.result?.seed != null ? String(t.result.seed) : "");
      window.scrollTo({ top: 0, behavior: "smooth" });
    }, 0);
    toast("info", "已填入这张作品的模型、提示词和种子");
  }

  const estimate: string[] = [];
  if (model) {
    const dlMin = Math.max(1, Math.round((model.download_gb * 1024) / DOWNLOAD_MBPS / 60));
    if (target.runtime === "auto" && !runtimes.some((r) => r.owned && r.accelerator === suggest)) {
      estimate.push(`先申请 ${suggest}（1–3 分钟），再部署 ComfyUI 并下载约 ${model.download_gb} GB 模型（约 ${dlMin + 2} 分钟）`);
    } else if (realRuntime && status && !downloaded) {
      estimate.push(`这张卡上还没有这个模型：第一次要下载约 ${model.download_gb} GB（约 ${dlMin} 分钟）`);
    } else if (realRuntime && downloaded) {
      estimate.push("模型已在这张卡上，直接开始生成");
    }
    if (lastRun?.result?.gen_seconds != null) {
      const r = lastRun.cu_per_hour ?? rate(lastRun.accelerator);
      const cost = r != null ? ` ≈ ${cu((lastRun.result.gen_seconds / 3600) * r)}` : "";
      estimate.push(`上次在 ${lastRun.accelerator} 上每个生成用了 ${duration(lastRun.result.gen_seconds)}${cost}`);
    }
  }

  const visibleWorks = (works ?? []).slice(0, shownWorks);

  return (
    <>
      <PageHead
        eyebrow="03 / 创作 · 生图与生视频"
        title="创作"
        accent=""
        lede="用开源模型在你的 Colab 显卡上生图、生视频。模型和工作流来自 ComfyUI 官方模板，第一次使用会自动部署 ComfyUI 并下载模型。"
      />

      <div className="create-grid">
        <section className="panel rise">
          <div className="panel-head">
            <h2>选模型</h2>
            <Seg
              value={kind}
              onChange={setKind}
              options={[
                { value: "all", label: "全部" },
                { value: "video", label: "视频" },
                { value: "image", label: "图像" },
              ]}
            />
          </div>
          <div className="gen-list">
            {list.map(([id, m]) => (
              <button key={id} type="button" className={`gen-item ${id === modelId ? "on" : ""}`} onClick={() => setModelId(id)}>
                <Pic src={`/img/${m.kind === "video" ? "feat-video" : "feat-image"}.webp`} className="gen-pic" fallback={<ChipArt size={24} />} />
                <span className="gen-text">
                  <span className="gen-name">
                    {m.name}
                    {m.experimental && <span className="tag">实验性</span>}
                  </span>
                  <span className="subtle small">{m.vendor} · {m.kind === "video" ? "视频" : "图像"} · 显存 ≈ {m.vram_gb} GB · 下载约 {m.download_gb} GB</span>
                </span>
              </button>
            ))}
          </div>
        </section>

        <div className="col">
          {model && (
            <section className="panel rise" style={{ animationDelay: "40ms" }}>
              <div className="panel-head">
                <h2>{model.name}</h2>
                <span className="mono subtle small">{model.license}</span>
              </div>
              <p className="model-summary">{model.summary}</p>
              <div className="uses">
                <span className={`use ${model.commercial === "yes" ? "ok-use" : ""}`}>{COMMERCIAL[model.commercial]}</span>
                <span className="use">推荐 {model.gpus.join(" / ")}</span>
                {model.fps && <span className="use">{model.fps} fps · 最长 {model.max_seconds} 秒</span>}
                {model.gated && <span className="use">需 HF 授权</span>}
              </div>
              {model.license_note && <p className="model-note">{model.license_note}</p>}
              {model.gated && hasHf === false && (
                <div className="api-box">
                  <p className="small">
                    这个模型的权重需要先在 Hugging Face 打开{" "}
                    <a className="link" href={`https://huggingface.co/${model.gated}`} target="_blank" rel="noreferrer">
                      {model.gated}
                    </a>{" "}
                    申请访问并同意许可，再在这里填一个只读 Token（在 huggingface.co/settings/tokens 创建）。
                  </p>
                  <div className="chat-row">
                    <input className="input" type="password" placeholder="hf_…" value={hfInput} onChange={(e) => setHfInput(e.target.value)} />
                    <button className="btn" onClick={saveHf} disabled={!hfInput.trim()}>
                      保存
                    </button>
                  </div>
                </div>
              )}
            </section>
          )}

          <Card title="选择显卡" hint={model ? `建议 ${model.gpus.join(" / ")}` : undefined} delay={80}>
            <RuntimePicker value={target} onChange={setTarget} minVram={model?.vram_gb ?? 0} gpus={model?.gpus} suggest={suggest} releaseByDefault={false} />
            {!gpuOk && <p className="err-text small">这张卡不在推荐列表里，可能显存或内存不够，建议换 {model?.gpus.join(" / ")}。</p>}
            {realRuntime && status && !downloaded && model && (
              <div className="row-between prefetch">
                <span className="subtle small">可以先把模型下载好，写提示词的时候它就在后台准备。</span>
                <button className="btn ghost narrow" onClick={prefetch} disabled={!!prefetchId && !status}>
                  现在就下载（{model.download_gb} GB）
                </button>
              </div>
            )}
            {prefetchId && <TaskPanel taskId={prefetchId} compact onDone={() => setPrefetchId(null)} />}
          </Card>

          <Card title="描述你想要的画面" delay={120}>
            {modes.length > 1 && (
              <Field label="模式">
                <Seg value={mode} onChange={setMode} options={modes.map((m) => ({ value: m, label: catalog?.mode_labels[m] ?? m }))} />
              </Field>
            )}
            <Field label={perLine ? `提示词 · 每行一条（${prompts.length} 条）` : "提示词"} hint="中英文都可以，写清主体、动作、场景、镜头和光线">
              <textarea
                className="input textarea"
                rows={perLine ? 6 : 4}
                value={prompt}
                placeholder={model ? `例如：${EXAMPLES[model.kind]}` : ""}
                onChange={(e) => setPrompt(e.target.value)}
              />
            </Field>
            {!prompt && model && (
              <button type="button" className="link small-link" onClick={() => setPrompt(EXAMPLES[model.kind])}>
                填入示例
              </button>
            )}
            {slots && (
              <Field label={slots.labels.join(" / ")} hint={slots.hint}>
                <FileDrop files={images} onChange={setImages} accept="image/*" multiple={slots.max > 1} label="拖入图片" />
              </Field>
            )}
            <div className="lang-row">
              {!isEdit && (
                <Field label={`画幅 · ${w}×${h}`}>
                  <select className="input" value={aspect} onChange={(e) => setAspect(e.target.value)}>
                    {ASPECTS.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.label}
                      </option>
                    ))}
                  </select>
                </Field>
              )}
              {!isEdit && model?.hd_size && (
                <Field label="清晰度">
                  <Seg
                    value={hd ? "hd" : "sd"}
                    onChange={(v) => setHd(v === "hd")}
                    options={[
                      { value: "sd", label: "标准" },
                      { value: "hd", label: "高清" },
                    ]}
                  />
                </Field>
              )}
              <Field label="种子（留空随机）">
                <input className="input mono" value={seed} placeholder="随机" onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ""))} />
              </Field>
            </div>
            {model?.kind === "video" && (
              <Field label={`时长 ${seconds} 秒`} hint="越长越慢，显存占用也越高">
                <input type="range" min={1} max={model.max_seconds ?? 5} step={1} value={seconds} onChange={(e) => setSeconds(Number(e.target.value))} />
              </Field>
            )}
            <div className="lang-row">
              <Field label="每条提示词生成">
                <Seg
                  value={count}
                  onChange={setCount}
                  options={[
                    { value: "1", label: "1 个" },
                    { value: "2", label: "2 个" },
                    { value: "4", label: "4 个" },
                  ]}
                />
              </Field>
              <Toggle checked={perLine} onChange={setPerLine} label="批量：每行一条提示词" />
            </div>
            {model?.fast && <Toggle checked={fast} onChange={setFast} label={`加速生成（${model.fast}，快几倍、画质略降）`} />}
            {hd && model?.kind === "video" && <p className="subtle small">高清视频更吃显存和时间，建议 A100 80GB / RTX PRO 6000 / H100。</p>}
            {estimate.length > 0 && (
              <ul className="estimate">
                {estimate.map((e) => (
                  <li key={e}>{e}</li>
                ))}
              </ul>
            )}
            <button className="btn primary full" disabled={!canStart} onClick={start}>
              {uploading ? "等待图片上传…" : submitting ? "提交中…" : `${model?.kind === "video" ? "生成视频" : "生成图片"}${total > 1 ? ` × ${total}` : ""}`} <span className="arrow">→</span>
            </button>
          </Card>
        </div>
      </div>

      <section className="block rise" style={{ animationDelay: "160ms" }}>
        <div className="block-head">
          <h2>我的作品</h2>
          <span className="subtle small">保存在 ~/GPUDock/outputs，点一张看详情</span>
        </div>
        {openId && (
          <Card delay={0}>
            <TaskPanel key={openId} taskId={openId} onRetried={(t) => setOpenId(t.id)} onDone={loadWorks} onDeleted={() => { setOpenId(null); loadWorks(); }} />
          </Card>
        )}
        {works && works.length === 0 ? (
          <div className="empty-state">
            <ChipArt size={36} />
            <div>
              <b>还没有作品</b>
              <p className="subtle small">写一段提示词，生成第一张图或第一段视频。</p>
            </div>
          </div>
        ) : (
          <div className="gallery">
            {visibleWorks.map((t) => (
              <WorkCard
                key={t.id}
                task={t}
                model={t.gen ? models[t.gen.model] : undefined}
                open={t.id === openId}
                onOpen={() => setOpenId(t.id === openId ? null : t.id)}
                onReuse={() => reuse(t)}
                onReroll={async () => {
                  const n = await retryTask(t.id, { seed: null });
                  if (n) {
                    setOpenId(n.id);
                    loadWorks();
                  }
                }}
              />
            ))}
          </div>
        )}
        {works && works.length > shownWorks && (
          <button className="btn ghost" onClick={() => setShownWorks((n) => n + 24)}>
            显示更多（还有 {works.length - shownWorks} 个）
          </button>
        )}
      </section>
    </>
  );
}

function WorkCard({
  task,
  model,
  open,
  onOpen,
  onReuse,
  onReroll,
}: {
  task: TaskSummary;
  model?: GenModel;
  open: boolean;
  onOpen: () => void;
  onReuse: () => void;
  onReroll: () => void;
}) {
  const media = task.outputs.find((o) => o.kind === "image" || o.kind === "video");
  const active = ["queued", "running"].includes(task.status);
  const g = task.gen;
  return (
    <article className={`work ${open ? "on" : ""}`}>
      <button type="button" className="work-media" onClick={onOpen} aria-label="查看详情">
        {media?.kind === "image" && <img src={media.url} alt={g?.prompt ?? ""} loading="lazy" />}
        {media?.kind === "video" && (
          <video
            src={media.url}
            muted
            loop
            playsInline
            preload="metadata"
            onMouseEnter={(e) => e.currentTarget.play().catch(() => {})}
            onMouseLeave={(e) => e.currentTarget.pause()}
          />
        )}
        {!media && (
          <span className={`work-state ${task.status}`}>
            {active ? (
              <>
                <span className="mono">{Math.round(task.progress * 100)}%</span>
                <span className="small">{task.stage}</span>
                <span className="mini-bar wide"><span style={{ width: `${Math.round(task.progress * 100)}%` }} /></span>
              </>
            ) : (
              <span className="small">{task.status === "error" ? "失败" : task.status === "cancelled" ? "已取消" : task.status === "interrupted" ? "已中断" : "没有输出"}</span>
            )}
          </span>
        )}
        {media?.kind === "video" && <span className="work-kind">视频</span>}
      </button>
      <div className="work-body">
        <p className="work-prompt" title={g?.prompt}>{g?.prompt ?? task.title}</p>
        <div className="subtle small work-meta">
          {[model?.name, g && `${g.width}×${g.height}`, task.result?.seed != null && `种子 ${task.result.seed}`, ago(task.created)].filter(Boolean).join(" · ")}
        </div>
        <div className="work-actions">
          <button className="link" onClick={onReuse} disabled={!g}>复用参数</button>
          <button className="link" onClick={onReroll} disabled={!task.can_retry}>换个种子</button>
        </div>
      </div>
    </article>
  );
}
