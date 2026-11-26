import { useEffect, useRef, useState } from "react";
import { api, fileUrl, post, saveFile } from "../api";
import { useApp } from "../ctx";
import type { ReviewSegment, TaskDetail, TaskOutput } from "../types";
import { duration, usePoll } from "../util";
import { fmtSize } from "./common";

const STATUS: Record<string, { label: string; cls: string }> = {
  queued: { label: "排队中", cls: "starting" },
  running: { label: "进行中", cls: "busy" },
  waiting_review: { label: "等你校对", cls: "starting" },
  done: { label: "完成", cls: "idle" },
  error: { label: "失败", cls: "err" },
  cancelled: { label: "已取消", cls: "muted" },
  interrupted: { label: "已中断", cls: "muted" },
};

const ITEM_STATUS: Record<string, string> = {
  waiting: "等待",
  uploading: "上传中",
  uploaded: "已上传",
  running: "处理中",
  speaking: "配音中",
  rendering: "合成中",
  done: "完成",
};

export default function TaskPanel({ taskId, compact = false, onDone }: { taskId: string; compact?: boolean; onDone?: (t: TaskDetail) => void }) {
  const { handle, toast } = useApp();
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [showLogs, setShowLogs] = useState(false);
  const logNext = useRef(0);
  const notified = useRef(false);
  const active = !task || ["queued", "running", "waiting_review"].includes(task.status);

  useEffect(() => {
    logNext.current = 0;
    notified.current = false;
    setLogs([]);
    setTask(null);
  }, [taskId]);

  usePoll(
    async () => {
      try {
        const t = await api<TaskDetail>(`/api/tasks/${taskId}?log_since=${logNext.current}`);
        logNext.current = t.log_next;
        if (t.logs.length) setLogs((l) => [...l, ...t.logs].slice(-800));
        setTask(t);
        if (!notified.current && ["done", "error", "cancelled"].includes(t.status)) {
          notified.current = true;
          onDone?.(t);
        }
      } catch (e) {
        handle(e);
      }
    },
    task?.status === "waiting_review" ? 3000 : 900,
    active,
  );

  if (!task) return <div className="task-panel"><div className="spinner" /></div>;
  const st = STATUS[task.status] ?? STATUS.running;
  const elapsed = (task.finished ?? Date.now() / 1000) - (task.started ?? task.created);

  async function cancel() {
    if (!window.confirm("取消这个任务？已完成的部分不会保留。")) return;
    try {
      await post(`/api/tasks/${taskId}/cancel`);
      toast("info", "正在取消…");
    } catch (e) {
      handle(e);
    }
  }

  return (
    <div className={`task-panel ${compact ? "compact" : ""}`}>
      <div className="task-head">
        <div className="task-title">
          <span className={`status-pill ${st.cls}`}>{st.label}</span>
          <b>{task.title}</b>
        </div>
        <div className="row-gap">
          <span className="subtle small">{duration(elapsed)}</span>
          {active && task.status !== "waiting_review" && (
            <button className="btn ghost danger" onClick={cancel}>取消</button>
          )}
          {task.status === "waiting_review" && <button className="btn ghost danger" onClick={cancel}>放弃</button>}
        </div>
      </div>

      <div className="task-progress">
        <div className={`bar big ${active ? "live" : ""}`}>
          <div className={`bar-fill ${task.status === "error" ? "hot" : ""}`} style={{ width: `${Math.round(task.progress * 100)}%` }} />
        </div>
        <div className="task-stage">
          <span>{task.stage}</span>
          <span className="subtle">{Math.round(task.progress * 100)}%</span>
        </div>
      </div>

      {task.error && <div className="notice error">{task.error}</div>}

      {task.status === "waiting_review" && task.review && <ReviewEditor taskId={taskId} review={task.review} />}

      {!compact && task.items.length > 0 && (
        <ul className="item-list">
          {task.items.map((it) => (
            <li key={it.name}>
              <span className={`pulse-dot ${it.status === "done" ? "ok" : it.status === "waiting" ? "" : "busy"}`} />
              <span className="file-name">{it.name}</span>
              <span className="subtle small">{it.detail}</span>
              <span className="subtle small">{ITEM_STATUS[it.status] ?? it.status}</span>
            </li>
          ))}
        </ul>
      )}

      {task.result?.reply && <div className="reply">{task.result.reply}</div>}

      {task.outputs.length > 0 && <Outputs taskId={taskId} outputs={task.outputs} />}

      {!compact && (
        <div className="log-wrap">
          <button className="link" onClick={() => setShowLogs((s) => !s)}>
            {showLogs ? "收起日志" : `查看日志（${logs.length} 行）`}
          </button>
          {showLogs && <pre className="output logs">{logs.join("\n") || "暂无日志"}</pre>}
        </div>
      )}
    </div>
  );
}

