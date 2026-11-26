import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, post, put } from "../api";
import { Ctx, type AppCtx, type Route } from "../ctx";
import type { Account, AppEvent, Catalog, GpuCardData, Job, RuntimesResponse } from "../types";
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

const NAV: { route: Route; label: string }[] = [
  { route: "gpus", label: "显卡" },
  { route: "models", label: "模型库" },
  { route: "create", label: "创作" },
  { route: "transcribe", label: "批量转写" },
  { route: "dub", label: "批量配音" },
  { route: "tts", label: "批量朗读" },
  { route: "tasks", label: "任务" },
];

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

  useEffect(() => {
    const onHash = () => setRoute(readRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const go = useCallback((r: Route) => {
    window.location.hash = `/${r}`;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  const toast = useCallback((kind: Toast["kind"], text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, text }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 12000 : 5000);
  }, []);

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

  usePoll(async () => {
    try {
      const r = await api<{ events: AppEvent[]; next: number }>(`/api/events?since=${eventCursor ?? 0}`);
      if (eventCursor !== null) {
        for (const ev of r.events) {
          const kind = ev.kind.endsWith("error") ? "error" : ev.kind === "created" || ev.kind === "task_done" ? "success" : "info";
          toast(kind, ev.message);
        }
      }
      setEventCursor(r.next);
    } catch {
      /* ignore */
    }
  }, 4000);

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

  async function setAutoRelease(minutes: number) {
    try {
      await put("/api/settings", { auto_release_minutes: minutes });
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
    }),
    [account, catalog, specs, specsError, data, selected, reloadRuntimes, toast, handle, go, startRuntime],
  );

  const initial = (account?.email ?? "G").slice(0, 1).toUpperCase();

  return (
    <Ctx.Provider value={ctx}>
      <div className="app">
                <header className="topbar">
          <a className="logo-link" onClick={() => go("gpus")}>
            <Logo withText />
          </a>
          <nav className="top-nav">
            {NAV.map((n) => (
              <button key={n.route} className={`nav-link ${route === n.route ? "on" : ""}`} onClick={() => go(n.route)}>
                {n.label}
              </button>
            ))}
          </nav>
          <div className="topbar-right">
            <label className="pill-select" title="只会释放 GPUDock 创建的运行时">
              <span>空闲自动释放</span>
              <select value={data?.auto_release_minutes ?? 30} onChange={(e) => setAutoRelease(Number(e.target.value))}>
                <option value={0}>关闭</option>
                <option value={15}>15 分钟</option>
                <option value={30}>30 分钟</option>
                <option value={60}>1 小时</option>
                <option value={120}>2 小时</option>
              </select>
            </label>
            <div className="account-wrap">
              <button className="account-pill" onClick={() => setMenu((m) => !m)}>
                <span className="avatar">{initial}</span>
                {account && <span className="tier">{tierLabel(account.tier)}</span>}
              </button>
              {menu && (
                <div className="menu" onMouseLeave={() => setMenu(false)}>
                  <div className="menu-mail">{account?.email ?? "已登录"}</div>
                  <button className="menu-item" onClick={logout}>退出登录</button>
                </div>
              )}
            </div>
          </div>
        </header>

        <nav className="mobile-nav">
          {NAV.map((n) => (
            <button key={n.route} className={`nav-link ${route === n.route ? "on" : ""}`} onClick={() => go(n.route)}>
              {n.label}
            </button>
          ))}
        </nav>

        <main className="content" key={route}>
          {route === "gpus" && <Gpus />}
          {route === "models" && <Models />}
          {route === "create" && <Create />}
          {route === "transcribe" && <Transcribe />}
          {route === "dub" && <Dub />}
          {route === "tts" && <Tts />}
          {route === "tasks" && <Tasks />}
          <footer className="foot subtle small">
            GPUDock 只在你的电脑上运行 · 通过 Google 官方 Colab API 管理运行时 · 与 Google 无关联
          </footer>
        </main>

        <div className="toasts">
          {toasts.map((t) => (
            <div key={t.id} className={`toast ${t.kind}`}>{t.text}</div>
          ))}
        </div>
      </div>
    </Ctx.Provider>
  );
}

export function friendly(error: string | null): string {
  if (!error) return "未知错误";
  if (error.includes("NO_RUNTIMES") || error.includes("resource_exhausted")) {
    return "这张卡暂时没有空闲资源（Colab 那边没货）。稍等一会儿再试，或者换一张卡。";
  }
  return error;
}
