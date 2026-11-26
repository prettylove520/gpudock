import { useEffect, useMemo, useState } from "react";
import { api, put } from "../api";
import { useApp, type Target } from "../ctx";
import { Card, Field, FileDrop, PageHead, RuntimePicker, Seg, Toggle, type PickedFile } from "../components/common";
import TaskPanel from "../components/TaskPanel";
import { useStoredTask } from "./shared";

type Translator = "runtime" | "api" | "none";
type VoiceMode = "preset" | "clone";

const API_PRESETS = [
  { label: "DeepSeek", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  { label: "通义千问", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
  { label: "OpenAI", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini" },
];

interface TranslatorCfg {
  base_url: string;
  model: string;
  has_key: boolean;
}

export default function Dub() {
  const { catalog, handle, toast, submitTask } = useApp();
  const [target, setTarget] = useState<Target>({ runtime: null, release: true });
  const [files, setFiles] = useState<PickedFile[]>([]);
  const [src, setSrc] = useState("auto");
  const [tgt, setTgt] = useState("zh");
  const [translator, setTranslator] = useState<Translator>("runtime");
  const [voiceMode, setVoiceMode] = useState<VoiceMode>("preset");
  const [voice, setVoice] = useState("");
  const [keepBgm, setKeepBgm] = useState(true);
  const [review, setReview] = useState(true);
  const [speed, setSpeed] = useState(1.0);
  const [consent, setConsent] = useState(false);
  const [cfg, setCfg] = useState<TranslatorCfg | null>(null);
  const [cfgForm, setCfgForm] = useState({ base_url: "", model: "", api_key: "" });
  const [taskId, setTaskId] = useStoredTask("dub");
  const [starting, setStarting] = useState(false);

  const kokoroCode = catalog?.langs[tgt]?.kokoro ?? null;
  const voices = useMemo(() => (kokoroCode && catalog ? catalog.kokoro_voices[kokoroCode] ?? [] : []), [catalog, kokoroCode]);
  useEffect(() => {
    if (!voices.some((v) => v.id === voice)) setVoice(voices[0]?.id ?? "");
  }, [voices, voice]);
  useEffect(() => {
    if (catalog && !kokoroCode && voiceMode === "preset") setVoiceMode("clone");
  }, [catalog, kokoroCode, voiceMode]);

  useEffect(() => {
    api<TranslatorCfg>("/api/translator")
      .then((c) => {
        setCfg(c);
        setCfgForm({ base_url: c.base_url, model: c.model, api_key: "" });
      })
      .catch(handle);
  }, [handle]);

  async function saveCfg() {
    try {
      const c = await put<TranslatorCfg>("/api/translator", { ...cfgForm, api_key: cfgForm.api_key || null });
      setCfg(c);
      setCfgForm((f) => ({ ...f, api_key: "" }));
      toast("success", "翻译 API 已保存，密钥存在系统钥匙串里。");
    } catch (e) {
      handle(e);
    }
  }

  const ready = files.filter((f) => f.status === "done");
  const uploading = files.some((f) => f.status === "uploading");
  const apiReady = translator !== "api" || !!(cfg?.base_url && cfg?.model && cfg?.has_key);
  const suggest = voiceMode === "clone" ? "L4" : "T4";
  const canStart = !!target.runtime && ready.length > 0 && !uploading && apiReady && (voiceMode === "preset" || consent) && !starting;

  async function start() {
    setStarting(true);
    const t = await submitTask({
      kind: "dub",
      target,
      accelerator: suggest,
      params: { src_lang: src, tgt_lang: tgt, translator, voice_mode: voiceMode, voice, keep_bgm: keepBgm, review, speed, consent },
      uploads: ready.flatMap((f) => (f.id ? [f.id] : [])),
    });
    setStarting(false);
    if (t) {
      setTaskId(t.id);
      setFiles([]);
    }
  }

  const langs = catalog ? Object.entries(catalog.langs) : [];

  return (
    <>
      <PageHead
        eyebrow="05 / 批量配音 · 识别 → 翻译 → 校对 → 配音 → 混音"
        title="批量配音" accent=""
        lede="把视频一键翻译成另一种语言并重新配音，保留背景音乐，同时输出单语和双语字幕。需要的模型会在第一次使用时自动部署（约 5–10 分钟）。"
      />
      <div className="two-col">
        <div className="col">
          <Card title="1. 选择显卡" hint="预置音色用 T4 即可；克隆原声需要 L4 或更好" delay={40}>
            <RuntimePicker value={target} onChange={setTarget} minVram={voiceMode === "clone" ? 12 : 8} bigRam={voiceMode === "clone"} suggest={suggest} />
            {review && target.release && <p className="subtle small">开了“先让我校对”：校对期间显卡会一直等你，确认后才继续并在完成时释放。</p>}
          </Card>
          <Card title="2. 添加视频或音频" delay={80}>
            <FileDrop files={files} onChange={setFiles} accept="video/*,audio/*" label="拖入要配音的文件" />
          </Card>
          <Card title="3. 语言" delay={120}>
            <div className="lang-row">
              <Field label="原语言">
                <select className="input" value={src} onChange={(e) => setSrc(e.target.value)}>
                  <option value="auto">自动识别</option>
                  {langs.map(([k, v]) => <option key={k} value={k}>{v.name}</option>)}
                </select>
              </Field>
              <span className="lang-arrow">→</span>
              <Field label="配成">
                <select className="input" value={tgt} onChange={(e) => setTgt(e.target.value)}>
                  {langs.map(([k, v]) => <option key={k} value={k}>{v.name}</option>)}
                </select>
              </Field>
            </div>
            <Field label="翻译方式">
              <Seg
                value={translator}
                onChange={setTranslator}
                options={[
                  { value: "runtime", label: "运行时内 Qwen3（免费）" },
                  { value: "api", label: "外部 API" },
                  { value: "none", label: "不翻译（同语言重配）" },
                ]}
              />
            </Field>
            {translator === "api" && (
              <div className="api-box">
                <div className="chips">
                  {API_PRESETS.map((p) => (
                    <button key={p.label} type="button" className="snippet" onClick={() => setCfgForm((f) => ({ ...f, base_url: p.base_url, model: p.model }))}>
                      {p.label}
                    </button>
                  ))}
                </div>
                <Field label="API 地址（OpenAI 兼容）">
                  <input className="input" value={cfgForm.base_url} placeholder="https://api.deepseek.com/v1" onChange={(e) => setCfgForm({ ...cfgForm, base_url: e.target.value })} />
                </Field>
                <div className="lang-row">
                  <Field label="模型">
                    <input className="input" value={cfgForm.model} placeholder="deepseek-chat" onChange={(e) => setCfgForm({ ...cfgForm, model: e.target.value })} />
                  </Field>
                  <Field label={cfg?.has_key ? "密钥（已保存，留空不改）" : "密钥"}>
                    <input className="input" type="password" value={cfgForm.api_key} placeholder="sk-…" onChange={(e) => setCfgForm({ ...cfgForm, api_key: e.target.value })} />
                  </Field>
                </div>
                <div className="row-gap">
                  <button className="btn ghost" onClick={saveCfg}>保存</button>
                  <span className="subtle small">翻译在你的电脑上调用，密钥不会上传到 Colab。</span>
                </div>
              </div>
            )}
          </Card>
        </div>
        <div className="col">
          <Card title="4. 声音" delay={160}>
            <Seg
              value={voiceMode}
              onChange={setVoiceMode}
              options={[
                { value: "preset", label: "预置音色（Kokoro）", disabled: !kokoroCode },
                { value: "clone", label: "克隆原声（VoxCPM2）" },
              ]}
            />
            {voiceMode === "preset" ? (
              <Field label="音色">
                <div className="voice-grid">
                  {voices.map((v) => (
                    <button key={v.id} type="button" className={`voice ${voice === v.id ? "on" : ""}`} onClick={() => setVoice(v.id)}>
                      {v.label}
                    </button>
                  ))}
                </div>
              </Field>
            ) : (
              <label className="consent">
                <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
                <span>
                  我确认拥有视频中说话人的声音使用授权（例如本人或已获同意）。克隆他人声音用于冒充、欺骗属于违规用途，Colab 也明令禁止生成 deepfake。
                </span>
              </label>
            )}
            <Field label={`语速 ${speed.toFixed(2)}×`} hint="太快的句子会自动加速对齐，最多 1.35 倍">
              <input type="range" min={0.8} max={1.3} step={0.05} value={speed} onChange={(e) => setSpeed(Number(e.target.value))} />
            </Field>
            <Toggle checked={keepBgm} onChange={setKeepBgm} label="保留背景音乐和音效" hint="用 Demucs 把人声去掉，再把配音混进原背景" />
            <Toggle checked={review} onChange={setReview} label="翻译后先让我校对" hint="推荐：改好译文再配音，效果更好" />
            <button className="btn primary full" disabled={!canStart} onClick={start}>
              {uploading ? "等待文件上传…" : `开始配音 ${ready.length ? `${ready.length} 个文件` : ""}`} <span className="arrow">→</span>
            </button>
            {!apiReady && <p className="err-text small">请先保存外部翻译 API 的地址、模型和密钥。</p>}
          </Card>
        </div>
      </div>
      {taskId && (
        <Card title="任务进度" delay={0}>
          <TaskPanel taskId={taskId} onRetried={(t) => setTaskId(t.id)} />
        </Card>
      )}
    </>
  );
}