function Outputs({ taskId, outputs }: { taskId: string; outputs: TaskOutput[] }) {
  const { handle } = useApp();
  const [preview, setPreview] = useState<{ name: string; url: string; kind: string } | null>(null);
  // open the main result right away when it is small media (a TTS clip, a generated image or short video)
  const first = outputs[0];
  const autoOpen =
    !!first && ((outputs.length === 1 && first.kind === "audio") || first.kind === "image" || (first.kind === "video" && first.size < 60 * 1024 * 1024 && outputs.length <= 2));

  async function open(o: TaskOutput) {
    try {
      const url = await fileUrl(taskId, o.name);
      setPreview((p) => {
        if (p) URL.revokeObjectURL(p.url);
        return { name: o.name, url, kind: o.kind };
      });
    } catch (e) {
      handle(e);
    }
  }

  useEffect(() => {
    if (autoOpen) open(first);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoOpen, taskId]);

  async function reveal() {
    try {
      await post(`/api/tasks/${taskId}/reveal`);
    } catch (e) {
      handle(e);
    }
  }

  return (
    <div className="outputs">
      <div className="outputs-head">
        <b>产出文件</b>
        <button className="btn ghost" onClick={reveal}>在文件夹中显示</button>
      </div>
      {preview && (
        <div className="preview">
          {preview.kind === "video" && <video src={preview.url} controls autoPlay={false} loop />}
          {preview.kind === "audio" && <audio src={preview.url} controls />}
          {preview.kind === "image" && <img src={preview.url} alt={preview.name} />}
          {!["video", "audio", "image"].includes(preview.kind) && <TextPreview url={preview.url} />}
        </div>
      )}
      <ul className="out-list">
        {outputs.map((o) => (
          <li key={o.name}>
            <span className={`kind-dot k-${o.kind}`} />
            <span className="file-name">{o.name}</span>
            <span className="subtle small">{fmtSize(o.size)}</span>
            <button className="link" onClick={() => open(o)}>{o.kind === "video" || o.kind === "audio" ? "播放" : o.kind === "image" ? "查看" : "预览"}</button>
            <button className="link" onClick={() => saveFile(taskId, o.name).catch(handle)}>下载</button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function TextPreview({ url }: { url: string }) {
  const [text, setText] = useState("");
  useEffect(() => {
    fetch(url).then((r) => r.text()).then((t) => setText(t.slice(0, 20000)));
  }, [url]);
  return <pre className="output">{text}</pre>;
}

function ReviewEditor({ taskId, review }: { taskId: string; review: NonNullable<TaskDetail["review"]> }) {
  const { handle, toast } = useApp();
  const [edits, setEdits] = useState<Record<string, Record<number, string>>>({});
  const [sending, setSending] = useState(false);

  function value(name: string, seg: ReviewSegment) {
    return edits[name]?.[seg.id] ?? seg.text;
  }

  async function submit() {
    setSending(true);
    try {
      const payload: Record<string, { id: number; text: string }[]> = {};
      for (const [name, map] of Object.entries(edits)) payload[name] = Object.entries(map).map(([id, text]) => ({ id: Number(id), text }));
      await post(`/api/tasks/${taskId}/review`, { edits: payload });
      toast("success", "已确认译文，开始配音。");
    } catch (e) {
      handle(e);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="review">
      <div className="review-head">
        <div>
          <b>校对译文</b>
          <p className="subtle small">直接修改右侧译文，改完点“确认并开始配音”。译文越接近原句时长，配音越自然。</p>
        </div>
        <button className="btn primary" onClick={submit} disabled={sending}>
          确认并开始配音 <span className="arrow">→</span>
        </button>
      </div>
      {review.items.map((item) => (
        <div key={item.name} className="review-file">
          <div className="subtle small review-file-name">{item.name} · {item.segments.length} 句</div>
          <div className="review-rows">
            {item.segments.map((s) => (
              <div key={s.id} className="review-row">
                <span className="ts">{fmtTime(s.start)}</span>
                <span className="src">{s.source}</span>
                <textarea
                  value={value(item.name, s)}
                  rows={Math.max(1, Math.ceil(value(item.name, s).length / 28))}
                  onChange={(e) => setEdits((m) => ({ ...m, [item.name]: { ...(m[item.name] ?? {}), [s.id]: e.target.value } }))}
                />
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function fmtTime(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}
