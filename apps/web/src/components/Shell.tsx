import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, askToNotify, notify, post, put } from "../api";
import { Ctx, type AppCtx, type ConfirmOptions, type Route, type TaskRequest } from "../ctx";
import type { Account, AppEvent, Catalog, GpuCardData, Job, RuntimesResponse, TaskDetail, TaskSummary } from "../types";
import { tierLabel, usePoll } from "../util";
import { Logo } from "./Logo";
import Gpus from "../pages/Gpus";
import Models from "../pages/Models";
import Create from "../pages/Create";
import Transcribe from "../pages/Transcribe";
import Dub from "../pages/Dub";
import Tts from "../pages/Tts";
import Tasks from "../pages/Tasks";

interface Toast {
  id: number;
  kind: "info" | "error" | "success";
  text: string;
}

interface Dialog extends ConfirmOptions {
  resolve: (ok: boolean) => void;
}

const NAV: { route: Route; label: string; group: string; icon: string }[] = [
  { route: "gpus", label: "显卡", group: "", icon: "M5 7h14v10H5zM9 4v3M15 4v3M9 17v3M15 17v3M2 10h3M2 14h3M19 10h3M19 14h3" },
  { route: "tasks", label: "任务", group: "", icon: "M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01" },
  { route: "create", label: "创作", group: "批量工具", icon: "M4 5h16v14H4zM4 15l5-5 4 4 3-3 4 4" },
  { route: "transcribe", label: "批量转写", group: "批量工具", icon: "M4 6h16M4 10h10M4 14h16M4 18h8" },
  { route: "dub", label: "批量配音", group: "批量工具", icon: "M12 4a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V7a3 3 0 0 1 3-3zM6 11a6 6 0 0 0 12 0M12 17v3" },
  { route: "tts", label: "批量朗读", group: "批量工具", icon: "M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2" },
  { route: "models", label: "模型库", group: "资源", icon: "M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 12l8-4.5M12 12v9M12 12L4 7.5" },
];

