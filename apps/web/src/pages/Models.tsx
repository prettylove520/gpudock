import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useApp, type Target } from "../ctx";
import type { AgentStatus, GenModel, Recipe } from "../types";
import { Card, Field, PageHead, RuntimePicker } from "../components/common";
import TaskPanel from "../components/TaskPanel";
import { ChipArt, Pic } from "../components/Logo";

const CATEGORIES: { id: Recipe["category"]; label: string; hint: string }[] = [
  { id: "asr", label: "语音识别", hint: "语音 → 带时间轴的文字" },
  { id: "tts", label: "语音合成", hint: "文字 → 语音" },
  { id: "audio", label: "音频处理", hint: "人声与背景分离" },
  { id: "llm", label: "大语言模型", hint: "翻译、改写、对话" },
  { id: "engine", label: "生成引擎", hint: "生图、生视频的运行环境，用“创作”时会自动部署" },
];
const CAT_HUE: Record<string, string> = { asr: "#6f8a64", tts: "#3f7d83", audio: "#a7843f", llm: "#b4553b", engine: "#5b6f97" };
const CAT_IMG: Record<string, string> = { asr: "feat-transcribe", tts: "feat-tts", audio: "feat-dub", llm: "feat-models", engine: "feat-image" };
const COMMERCIAL: Record<string, string> = { yes: "可商用", conditional: "有条件商用", unknown: "商用条款见仓库", no: "非商用" };

