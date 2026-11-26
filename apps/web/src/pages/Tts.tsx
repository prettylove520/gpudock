import { useEffect, useMemo, useState } from "react";
import { post } from "../api";
import { useApp } from "../ctx";
import type { TaskDetail } from "../types";
import { Card, Field, FileDrop, PageHead, RuntimePicker, Seg, Toggle, type PickedFile } from "../components/common";
import TaskPanel from "../components/TaskPanel";
import { useStoredTask } from "./shared";

type Engine = "kokoro" | "voxcpm";

export default function Tts() {
  const { catalog, handle } = useApp();
  const [runtime, setRuntime] = useState<string | null>(null);
  const [text, setText] = useState("欢迎使用 GPUDock，这是第一句。\n每一行都会生成一个独立的音频文件。");
  const [engine, setEngine] = useState<Engine>("kokoro");
  const [lang, setLang] = useState("zh");
  const [voice, setVoice] = useState("");
  const [speed, setSpeed] = useState(1.0);
  const [merge, setMerge] = useState(true);
  const [ref, setRef] = useState<PickedFile[]>([]);
  const [consent, setConsent] = useState(false);
  const [taskId, setTaskId] = useStoredTask("tts");
  const [previewId, setPreviewId] = useState<string | null>(null);

  const code = catalog?.langs[lang]?.kokoro ?? null;
  const voices = useMemo(() => (code && catalog ? catalog.kokoro_voices[code] ?? [] : []), [catalog, code]);
  useEffect(() => {
    if (!voices.some((v) => v.id === voice)) setVoice(voices[0]?.id ?? "");
  }, [voices, voice]);
  useEffect(() => {
    if (catalog && !code && engine === "kokoro") setEngine("voxcpm");
  }, [catalog, code, engine]);

  const lines = text.split("\n").filter((l) => l.trim());
  const refReady = ref.find((f) => f.status === "done");
  const cloneOk = engine === "kokoro" || !refReady || consent;

  async function run(kind: "tts" | "tts_preview") {
    if (!runtime) return;
    try {
      const t = await post<TaskDetail>("/api/tasks", {
        kind,
        runtime,
        params: { engine, lang, voice, speed, merge, consent, text: kind === "tts_preview" ? lines[0] : text },
        uploads: engine === "voxcpm" && refReady?.id ? [refReady.id] : [],
      });
      if (kind === "tts") setTaskId(t.id);
      else setPreviewId(t.id);
    } catch (e) {
      handle(e);
    }
  }

  return (
    <>
      <PageHead
        eyebrow="06 / 批量朗读 · Kokoro 预置音色，VoxCPM2 声音克隆"
        title="批量朗读" accent=""
        lede="一行文字生成一个音频，可以合并成一整段。适合有声书、口播稿、课程旁白。"
      />
      <div className="two-col">
        <div className="col">
          <Card title="1. 选择显卡" delay={40}>
            <RuntimePicker value={runtime} onChange={setRuntime} minVram={engine === "voxcpm" ? 10 : 2} bigRam={engine === "voxcpm"} suggest={engine === "voxcpm" ? "L4" : "T4"} />
          </Card>
          <Card title="2. 文字" hint={`${lines.length} 行`} delay={80}>
            <textarea className="input textarea" rows={12} value={text} onChange={(e) => setText(e.target.value)} placeholder="每行一句，空行会被忽略" />
          </Card>
        </div>
        <div className="col">
          <Card title="3. 声音" delay={120}>
            <Field label="语言">
              <select className="input" value={lang} onChange={(e) => setLang(e.target.value)}>
                {catalog && Object.entries(catalog.langs).map(([k, v]) => <option key={k} value={k}>{v.name}</option>)}
              </select>
            </Field>
            <Seg
              value={engine}
              onChange={setEngine}
              options={[
                { value: "kokoro", label: "预置音色（快）", disabled: !code },
                { value: "voxcpm", label: "克隆 / 多语言（VoxCPM2）" },
              ]}
            />
            {engine === "kokoro" ? (
              <div className="voice-grid">
                {voices.map((v) => (
                  <button key={v.id} type="button" className={`voice ${voice === v.id ? "on" : ""}`} onClick={() => setVoice(v.id)}>
                    {v.label}
                  </button>
                ))}
              </div>
            ) : (
              <>
                <FileDrop files={ref} onChange={setRef} accept="audio/*,video/*" multiple={false} label="参考音频（可选，5–15 秒清晰人声）" />
                {refReady && (
                  <label className="consent">
                    <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
                    <span>我确认拥有这段声音的使用授权（本人或已获同意），不会用于冒充或欺骗。</span>
                  </label>
                )}
                {!refReady && <p className="subtle small">不提供参考音频时，VoxCPM2 会用默认音色朗读。</p>}
              </>
            )}
            <Field label={`语速 ${speed.toFixed(2)}×`}>
              <input type="range" min={0.7} max={1.4} step={0.05} value={speed} onChange={(e) => setSpeed(Number(e.target.value))} />
            </Field>
            <Toggle checked={merge} onChange={setMerge} label="额外合并成一个完整音频" />
            <div className="row-gap">
              <button className="btn ghost" disabled={!runtime || !lines.length || !cloneOk} onClick={() => run("tts_preview")}>
                试听第一句
              </button>
              <button className="btn primary" disabled={!runtime || !lines.length || !cloneOk} onClick={() => run("tts")}>
                生成 {lines.length} 段音频 <span className="arrow">→</span>
              </button>
            </div>
            {previewId && <TaskPanel taskId={previewId} compact />}
          </Card>
        </div>
      </div>
      {taskId && (
        <Card title="任务进度" delay={0}>
          <TaskPanel taskId={taskId} />
        </Card>
      )}
    </>
  );
}
