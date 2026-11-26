import { useMemo, useState } from "react";
import { api } from "../api";
import { useApp } from "../ctx";
import type { TaskSummary } from "../types";
import { ago, cu, duration, usePoll } from "../util";
import { Card, PageHead, Seg } from "../components/common";
import TaskPanel from "../components/TaskPanel";
import { ChipArt } from "../components/Logo";

const STATUS: Record<string, { label: string; cls: string }> = {
  queued: { label: "排队中", cls: "busy" },
  running: { label: "进行中", cls: "busy" },
  waiting_review: { label: "等你校对", cls: "starting" },
  done: { label: "完成", cls: "idle" },
  error: { label: "失败", cls: "err" },
  cancelled: { label: "已取消", cls: "muted" },
  interrupted: { label: "已中断", cls: "muted" },
};

type Filter = "all" | "active" | "failed" | "generate" | "dub" | "transcribe" | "tts";
const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "active", label: "进行中" },
  { value: "failed", label: "失败" },
  { value: "generate", label: "创作" },
  { value: "dub", label: "配音" },
  { value: "transcribe", label: "转写" },
  { value: "tts", label: "朗读" },
];

function matches(t: TaskSummary, f: Filter): boolean {
  if (f === "all") return true;
  if (f === "active") return ["queued", "running", "waiting_review"].includes(t.status);
  if (f === "failed") return ["error", "interrupted"].includes(t.status);
  if (f === "tts") return t.kind === "tts" || t.kind === "tts_preview";
  return t.kind === f;
}

export default function Tasks() {
  const { handle } = useApp();
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");

  const load = async () => {
    try {
      setTasks(await api<TaskSummary[]>("/api/tasks"));
    } catch (e) {
      handle(e);
    }
  };
  usePoll(load, 3000);

  const shown = useMemo(() => (tasks ?? []).filter((t) => matches(t, filter)), [tasks, filter]);
  const spent = useMemo(() => (tasks ?? []).reduce((s, t) => s + (t.est_cu ?? 0), 0), [tasks]);

  return (
    <>
      <PageHead eyebrow="07 / 任务 · 所有批量任务" title="任务记录" accent="" lede="产出文件保存在你电脑上的 ~/GPUDock/outputs 文件夹，释放运行时也不会丢。失败或中断的任务可以直接重新运行。" />
      <Card delay={40}>
        <div className="row-between">
          <Seg value={filter} onChange={setFilter} options={FILTERS} />
          {spent > 0 && <span className="subtle small">这些任务合计约 {cu(spent)}（按运行时长和显卡单价估算）</span>}
        </div>
        {!tasks ? (
          <div className="spinner" />
        ) : shown.length === 0 ? (
          <div className="empty-state">
            <ChipArt size={36} />
            <div>
              <b>{tasks.length ? "没有符合条件的任务" : "还没有任务"}</b>
              <p className="subtle small">去“创作”“批量转写”“批量配音”或“批量朗读”开始第一个吧。</p>
            </div>
          </div>
        ) : (
          <ul className="task-list">
            {shown.map((t) => {
              const st = STATUS[t.status] ?? STATUS.running;
              const active = ["queued", "running", "waiting_review"].includes(t.status);
              const took = t.started ? (t.finished ?? Date.now() / 1000) - t.started : null;
              return (
                <li key={t.id} className={open === t.id ? "open" : ""}>
                  <button className="task-row" onClick={() => setOpen(open === t.id ? null : t.id)} aria-expanded={open === t.id}>
                    <span className={`status-pill ${st.cls}`}>{st.label}</span>
                    <span className="task-name">
                      <span className="kind-label">{t.kind_label}</span>
                      {t.title.replace(`${t.kind_label} · `, "")}
                      {active && <span className="subtle small"> · {t.stage}</span>}
                    </span>
                    <span className="subtle task-meta">
                      {[t.accelerator, took != null && duration(took), t.est_cu != null && cu(t.est_cu)].filter(Boolean).join(" · ")}
                    </span>
                    <span className="subtle task-when" title={new Date(t.created * 1000).toLocaleString()}>{ago(t.created)}</span>
                    <span className="mini-bar"><span style={{ width: `${Math.round(t.progress * 100)}%` }} /></span>
                  </button>
                  {open === t.id && (
                    <div className="task-open">
                      <TaskPanel
                        taskId={t.id}
                        showTitle={false}
                        onRetried={(n) => {
                          setOpen(n.id);
                          setFilter("all");
                          load();
                        }}
                      />
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </>
  );
}
