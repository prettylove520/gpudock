import { useState } from "react";
import { api } from "../api";
import { useApp } from "../ctx";
import type { TaskSummary } from "../types";
import { usePoll } from "../util";
import { Card, PageHead } from "../components/common";
import TaskPanel from "../components/TaskPanel";
import { ChipArt } from "../components/Logo";

const STATUS: Record<string, string> = {
  queued: "排队中",
  running: "进行中",
  waiting_review: "等你校对",
  done: "完成",
  error: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

export default function Tasks() {
  const { handle } = useApp();
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  usePoll(async () => {
    try {
      setTasks(await api<TaskSummary[]>("/api/tasks"));
    } catch (e) {
      handle(e);
    }
  }, 3000);

  return (
    <>
      <PageHead eyebrow="07 / 任务 · 所有批量任务" title="任务记录" accent="" lede="产出文件保存在你电脑上的 ~/GPUDock/outputs 文件夹，释放运行时也不会丢。" />
      <Card delay={40}>
        {!tasks ? (
          <div className="spinner" />
        ) : tasks.length === 0 ? (
          <div className="empty-state">
            <ChipArt size={36} />
            <div>
              <b>还没有任务</b>
              <p className="subtle small">去“批量转写”“批量配音”或“批量朗读”开始第一个吧。</p>
            </div>
          </div>
        ) : (
          <ul className="task-list">
            {tasks.map((t) => (
              <li key={t.id} className={open === t.id ? "open" : ""}>
                <button className="task-row" onClick={() => setOpen(open === t.id ? null : t.id)}>
                  <span className={`status-pill ${t.status === "done" ? "idle" : t.status === "error" ? "err" : ["running", "queued"].includes(t.status) ? "busy" : "starting"}`}>
                    {STATUS[t.status] ?? t.status}
                  </span>
                  <span className="task-name">{t.title}</span>
                  <span className="subtle small">{new Date(t.created * 1000).toLocaleString()}</span>
                  <span className="mini-bar"><span style={{ width: `${Math.round(t.progress * 100)}%` }} /></span>
                </button>
                {open === t.id && (
                  <div className="task-open">
                    <TaskPanel taskId={t.id} />
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}
