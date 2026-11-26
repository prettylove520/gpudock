import { useState } from "react";
import { post } from "../api";
import { useApp } from "../ctx";
import type { TaskDetail } from "../types";
import { Card, Field, FileDrop, PageHead, RuntimePicker, Seg, Toggle, type PickedFile } from "../components/common";
import TaskPanel from "../components/TaskPanel";
import { useStoredTask } from "./shared";

const MODELS = [
  { value: "large-v3-turbo", label: "Turbo（推荐）" },
  { value: "large-v3", label: "Large v3（最准）" },
  { value: "medium", label: "Medium" },
  { value: "small", label: "Small（最快）" },
];
const FORMATS = [
  { id: "srt", label: "SRT 字幕" },
  { id: "txt", label: "纯文本" },
  { id: "vtt", label: "VTT 字幕" },
  { id: "json", label: "JSON（带时间戳）" },
];

export default function Transcribe() {
  const { catalog, handle } = useApp();
  const [runtime, setRuntime] = useState<string | null>(null);
  const [files, setFiles] = useState<PickedFile[]>([]);
  const [model, setModel] = useState("large-v3-turbo");
  const [language, setLanguage] = useState("auto");
  const [formats, setFormats] = useState<string[]>(["srt", "txt"]);
  const [vad, setVad] = useState(true);
  const [taskId, setTaskId] = useStoredTask("transcribe");
  const [starting, setStarting] = useState(false);

  const ready = files.filter((f) => f.status === "done");
  const uploading = files.some((f) => f.status === "uploading");

  async function start() {
    if (!runtime) return;
    setStarting(true);
    try {
      const t = await post<TaskDetail>("/api/tasks", {
        kind: "transcribe",
        runtime,
        params: { model, language, formats, vad },
        uploads: ready.map((f) => f.id),
      });
      setTaskId(t.id);
      setFiles([]);
    } catch (e) {
      handle(e);
    } finally {
      setStarting(false);
    }
  }

  return (
    <>
      <PageHead eyebrow="04 / 批量转写 · faster-whisper，99 种语言" title="批量转写" accent="" lede="把一批音频或视频转成带时间轴的字幕和文稿。第一次使用会在运行时上自动部署 Whisper（约 2 分钟）。" />
      <div className="two-col">
        <div className="col">
          <Card title="1. 选择显卡" delay={40}>
            <RuntimePicker value={runtime} onChange={setRuntime} minVram={6} />
          </Card>
          <Card title="2. 添加文件" hint="mp3 / wav / m4a / mp4 / mov …" delay={80}>
            <FileDrop files={files} onChange={setFiles} accept="audio/*,video/*" label="拖入音频或视频" />
          </Card>
        </div>
        <div className="col">
          <Card title="3. 选项" delay={120}>
            <Field label="识别模型">
              <Seg value={model} options={MODELS} onChange={setModel} />
            </Field>
            <Field label="语言" hint="中文选“中文”会输出简体和标点">
              <select className="input" value={language} onChange={(e) => setLanguage(e.target.value)}>
                <option value="auto">自动识别</option>
                {catalog && Object.entries(catalog.langs).map(([k, v]) => <option key={k} value={k}>{v.name}</option>)}
              </select>
            </Field>
            <Field label="输出格式">
              <div className="chip-toggles">
                {FORMATS.map((f) => (
                  <button
                    key={f.id}
                    type="button"
                    className={`chip-toggle ${formats.includes(f.id) ? "on" : ""}`}
                    onClick={() => setFormats((cur) => (cur.includes(f.id) ? cur.filter((x) => x !== f.id) : [...cur, f.id]))}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
            </Field>
            <Toggle checked={vad} onChange={setVad} label="跳过静音片段" hint="减少空白处的乱码和重复" />
            <button className="btn primary full" disabled={!runtime || !ready.length || uploading || !formats.length || starting} onClick={start}>
              {uploading ? "等待文件上传…" : `开始转写 ${ready.length ? `${ready.length} 个文件` : ""}`} <span className="arrow">→</span>
            </button>
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