function NavIcon({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

function readRoute(): Route {
  const r = window.location.hash.replace(/^#\/?/, "").split(/[?&]/)[0] as Route;
  return NAV.some((n) => n.route === r) ? r : "gpus";
}

export default function Shell({ onLoggedOut }: { onLoggedOut: () => void }) {
  const [route, setRoute] = useState<Route>(readRoute);
  const [account, setAccount] = useState<Account | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [specs, setSpecs] = useState<GpuCardData[] | null>(null);
  const [specsError, setSpecsError] = useState<string | null>(null);
  const [data, setData] = useState<RuntimesResponse | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [myJobs, setMyJobs] = useState<Record<string, Job>>({});
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [eventCursor, setEventCursor] = useState<number | null>(null);
  const [menu, setMenu] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [activeTasks, setActiveTasks] = useState<TaskSummary[]>([]);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // back/forward and edited URLs change only the hash: start each page at the top
    const onHash = () => {
      setRoute(readRoute());
      window.scrollTo({ top: 0 });
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (!menu) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenu(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setMenu(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setNavOpen(false);
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [navOpen]);

  const go = useCallback((r: Route) => {
    setNavOpen(false);
    window.location.hash = `/${r}`;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);
  const toast = useCallback(
    (kind: Toast["kind"], text: string) => {
      const id = Date.now() + Math.random();
      setToasts((t) => [...t.slice(-4), { id, kind, text }]);
      window.setTimeout(() => dismiss(id), kind === "error" ? 12000 : 5000);
    },
    [dismiss],
  );

  const confirm = useCallback((opts: ConfirmOptions) => new Promise<boolean>((resolve) => setDialog({ ...opts, resolve })), []);
  const closeDialog = useCallback(
    (ok: boolean) => {
      dialog?.resolve(ok);
      setDialog(null);
    },
    [dialog],
  );

  const handle = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.code === "login") {
        onLoggedOut();
        return;
      }
      toast("error", e instanceof Error ? e.message : String(e));
    },
    [onLoggedOut, toast],
  );

  useEffect(() => {
    api<Account>("/api/account").then(setAccount).catch(handle);
    api<Catalog>("/api/catalog").then(setCatalog).catch(handle);
    api<GpuCardData[]>("/api/specs").then(setSpecs).catch((e) => {
      setSpecsError(e instanceof Error ? e.message : String(e));
      handle(e);
    });
  }, [handle]);

  const reloadRuntimes = useCallback(async () => {
    try {
      const r = await api<RuntimesResponse>("/api/runtimes");
      setData(r);
      setSelected((cur) => {
        if (cur && r.runtimes.some((x) => x.id === cur)) return cur;
        return r.runtimes.find((x) => x.owned)?.id ?? r.runtimes[0]?.id ?? null;
      });
    } catch (e) {
      handle(e);
    }
  }, [handle]);
  usePoll(reloadRuntimes, 5000);

  const loadActive = useCallback(async () => {
    try {
      setActiveTasks(await api<TaskSummary[]>("/api/tasks?active=true"));
    } catch {
      /* the badge is best-effort */
    }
  }, []);
  usePoll(loadActive, 4000);

  useEffect(() => {
    document.title = activeTasks.length ? `(${activeTasks.length}) GPUDock` : "GPUDock";
  }, [activeTasks.length]);

  const running = Object.values(myJobs).filter((j) => j.status === "running");
  usePoll(
    async () => {
      for (const j of running) {
        try {
          const fresh = await api<Job>(`/api/jobs/${j.id}`);
          setMyJobs((m) => ({ ...m, [j.id]: fresh }));
          if (fresh.status === "done" && fresh.result) {
            setSelected(fresh.result.id);
            reloadRuntimes();
          } else if (fresh.status === "error") {
            toast("error", `启动 ${fresh.label} 失败\n${friendly(fresh.error)}`);
            reloadRuntimes();
          } else if (fresh.status === "cancelled") {
            toast("info", `已取消 ${fresh.label} 的排队`);
            reloadRuntimes();
          }
        } catch (e) {
          handle(e);
        }
      }
    },
    1500,
    running.length > 0,
  );

  // Events keep coming in the background (slowly) so a finished task can raise a system notification.
  usePoll(
    async () => {
      try {
        const r = await api<{ events: AppEvent[]; next: number }>(`/api/events?since=${eventCursor ?? 0}`);
        if (eventCursor !== null) {
          for (const ev of r.events) {
            const kind = ev.kind.endsWith("error") ? "error" : ev.kind === "created" || ev.kind === "task_done" ? "success" : "info";
            toast(kind, ev.message);
            if (ev.kind.startsWith("task_") || ev.kind === "auto_released" || ev.kind === "created") notify(ev.message);
          }
          if (r.events.some((ev) => ev.kind.startsWith("task_"))) loadActive();
          if (r.events.some((ev) => ["created", "released", "auto_released", "reclaimed"].includes(ev.kind))) reloadRuntimes();
        }
        setEventCursor(r.next);
      } catch {
        /* ignore */
      }
    },
    4000,
    true,
    15000,
  );

  const startRuntime = useCallback(
    async (accelerator: string, shape: string, label: string) => {
      try {
        const job = await post<Job>("/api/runtimes", { accelerator, shape });
        setMyJobs((m) => ({ ...m, [job.id]: job }));
        toast("info", `正在向 Colab 申请 ${label}。如果暂时没货，会自动排队重试（最多 20 分钟）。`);
        reloadRuntimes();
      } catch (e) {
        handle(e);
      }
    },
    [handle, reloadRuntimes, toast],
  );

  const submitTask = useCallback(
    async ({ kind, target, accelerator, params, uploads = [] }: TaskRequest) => {
      if (!target.runtime) return null;
      try {
        const t = await post<TaskDetail>("/api/tasks", {
          kind,
          runtime: target.runtime,
          accelerator: target.runtime === "auto" ? accelerator : undefined,
          release_after: target.release,
          params,
          uploads,
        });
        askToNotify();
        loadActive();
        if (target.runtime === "auto") window.setTimeout(reloadRuntimes, 1500);
        return t;
      } catch (e) {
        handle(e);
        return null;
      }
    },
    [handle, loadActive, reloadRuntimes],
  );

  const retryTask = useCallback(
    async (id: string, overrides: Record<string, unknown> = {}) => {
      try {
        const t = await post<TaskDetail>(`/api/tasks/${id}/retry`, { overrides });
        askToNotify();
        loadActive();
        toast("info", "已重新提交");
        return t;
      } catch (e) {
        handle(e);
        return null;
      }
    },
    [handle, loadActive, toast],
  );

  async function saveSettings(body: Record<string, unknown>) {
    try {
      await put("/api/settings", body);
      reloadRuntimes();
    } catch (e) {
      handle(e);
    }
  }

  async function logout() {
    await post("/api/auth/logout").catch(() => {});
    onLoggedOut();
  }

  const ctx: AppCtx = useMemo(
    () => ({
      account,
      catalog,
      specs,
      specsError,
      data,
      runtimes: data?.runtimes ?? [],
      selected,
      setSelected,
      reloadRuntimes,
      toast,
      handle,
      go,
      startRuntime,
      confirm,
      activeTasks,
      submitTask,
      retryTask,
    }),
    [account, catalog, specs, specsError, data, selected, reloadRuntimes, toast, handle, go, startRuntime, confirm, activeTasks, submitTask, retryTask],
  );

  const initial = (account?.email ?? "G").slice(0, 1).toUpperCase();
  const busy = activeTasks.filter((t) => t.status !== "waiting_review").length;
  const review = activeTasks.length - busy;

  return (
    <Ctx.Provider value={ctx}>
      <div className={`app ${navOpen ? "nav-open" : ""}`}>
        <aside className="side">
          <a className="logo-link" href="#/gpus" onClick={(e) => { e.preventDefault(); go("gpus"); }}>
            <Logo withText />
          </a>
          {["", "批量工具", "资源"].map((group) => (
            <nav key={group || "main"} className="side-group" aria-label={group || "主导航"}>
              {group && <span className="side-label">{group}</span>}
              {NAV.filter((n) => n.group === group).map((n) => (
                <button key={n.route} className={`nav-link ${route === n.route ? "on" : ""}`} aria-current={route === n.route ? "page" : undefined} onClick={() => go(n.route)}>
                  <NavIcon d={n.icon} />
                  {n.label}
                  {n.route === "tasks" && activeTasks.length > 0 && <span className="nav-count" aria-label={`${activeTasks.length} 个进行中`}>{activeTasks.length}</span>}
                </button>
              ))}
            </nav>
          ))}
          <p className="side-foot foot">
            只在你的电脑上运行，通过 Google 官方 Colab API 管理运行时。与 Google 无关联。
          </p>
        </aside>
        {navOpen && <div className="nav-scrim" onClick={() => setNavOpen(false)} />}

        <div className="main">
        <header className="topbar">
          <button className="icon-btn menu-btn" aria-label="打开导航" aria-expanded={navOpen} onClick={() => setNavOpen(true)}>
            <NavIcon d="M4 7h16M4 12h16M4 17h16" />
          </button>
          <div className="topbar-right">
            {activeTasks.length > 0 && (
              <button className={`task-badge ${review ? "attention" : ""}`} onClick={() => go("tasks")} title={activeTasks.map((t) => `${t.title} · ${t.stage}`).join("\n")}>
                <span className="pulse-dot busy" />
                {review ? `${review} 个任务等你校对` : `${busy} 个任务进行中`}
                {busy === 1 && !review && <span className="mono subtle">{Math.round(activeTasks[0].progress * 100)}%</span>}
              </button>
            )}
            <label className="pill-select" title="只会释放 GPUDock 创建的运行时">
              <span>空闲自动释放</span>
              <select value={data?.auto_release_minutes ?? 30} onChange={(e) => saveSettings({ auto_release_minutes: Number(e.target.value) })}>
                <option value={0}>关闭</option>
                <option value={15}>15 分钟</option>
                <option value={30}>30 分钟</option>
                <option value={60}>1 小时</option>
                <option value={120}>2 小时</option>
              </select>
            </label>
            <div className="account-wrap" ref={menuRef}>
              <button className="account-pill" onClick={() => setMenu((m) => !m)} aria-expanded={menu}>
                <span className="avatar">{initial}</span>
                {account && <span className="tier">{tierLabel(account.tier)}</span>}
              </button>
              {menu && (
                <div className="menu" role="menu">
                  <div className="menu-mail">{account?.email ?? "已登录"}</div>
                  <label className="menu-check">
                    <input
                      type="checkbox"
                      checked={data?.release_on_exit ?? true}
                      onChange={(e) => saveSettings({ release_on_exit: e.target.checked })}
                    />
                    <span>
                      退出 GPUDock 时释放显卡
                      <span className="field-hint block">关掉 GPUDock 后空闲自动释放也会停止，建议开着</span>
                    </span>
                  </label>
                  <button className="menu-item" onClick={logout}>退出登录</button>
                </div>
              )}
            </div>
          </div>
        </header>

        <main className="content" key={route}>
          {route === "gpus" && <Gpus />}
          {route === "models" && <Models />}
          {route === "create" && <Create />}
          {route === "transcribe" && <Transcribe />}
          {route === "dub" && <Dub />}
          {route === "tts" && <Tts />}
          {route === "tasks" && <Tasks />}
        </main>
        </div>

        <div className="toasts" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} className={`toast ${t.kind}`}>
              <span>{t.text}</span>
              <button className="toast-x" aria-label="关闭" onClick={() => dismiss(t.id)}>×</button>
            </div>
          ))}
        </div>

        {dialog && <ConfirmDialog dialog={dialog} onClose={closeDialog} />}
      </div>
    </Ctx.Provider>
  );
}

function ConfirmDialog({ dialog, onClose }: { dialog: Dialog; onClose: (ok: boolean) => void }) {
  const okRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    okRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose(false);
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose(false)}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="dlg-title">
        <h2 id="dlg-title">{dialog.title}</h2>
        {dialog.body && <p className="subtle">{dialog.body}</p>}
        <div className="modal-actions">
          <button className="btn ghost" onClick={() => onClose(false)}>取消</button>
          <button ref={okRef} className={`btn ${dialog.danger ? "danger-solid" : "primary"}`} onClick={() => onClose(true)}>
            {dialog.ok ?? "确定"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function friendly(error: string | null): string {
  if (!error) return "未知错误";
  if (error.includes("NO_RUNTIMES") || error.includes("resource_exhausted")) {
    return "这张卡暂时没有空闲资源（Colab 那边没货）。稍等一会儿再试，或者换一张卡。";
  }
  return error;
}