export default function Models() {
  const { catalog, handle, runtimes, go, submitTask } = useApp();
  const [target, setTarget] = useState<Target>({ runtime: null, release: false });
  const runtime = target.runtime;
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [loading, setLoading] = useState(false);
  // deployments keep running when you leave the page; remember them so their progress shows again
  const [installs, setInstallsState] = useState<Record<string, string>>(() => JSON.parse(sessionStorage.getItem("gpudock.installs") || "{}"));
  const setInstalls = (fn: (m: Record<string, string>) => Record<string, string>) =>
    setInstallsState((m) => {
      const next = fn(m);
      sessionStorage.setItem("gpudock.installs", JSON.stringify(next));
      return next;
    });
  const [prompt, setPrompt] = useState("");
  const [chatId, setChatId] = useState<string | null>(null);
  const [llm, setLlm] = useState("qwen3:8b");

  const load = useCallback(
    async (refresh = false) => {
      if (!runtime) return;
      setLoading(true);
      try {
        setStatus(await api<AgentStatus>(`/api/runtimes/${runtime}/agent?refresh=${refresh}`));
      } catch (e) {
        handle(e);
      } finally {
        setLoading(false);
      }
    },
    [runtime, handle],
  );
  useEffect(() => {
    setStatus(null);
    load();
  }, [load]);

  async function install(id: string, params: Record<string, unknown> = { recipe: id }) {
    if (!runtime) return;
    const t = await submitTask({ kind: "install", target, accelerator: "", params });
    if (t) setInstalls((m) => ({ ...m, [id]: t.id }));
  }

  async function chat() {
    if (!runtime || !prompt.trim()) return;
    const t = await submitTask({ kind: "chat", target, accelerator: "", params: { prompt, model: llm } });
    if (t) setChatId(t.id);
  }

  const installed = status?.installed ?? {};
  const current = runtimes.find((r) => r.id === runtime);
  const templates = new Set(installed.comfyui?.templates ?? []);
  const pulled = new Set([...(installed.qwen?.models ?? []), ...(installed.qwen?.model ? [installed.qwen.model] : [])]);
  const llmInfo = catalog?.llm_models.find((m) => m.id === llm);
  const genState = (m: GenModel) => {
    const t = Object.values(m.templates);
    const n = t.filter((x) => templates.has(x)).length;
    return n === 0 ? "none" : n === t.length ? "all" : "some";
  };

  return (
    <>
      <PageHead
        eyebrow="02 / 模型库 · 开源模型一键部署到你的运行时"
        title="模型库" accent=""
        lede="部署只在这个运行时里有效，释放运行时后需要重新部署。批量任务会自动部署用到的模型，这里可以提前部署和试用。"
      />
      <Card title="部署到" hint={status?.python ? `Python ${status.python} · ${status.gpu ?? "CPU"}` : undefined} delay={40}>
        <div className="row-between">
          <RuntimePicker value={target} onChange={setTarget} allowAuto={false} />
          {runtime && (
            <button className="btn ghost" onClick={() => load(true)} disabled={loading}>
              {loading ? "检查中…" : "刷新状态"}
            </button>
          )}
        </div>
        {status?.busy && <p className="subtle small">运行时正在执行任务，部署状态稍后刷新。</p>}
      </Card>

      {catalog &&
        CATEGORIES.map((cat, ci) => {
          const items = Object.entries(catalog.recipes).filter(([, r]) => r.category === cat.id);
          if (!items.length) return null;
          return (
            <section key={cat.id} className="block rise" style={{ animationDelay: `${80 + ci * 40}ms` }}>
              <div className="block-head">
                <h2>{cat.label}</h2>
                <span className="subtle small">{cat.hint}</span>
              </div>
              <div className="model-grid">
                {items.map(([id, r]) => {
                  const done = !!installed[id];
                  const taskId = installs[id];
                  return (
                    <div key={id} className="model-card" style={{ ["--hue" as string]: CAT_HUE[cat.id] }}>
                      <div className="model-top">
                        <Pic src={`/img/${CAT_IMG[cat.id]}.webp`} className="model-pic" fallback={<ChipArt size={28} />} />
                        <div className="model-name">
                          <h3>{r.name}</h3>
                          <div className="subtle small mono">{r.engine}</div>
                        </div>
                        {done ? <span className="status-pill idle">已部署</span> : <span className="tag">未部署</span>}
                      </div>
                      <p className="model-summary">{r.summary}</p>
                      {r.note && <p className="model-note">{r.note}</p>}
                      <div className="uses">
                        <span className="use">{r.license}</span>
                        <span className={`use ${r.commercial === "yes" ? "ok-use" : ""}`}>{r.commercial === "yes" ? "可商用" : "非商用"}</span>
                        {r.vram_gb > 0 && <span className="use">显存 ≈ {r.vram_gb} GB</span>}
                        <span className="use">{r.download}</span>
                        <span className="use">部署约 {r.install_minutes} 分钟</span>
                      </div>
                      {taskId && <TaskPanel taskId={taskId} compact onDone={() => load(true)} onRetried={(t) => setInstalls((m) => ({ ...m, [id]: t.id }))} />}
                      <div className="row-between">
                        <a className="link" href={r.homepage} target="_blank" rel="noreferrer">项目主页 ↗</a>
                        <button className="btn narrow" disabled={!runtime || done} onClick={() => install(id)}>
                          {done ? "已就绪" : "部署"}
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </section>
          );
        })}

      {catalog && (
        <section className="block rise" style={{ animationDelay: "240ms" }}>
          <div className="block-head">
            <h2>生图与生视频</h2>
            <span className="subtle small">通过 ComfyUI 官方工作流运行，在“创作”页使用；这里可以提前把模型下载到运行时</span>
          </div>
          <div className="model-grid">
            {Object.entries(catalog.gen_models).map(([id, m]) => {
              const st = genState(m);
              const key = `gen:${id}`;
              const taskId = installs[key];
              return (
                <div key={id} className="model-card" style={{ ["--hue" as string]: m.kind === "video" ? "#b4553b" : "#a7843f" }}>
                  <div className="model-top">
                    <Pic src={`/img/${m.kind === "video" ? "feat-video" : "feat-image"}.webp`} className="model-pic" fallback={<ChipArt size={28} />} />
                    <div className="model-name">
                      <h3>
                        {m.name} {m.experimental && <span className="tag">实验性</span>}
                      </h3>
                      <div className="subtle small mono">
                        {m.vendor} · {Object.keys(m.templates).map((k) => catalog.mode_labels[k] ?? k).join(" / ")}
                      </div>
                    </div>
                    {st === "all" ? <span className="status-pill idle">已下载</span> : st === "some" ? <span className="tag">部分已下载</span> : <span className="tag">未下载</span>}
                  </div>
                  <p className="model-summary">{m.summary}</p>
                  {m.license_note && <p className="model-note">{m.license_note}</p>}
                  <div className="uses">
                    <span className="use">{m.license}</span>
                    <span className={`use ${m.commercial === "yes" ? "ok-use" : ""}`}>{COMMERCIAL[m.commercial]}</span>
                    <span className="use">显存 ≈ {m.vram_gb} GB</span>
                    <span className="use">下载约 {m.download_gb} GB</span>
                    <span className="use">推荐 {m.gpus.join(" / ")}</span>
                    {m.gated && <span className="use">需 HF 授权</span>}
                  </div>
                  {taskId && <TaskPanel taskId={taskId} compact onDone={() => load(true)} onRetried={(t) => setInstalls((m) => ({ ...m, [key]: t.id }))} />}
                  <div className="row-between">
                    <button
                      className="link"
                      onClick={() => {
                        sessionStorage.setItem("gpudock.create.model", id);
                        go("create");
                      }}
                    >
                      用它创作 →
                    </button>
                    <button
                      className="btn narrow"
                      disabled={!runtime || st === "all"}
                      onClick={() => install(key, { recipe: "comfyui", gen_models: [id] })}
                    >
                      {st === "all" ? "已就绪" : "下载模型"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      )}

      <Card title="试一试大模型" hint={current ? `在 ${current.display_name} 上运行` : "先选择一个运行时"} delay={280}>
        <Field label="模型" hint={llmInfo ? `${llmInfo.note} · 显存 ≈ ${llmInfo.vram_gb} GB${pulled.has(llm) ? " · 已下载" : ""}` : undefined}>
          <select className="input" value={llm} onChange={(e) => setLlm(e.target.value)}>
            {catalog?.llm_models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
        </Field>
        <div className="chat-row">
          <textarea
            className="input textarea"
            rows={3}
            value={prompt}
            placeholder="例如：把这句话翻译成英文：今天的视频我们来聊聊开源 GPU 工具。"
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") chat();
            }}
          />
          <button className="btn primary" disabled={!runtime || !prompt.trim()} onClick={chat}>
            发送 <span className="arrow">→</span>
          </button>
        </div>
        <p className="subtle small">第一次用某个模型会先自动下载（8B 约 5 GB，27B 约 17 GB）。部署好后，批量配音的翻译也会用它。</p>
        {chatId && <TaskPanel taskId={chatId} compact />}
      </Card>
    </>
  );
}
