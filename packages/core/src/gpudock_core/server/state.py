"""Everything the GUI server keeps in memory: login, runtimes, jobs, executions.

Only runtimes GPUDock created ("owned") can be released from the GUI or by the
idle auto-release. Runtimes the user opened in the Colab web UI are listed but
never touched.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..agent import AGENT_FILE
from ..agent.gpudock_agent import EVT
from ..auth import (
    Authenticator,
    AuthSettings,
    TokenStore,
    login_oauth,
    resolve_client_config,
)
from ..colab_api import ColabApi, Runtime, SpecKey
from ..errors import AuthError, ColabApiError
from ..files import upload
from ..jupyter import ConnectionClosed, ExecResult, Kernel, RuntimeConnection
from . import catalog

STATS_CODE = r"""
import json as _j, shutil as _sh, subprocess as _sp, time as _t
def _gpudock_stats():
    s = {"t": _t.time()}
    try:
        out = _sp.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                       "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout.strip()
        if out:
            n, u, mu, mt, tp = [x.strip() for x in out.splitlines()[0].split(",")]
            s["gpu"] = {"name": n, "util": float(u), "mem_used_mb": float(mu), "mem_total_mb": float(mt), "temp_c": float(tp)}
    except Exception as e:
        s["gpu_error"] = type(e).__name__
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
        s["ram"] = {"total_gb": round(info["MemTotal"] / 1048576, 1),
                    "used_gb": round((info["MemTotal"] - info["MemAvailable"]) / 1048576, 1)}
    except Exception:
        pass
    for root in ("/content", "/"):
        try:
            d = _sh.disk_usage(root)
            s["disk"] = {"total_gb": round(d.total / 1e9, 1), "used_gb": round(d.used / 1e9, 1)}
            break
        except Exception:
            pass
    return s
_gpudock_stats_result = _gpudock_stats()
"""


def now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------
class OwnedRuntimes:
    """Runtimes GPUDock created, persisted so a restart still knows them."""

    def __init__(self, path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return self._load()

    def get(self, name: str) -> dict[str, Any] | None:
        return self.all().get(name)

    def add(self, name: str, **meta: Any) -> None:
        with self._lock:
            data = self._load()
            data[name] = {"created_at": now(), "last_activity": now(), **meta}
            self._save(data)

    def update(self, name: str, **meta: Any) -> None:
        with self._lock:
            data = self._load()
            if name in data:
                data[name].update(meta)
                self._save(data)

    def remove(self, name: str) -> None:
        with self._lock:
            data = self._load()
            if data.pop(name, None) is not None:
                self._save(data)


@dataclass
class Settings:
    auto_release_minutes: int = 30
    # quitting GPUDock stops the idle auto-release too, so by default release on the way out
    release_on_exit: bool = True

    @classmethod
    def load(cls, path) -> Settings:
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return cls(
                    auto_release_minutes=int(data.get("auto_release_minutes", 30)),
                    release_on_exit=bool(data.get("release_on_exit", True)),
                )
            except (ValueError, TypeError):
                pass
        return cls()

    def to_dict(self) -> dict[str, Any]:
        return {"auto_release_minutes": self.auto_release_minutes, "release_on_exit": self.release_on_exit}

    def save(self, path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2))


class UsageLedger:
    """Closed runtime sessions (start, end, CU/hour), so spending survives a release."""

    KEEP_DAYS = 120

    def __init__(self, path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except ValueError:
            return []

    def add(self, name: str, meta: dict[str, Any], end: float) -> None:
        start = meta.get("created_at")
        if not start or meta.get("pending"):
            return
        info = catalog.info_for(meta.get("accelerator", ""), meta.get("shape", "SHAPE_STANDARD"))
        with self._lock:
            rows = [r for r in self._load() if r.get("end", 0) > now() - self.KEEP_DAYS * 86400]
            rows.append({"name": name, "label": meta.get("label", name), "accelerator": meta.get("accelerator"),
                         "start": start, "end": max(end, start), "cu_per_hour": info.get("cu_per_hour")})
            self.path.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")

    def summary(self, running: list[dict[str, Any]]) -> dict[str, Any]:
        """CU spent today and this month (local time), closed sessions plus the running ones."""
        t = time.localtime()
        day0 = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
        month0 = time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))
        rows = [*self._load(), *running]
        unknown = False

        def spent(since: float) -> float:
            nonlocal unknown
            total = 0.0
            for r in rows:
                start, end = max(r["start"], since), r["end"]
                if end <= start:
                    continue
                if r.get("cu_per_hour") is None:
                    unknown = True
                    continue
                total += (end - start) / 3600 * r["cu_per_hour"]
            return round(total, 2)

        today, month = spent(day0), spent(month0)
        return {"today_cu": today, "month_cu": month, "has_unpriced": unknown}


# ---------------------------------------------------------------------------
# jobs and executions
# ---------------------------------------------------------------------------
@dataclass
class Job:
    id: str
    kind: str
    label: str
    accelerator: str = ""
    shape: str = ""
    status: str = "running"  # running | done | error
    message: str = ""
    error: str | None = None
    result: dict[str, Any] | None = None
    started: float = field(default_factory=now)
    finished: float | None = None
    attempts: int = 0
    cancelled: bool = False
    auto: bool = False  # started by a task that asked for "any runtime of this kind"

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "auto": self.auto,
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "accelerator": self.accelerator,
            "shape": self.shape,
            "status": self.status,
            "message": self.message,
            "error": self.error,
            "result": self.result,
            "started": self.started,
            "finished": self.finished,
        }


@dataclass
class ExecRecord:
    id: str
    runtime: str
    code: str
    status: str = "running"  # running | ok | error | aborted
    chunks: list[dict[str, Any]] = field(default_factory=list)
    offset: int = 0  # chunks dropped from the front (long runs keep only the tail)
    error: dict[str, Any] | None = None
    started: float = field(default_factory=now)
    finished: float | None = None

    MAX_CHUNKS = 4000

    def add(self, kind: str, text: str) -> None:
        self.chunks.append({"i": self.offset + len(self.chunks), "kind": kind, "text": text})
        if len(self.chunks) > self.MAX_CHUNKS:
            drop = len(self.chunks) - self.MAX_CHUNKS
            del self.chunks[:drop]
            self.offset += drop

    def view(self, since: int = 0) -> dict[str, Any]:
        return {
            "id": self.id,
            "runtime": self.runtime,
            "status": self.status,
            "chunks": self.chunks[max(0, since - self.offset):],
            "next": self.offset + len(self.chunks),
            "truncated": since < self.offset,
            "error": self.error,
            "started": self.started,
            "finished": self.finished,
        }


# ---------------------------------------------------------------------------
# one runtime's Jupyter connections
# ---------------------------------------------------------------------------
class AgentError(RuntimeError):
    """A step of the runtime agent failed."""


class AgentBusy(RuntimeError):
    """The runtime's worker kernel is busy with another task."""


class EventParser:
    """Reassembles kernel stream chunks into lines and decodes agent events."""

    def __init__(self, on_event: Callable[[dict[str, Any]], None]) -> None:
        self.on_event = on_event
        self.buf = ""
        self.result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.tail: list[str] = []

    def feed(self, kind: str, text: str) -> None:
        if kind not in ("stdout", "stderr"):
            return
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self._line(line)

    def flush(self) -> None:
        if self.buf:
            self._line(self.buf)
            self.buf = ""

    def _line(self, line: str) -> None:
        line = line.rstrip("\r")
        if line.startswith(EVT):
            try:
                evt = json.loads(line[len(EVT) :])
            except ValueError:
                return
            if evt.get("type") == "result":
                self.result = {k: v for k, v in evt.items() if k != "type"}
            elif evt.get("type") == "error":
                self.last_error = evt.get("message")
            self.on_event(evt)
        elif line.strip():
            self.tail = (self.tail + [line])[-30:]
            self.on_event({"type": "raw", "message": line})


class RuntimeHandle:
    CONSOLE = "gpudock-console"
    MONITOR = "gpudock-monitor"
    WORKER = "gpudock-worker"
    AGENT_DIR = "content/gpudock"  # relative to the runtime's Jupyter root

    def __init__(self, name: str, api: ColabApi, *, fs_root: str = "/") -> None:
        self.name = name
        self.api = api
        self.fs_root = fs_root
        self.agent_root = posixpath.join(fs_root, self.AGENT_DIR)
        self.conn: RuntimeConnection | None = None
        self.expire_time = None
        self.kernels: dict[str, Kernel] = {}
        self.exec_lock = threading.Lock()
        self.monitor_lock = threading.Lock()
        self.setup_lock = threading.Lock()
        self.worker_lock = threading.Lock()
        self.busy_exec: str | None = None
        self.busy_task: str | None = None
        self.agent_uploaded: str | None = None
        self.agent_status: dict[str, Any] | None = None
        self.last_activity = now()
        self.stats_cache: dict[str, Any] | None = None
        self.stats_at = 0.0
        self.stats_failed_at = 0.0
        self.orphan_interrupted = False

    # ---- runtime agent ----------------------------------------------------
    def remote(self, *parts: str) -> str:
        """A path relative to the Jupyter root (for the contents/files API)."""
        return posixpath.join(self.AGENT_DIR, *parts)

    def absolute(self, remote_path: str) -> str:
        """The same path as the agent sees it on the runtime's filesystem."""
        return posixpath.join(self.fs_root, remote_path)

    def _ensure_agent(self) -> None:
        digest = hashlib.sha256(AGENT_FILE.read_bytes()).hexdigest()[:16]
        if self.agent_uploaded != digest:
            upload(self._connection(), AGENT_FILE, self.remote("gpudock_agent.py"))
            self.agent_uploaded = digest

    def run_agent(
        self, cmd: str, args: dict[str, Any], on_event: Callable[[dict[str, Any]], None], *, blocking: bool = True
    ) -> dict[str, Any]:
        """Run one agent command in the worker kernel; returns its result event.

        With ``blocking=False`` raises ``AgentBusy`` instead of waiting for a running task.
        """
        if not self.worker_lock.acquire(blocking=blocking):
            raise AgentBusy("运行时正在执行任务")
        try:
            self._ensure_agent()
            root = self.agent_root
            code = (
                "import os, sys, json, importlib\n"
                f"os.environ['GPUDOCK_ROOT'] = {root!r}\n"
                f"sys.path.insert(0, {root!r}) if {root!r} not in sys.path else None\n"
                "import gpudock_agent as _gd\n"
                "_gd = importlib.reload(_gd)\n"
                f"_gd.run_cli({cmd!r}, json.loads({json.dumps(args, ensure_ascii=False)!r}))\n"
            )
            parser = EventParser(on_event)
            kernel = self.kernel(self.WORKER)
            msg_id = kernel.send_execute(code)
            res = ExecResult(msg_id=msg_id, started=time.monotonic())
            reconnects = 0
            while True:
                try:
                    res = kernel.wait(msg_id, on_output=parser.feed, idle_timeout=None, result=res)
                    break
                except ConnectionClosed:
                    if reconnects >= 8:
                        raise
                    reconnects += 1
                    time.sleep(min(2**reconnects, 30))
                    kernel.reconnect(refresh_token=True)
            parser.flush()
            if not res.ok:
                if res.error and res.error.get("ename") == "KeyboardInterrupt":
                    raise AgentError("已取消")
                detail = parser.last_error or res.error_text() or "\n".join(parser.tail[-5:])
                raise AgentError(detail)
            return parser.result or {}
        finally:
            self.worker_lock.release()

    def interrupt_worker(self) -> None:
        kernel = self.kernels.get(self.WORKER)
        if kernel is not None:
            self._connection().interrupt(kernel.kernel_id)

    def _connection(self) -> RuntimeConnection:
        if self.conn is None:
            runtime = self.api.get_runtime(self.name)
            if runtime.connection is None:
                raise ColabApiError(None, None, "UNAVAILABLE", "运行时还没有就绪（没有连接信息），稍后再试。")
            self.expire_time = runtime.connection.expire_time

            def refresh() -> tuple[str, str]:
                fresh = self.api.get_runtime(self.name)
                assert fresh.connection is not None
                self.expire_time = fresh.connection.expire_time
                return fresh.connection.url, fresh.connection.token

            self.conn = RuntimeConnection(runtime.connection.url, runtime.connection.token, refresh=refresh)
        return self.conn

    def _token_seconds_left(self) -> float | None:
        if self.expire_time is None:
            return None
        return self.expire_time.timestamp() - now()

    def kernel(self, which: str) -> Kernel:
        with self.setup_lock:
            conn = self._connection()
            left = self._token_seconds_left()
            if left is not None and left < 300:
                conn.refresh_token()
                for k in self.kernels.values():
                    k.close()
                self.kernels.clear()
            if which in self.kernels and self.kernels[which].ws is not None:
                return self.kernels[which]
            path = f"gpudock/{which}.ipynb"
            kernel_id = None
            for session in conn.list_sessions():
                if session.get("path") == path:
                    kernel_id = session["kernel"]["id"]
                    # Only run_agent asks for the worker, holding worker_lock, before it sends
                    # anything: if the kernel is busy now, it runs a command orphaned by a
                    # restart of GPUDock. Nobody will collect its result, and everything sent
                    # would queue behind it, so stop it. (The console is left alone: that may
                    # be the user's own long-running code.)
                    if which == self.WORKER and session["kernel"].get("execution_state") == "busy":
                        conn.interrupt(kernel_id)
                        self.orphan_interrupted = True
                        # wait for it to wind down: a request queued behind a cell that
                        # fails with KeyboardInterrupt would be aborted along with it
                        deadline = time.monotonic() + 30
                        while time.monotonic() < deadline:
                            time.sleep(0.5)
                            resp = conn.request("GET", f"api/kernels/{kernel_id}")
                            if resp.status_code != 200 or resp.json().get("execution_state") != "busy":
                                break
                    break
            if kernel_id is None:
                _sid, kernel_id = conn.create_session(name=which)
            k = Kernel(conn, kernel_id).connect()
            self.kernels[which] = k
            return k

    def execute(self, code: str, on_output: Callable[[str, str], None]) -> ExecResult:
        with self.exec_lock:
            kernel = self.kernel(self.CONSOLE)
            msg_id = kernel.send_execute(code)
            result = ExecResult(msg_id=msg_id, started=time.monotonic())
            reconnects = 0
            while True:
                try:
                    return kernel.wait(msg_id, on_output=on_output, idle_timeout=None, result=result)
                except ConnectionClosed:
                    if reconnects >= 5:
                        raise
                    reconnects += 1
                    on_output("system", f"\n[与运行时的连接断开，第 {reconnects} 次重连…]\n")
                    time.sleep(min(2**reconnects, 30))
                    kernel.reconnect(refresh_token=True)

    def interrupt(self) -> None:
        kernel = self.kernels.get(self.CONSOLE)
        if kernel is None:
            return
        self._connection().interrupt(kernel.kernel_id)

    def stats(self, max_age: float = 5.0) -> dict[str, Any]:
        """Resource usage from the monitor kernel.

        Never queues: the UI polls every few seconds, and a slow kernel (just
        started, or the runtime is gone) must not pile requests up until the
        server's worker threads run out. While a read is in flight, or for a
        short while after one failed, the last result (or a placeholder) is
        returned instead.
        """
        if self.stats_cache and now() - self.stats_at < max_age:
            return self.stats_cache
        if now() - self.stats_failed_at < 20 or not self.monitor_lock.acquire(blocking=False):
            return {**(self.stats_cache or {}), "stale": True}
        try:
            kernel = self.kernel(self.MONITOR)
            try:
                data = kernel.eval_json("_gpudock_stats_result", setup=STATS_CODE, idle_timeout=30)
            except ConnectionClosed:
                kernel.reconnect(refresh_token=True)
                data = kernel.eval_json("_gpudock_stats_result", setup=STATS_CODE, idle_timeout=30)
        except Exception:
            self.stats_failed_at = now()
            raise
        finally:
            self.monitor_lock.release()
        self.stats_cache, self.stats_at = data, now()
        return data

    def close(self) -> None:
        for k in self.kernels.values():
            k.close()
        self.kernels.clear()
        if self.conn is not None:
            self.conn.close()
            self.conn = None


# ---------------------------------------------------------------------------
# the manager
# ---------------------------------------------------------------------------
class Manager:
    def __init__(
        self,
        *,
        config_dir,
        api_factory: Callable[[], ColabApi] | None = None,
        login_fn: Callable[..., Any] = login_oauth,
        runtime_fs_root: str = "/",
    ) -> None:
        self.config_dir = config_dir
        self.runtime_fs_root = runtime_fs_root
        self.owned = OwnedRuntimes(config_dir / "runtimes.json")
        self.usage = UsageLedger(config_dir / "usage.json")
        self.settings_path = config_dir / "app_settings.json"
        self.settings = Settings.load(self.settings_path)
        # A create that was still waiting when GPUDock last quit may have finished
        # afterwards; drop the "pending" mark so the idle auto-release covers it.
        for name, meta in self.owned.all().items():
            if meta.get("pending"):
                self.owned.update(name, pending=False, created_at=meta.get("created_at") or now(), last_activity=now())
        self._api_factory = api_factory or (lambda: ColabApi(Authenticator(AuthSettings.load()).headers))
        self._login_fn = login_fn
        self._api: ColabApi | None = None
        self._lock = threading.RLock()
        self.handles: dict[str, RuntimeHandle] = {}
        self.jobs: dict[str, Job] = {}
        self.execs: dict[str, ExecRecord] = {}
        self.events: list[dict[str, Any]] = []
        self.events_offset = 0
        self.login: dict[str, Any] = {"status": "idle", "url": None, "error": None}
        self._cache: dict[str, tuple[float, Any]] = {}
        self._live: tuple[float, list[Runtime]] | None = None
        self._live_lock = threading.Lock()
        self._last_seen: dict[str, float] = {}
        self._reaper: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- helpers ------------------------------------------------------------
    @property
    def api(self) -> ColabApi:
        with self._lock:
            if self._api is None:
                self._api = self._api_factory()
            return self._api

    def _cached(self, key: str, ttl: float, fn: Callable[[], Any]) -> Any:
        hit = self._cache.get(key)
        if hit and now() - hit[0] < ttl:
            return hit[1]
        value = fn()
        self._cache[key] = (now(), value)
        return value

    def _reset(self) -> None:
        with self._lock:
            for h in self.handles.values():
                h.close()
            self.handles.clear()
            self._api = None
            self._cache.clear()
            self._live = None

    MAX_EVENTS = 300

    def _event(self, kind: str, message: str, **data: Any) -> None:
        self.events.append({"i": self.events_offset + len(self.events), "t": now(), "kind": kind, "message": message, **data})
        if len(self.events) > self.MAX_EVENTS:
            drop = len(self.events) - self.MAX_EVENTS
            del self.events[:drop]
            self.events_offset += drop

    def events_since(self, since: int) -> dict[str, Any]:
        return {"events": self.events[max(0, since - self.events_offset):], "next": self.events_offset + len(self.events)}

    def live_runtimes(self, max_age: float = 3.0) -> list[Runtime]:
        """Colab's runtime list, shared by every caller for a few seconds (the UI
        polls it from each open tab; one request at a time goes to Colab)."""
        cached = self._live
        if cached and now() - cached[0] < max_age:
            return cached[1]
        with self._live_lock:
            cached = self._live
            if cached and now() - cached[0] < max_age:
                return cached[1]
            live = self.api.list_runtimes()
            self._live = (now(), live)
            for r in live:
                self._last_seen[r.name] = now()
            return live

    def _invalidate_live(self) -> None:
        self._live = None

    def handle(self, name: str) -> RuntimeHandle:
        name = _full(name)
        with self._lock:
            if name not in self.handles:
                self.handles[name] = RuntimeHandle(name, self.api, fs_root=self.runtime_fs_root)
            return self.handles[name]

    # ---- auth -------------------------------------------------------------
    def auth_status(self) -> dict[str, Any]:
        settings = AuthSettings.load()
        logged_in = settings.mode == "adc" or TokenStore().load() is not None
        try:
            resolve_client_config()
            client_ready, client_error = True, None
        except AuthError as err:
            client_ready, client_error = settings.mode == "adc", str(err)
        return {
            "logged_in": logged_in,
            "mode": settings.mode,
            "client_ready": client_ready,
            "client_error": client_error,
            "login": dict(self.login),
        }

    def start_login(self) -> dict[str, Any]:
        with self._lock:
            if self.login.get("status") == "running" and now() - self.login.get("started", 0) < 600:
                return dict(self.login)
            self.login = {"status": "running", "url": None, "error": None, "started": now()}

        def on_url(url: str) -> None:
            self.login["url"] = url

        def run() -> None:
            try:
                self._login_fn(on_url=on_url)
                self._reset()
                self.login.update(status="done", error=None)
            except AuthError as err:
                self.login.update(status="error", error=str(err))
            except Exception as err:  # timeout, closed browser, network
                self.login.update(status="error", error=f"登录没有完成：{type(err).__name__}。可以再点一次“用 Google 登录”。")

        threading.Thread(target=run, name="gpudock-login", daemon=True).start()
        return dict(self.login)

    def logout(self) -> None:
        TokenStore().clear()
        self._reset()
        self.login = {"status": "idle", "url": None, "error": None}

    def account(self) -> dict[str, Any]:
        tier = self._cached("tier", 120, self.api.get_subscription)

        def email() -> str | None:
            try:
                return Authenticator(AuthSettings.load()).token_info().get("email")
            except Exception:
                return None

        return {"tier": tier, "email": self._cached("email", 3600, email)}

    # ---- specs & runtimes -------------------------------------------------
    def specs(self) -> list[dict[str, Any]]:
        return self._cached("specs", 300, lambda: catalog.build_cards(self.api.list_specs()))

    def runtimes(self) -> dict[str, Any]:
        live = self.live_runtimes()
        owned = self.owned.all()
        names = {r.name for r in live}
        # forget owned runtimes that Colab already reclaimed
        for name, meta in owned.items():
            if name not in names and not meta.get("pending"):
                self.usage.add(name, meta, self._last_seen.get(name) or now())
                self.owned.remove(name)
                with self._lock:
                    handle = self.handles.pop(name, None)
                if handle:
                    handle.close()
                self._event("reclaimed", f"{meta.get('label', name)} 已被 Colab 回收或在别处释放。", runtime=name)
        items = [self._runtime_view(r, owned.get(r.name)) for r in live]
        pending = [j.to_dict() for j in self.jobs.values() if j.kind == "create" and j.status == "running"]
        for job_id, j in list(self.jobs.items()):
            if j.finished and now() - j.finished > 3600:
                self.jobs.pop(job_id, None)
        return {
            "runtimes": items,
            "pending": pending,
            "auto_release_minutes": self.settings.auto_release_minutes,
            "release_on_exit": self.settings.release_on_exit,
            "usage": self.usage_summary(items),
        }

    def usage_summary(self, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        running = [
            {"start": r["created_at"], "end": now(), "cu_per_hour": r["cu_per_hour"]}
            for r in (items or []) if r.get("owned") and r.get("created_at")
        ]
        return self.usage.summary(running)

    def _runtime_view(self, rt: Runtime, meta: dict[str, Any] | None) -> dict[str, Any]:
        spec = rt.spec or SpecKey("VARIANT_UNSPECIFIED", "NONE")
        info = catalog.info_for(spec.accelerator, spec.shape)
        handle = self.handles.get(rt.name)
        created = meta.get("created_at") if meta else None
        last = max(filter(None, [meta.get("last_activity") if meta else None, handle.last_activity if handle else None]), default=None)
        busy = bool(handle and (handle.busy_exec or handle.busy_task))
        idle_s = 0 if busy or last is None else now() - last
        auto = self.settings.auto_release_minutes
        uptime = now() - created if created else None
        rate = info.get("cu_per_hour")
        return {
            "name": rt.name,
            "id": rt.id,
            "accelerator": spec.accelerator,
            "shape": spec.shape,
            "label": spec.label,
            "display_name": info.get("name", spec.accelerator),
            "vram_gb": info.get("vram_gb"),
            "cu_per_hour": rate,
            "owned": meta is not None,
            "auto": bool(meta and meta.get("auto")),
            "created_at": created,
            "uptime_s": uptime,
            "est_cu": round(uptime / 3600 * rate, 2) if (uptime and rate) else None,
            "busy": busy,
            "busy_exec": handle.busy_exec if handle else None,
            "busy_task": handle.busy_task if handle else None,
            "idle_s": idle_s,
            "auto_release_in_s": max(0, auto * 60 - idle_s) if (meta is not None and auto > 0 and not busy) else None,
        }

    retry_wait_s = 45.0
    max_queue_minutes = 20.0

    def create_runtime(self, accelerator: str, shape: str = "SHAPE_STANDARD", *, auto_retry: bool = True, auto: bool = False) -> Job:
        """Ask Colab for a runtime. When the accelerator is out of stock
        (NO_RUNTIMES), keep retrying in the background for up to
        ``max_queue_minutes`` unless ``auto_retry`` is off or the job is cancelled."""
        accelerator = accelerator.upper()
        specs = self.api.list_specs()
        match = next((s for s in specs if s.key.accelerator.upper() == accelerator and s.key.shape == shape), None)
        if match is None:
            raise LookupError(f"你的账号没有 {accelerator}（{shape}）这个规格。")
        if not match.eligible:
            raise PermissionError(f"你的账号当前不能申请 {match.key.label}，可能需要升级 Colab 订阅。")
        info = catalog.info_for(accelerator, shape)
        job = Job(id=uuid.uuid4().hex[:12], kind="create", label=match.key.label, accelerator=accelerator, shape=shape, message="正在向 Colab 申请…", auto=auto)
        self.jobs[job.id] = job

        def attempt() -> Runtime:
            runtime_id = f"gpudock-{uuid.uuid4().hex[:8]}"
            name = f"runtimes/{runtime_id}"
            self.owned.add(name, label=match.key.label, accelerator=accelerator, shape=shape, pending=True)

            def progress(msg: str) -> None:
                job.message = msg

            try:
                try:
                    runtime = self.api.create_runtime(match.key, runtime_id=runtime_id, on_progress=progress)
                except ColabApiError as err:
                    if err.status != "INVALID_ARGUMENT":
                        raise
                    self.owned.remove(name)
                    runtime = self.api.create_runtime(match.key, on_progress=progress)
                    self.owned.add(runtime.name, label=match.key.label, accelerator=accelerator, shape=shape)
            except Exception:
                self.owned.remove(name)
                raise
            if runtime.name != name:
                self.owned.remove(name)
                if self.owned.get(runtime.name) is None:
                    self.owned.add(runtime.name, label=match.key.label, accelerator=accelerator, shape=shape)
            self.owned.update(runtime.name, pending=False, created_at=now(), last_activity=now(), auto=auto)
            self._invalidate_live()
            return runtime

        def run() -> None:
            deadline = now() + self.max_queue_minutes * 60
            try:
                while True:
                    job.attempts += 1
                    try:
                        runtime = attempt()
                        break
                    except ColabApiError as err:
                        out_of_stock = err.reason == "NO_RUNTIMES" or "resource_exhausted" in err.message
                        if not (auto_retry and out_of_stock) or now() + self.retry_wait_s > deadline:
                            raise
                        waited = int((now() - job.started) / 60)
                        job.message = f"{match.key.label} 暂时没货，排队自动重试中（已尝试 {job.attempts} 次，已等 {waited} 分钟）"
                        end = now() + self.retry_wait_s
                        while now() < end:
                            if job.cancelled:
                                raise ColabApiError(None, 1, "CANCELLED", "已取消排队") from None
                            time.sleep(min(1.0, self.retry_wait_s))
                    if job.cancelled:
                        raise ColabApiError(None, 1, "CANCELLED", "已取消排队")
                job.status, job.message = "done", "已就绪"
                job.result = {"name": runtime.name, "id": runtime.id}
                self._event("created", f"{info.get('name', accelerator)} 已就绪，可以开始使用。", runtime=runtime.name)
            except Exception as err:
                job.status = "error"
                if isinstance(err, ColabApiError) and err.status == "CANCELLED":
                    job.status, job.error = "cancelled", "已取消排队"
                else:
                    job.error = str(err) if isinstance(err, ColabApiError | AuthError) else f"{type(err).__name__}: {err}"
            finally:
                job.finished = now()

        threading.Thread(target=run, name=f"gpudock-create-{job.id}", daemon=True).start()
        return job

    def release(self, name: str, *, force: bool = False, reason: str = "manual") -> None:
        name = _full(name)
        meta = self.owned.get(name)
        if meta is None and not force:
            raise PermissionError("这个运行时不是 GPUDock 创建的，请在 Colab 网页里管理它。")
        with self._lock:
            handle = self.handles.pop(name, None)
        if handle:
            handle.close()
        self.api.delete_runtime(name, allow_missing=True)
        if meta:
            self.usage.add(name, meta, now())
        self.owned.remove(name)
        self._invalidate_live()
        label = (meta or {}).get("label", name)
        if reason == "idle":
            self._event("auto_released", f"{label} 已空闲 {self.settings.auto_release_minutes} 分钟，已自动释放以节省计算单元。", runtime=name)
        elif reason == "task":
            self._event("auto_released", f"任务完成，{label} 已按设置释放，计费已停止。", runtime=name)
        else:
            self._event("released", f"{label} 已释放。", runtime=name)

    def release_all_owned(self) -> list[str]:
        """Release every runtime GPUDock created (used when quitting). Returns labels."""
        done = []
        for name, meta in self.owned.all().items():
            if meta.get("pending"):
                continue
            try:
                self.release(name, reason="exit")
                done.append(meta.get("label", name))
            except Exception:
                pass
        return done

    # ---- code execution ---------------------------------------------------
    def execute(self, name: str, code: str) -> ExecRecord:
        handle = self.handle(name)
        if handle.busy_exec:
            raise RuntimeError("这个运行时正在执行代码，等它结束或先点“中断”。")
        record = ExecRecord(id=uuid.uuid4().hex[:12], runtime=handle.name, code=code)
        self.execs[record.id] = record
        for old in [k for k, r in self.execs.items() if r.finished][:-30]:
            self.execs.pop(old, None)
        handle.busy_exec = record.id
        self._touch(handle)

        def run() -> None:
            try:
                res = handle.execute(code, record.add)
                record.status = res.status if res.status in ("ok", "error", "aborted") else "ok"
                if res.error:
                    record.error = res.error
            except Exception as err:
                record.status = "error"
                record.error = {"ename": type(err).__name__, "evalue": str(err), "traceback": []}
                record.add("system", f"\n[执行失败：{err}]\n")
            finally:
                record.finished = now()
                handle.busy_exec = None
                self._touch(handle)

        threading.Thread(target=run, name=f"gpudock-exec-{record.id}", daemon=True).start()
        return record

    def interrupt(self, name: str) -> None:
        self.handle(name).interrupt()

    def stats(self, name: str) -> dict[str, Any]:
        return self.handle(name).stats()

    def _touch(self, handle: RuntimeHandle) -> None:
        handle.last_activity = now()
        self.owned.update(handle.name, last_activity=handle.last_activity)

    # ---- settings & auto-release -------------------------------------------
    def update_settings(self, *, auto_release_minutes: int | None = None, release_on_exit: bool | None = None) -> Settings:
        if auto_release_minutes is not None:
            if not 0 <= auto_release_minutes <= 24 * 60:
                raise ValueError("自动释放时间要在 0（关闭）到 1440 分钟之间。")
            self.settings.auto_release_minutes = auto_release_minutes
        if release_on_exit is not None:
            self.settings.release_on_exit = release_on_exit
        self.settings.save(self.settings_path)
        return self.settings

    def reap_once(self) -> list[str]:
        """Release owned runtimes idle longer than the setting. Returns released names."""
        minutes = self.settings.auto_release_minutes
        if minutes <= 0:
            return []
        released = []
        try:
            live = {r.name for r in self.live_runtimes(max_age=0)}
        except Exception:
            return []
        for name, meta in self.owned.all().items():
            if meta.get("pending") or name not in live:
                continue
            handle = self.handles.get(name)
            if handle and (handle.busy_exec or handle.busy_task):
                continue
            last = max(meta.get("last_activity") or 0, handle.last_activity if handle else 0)
            if now() - last >= minutes * 60:
                try:
                    self.release(name, reason="idle")
                    released.append(name)
                except Exception as err:
                    self._event("error", f"自动释放 {name} 失败：{err}", runtime=name)
        return released

    def start_reaper(self, interval: float = 60.0) -> None:
        def loop() -> None:
            while not self._stop.wait(interval):
                if TokenStore().load() is None and AuthSettings.load().mode != "adc":
                    continue
                self.reap_once()

        self._reaper = threading.Thread(target=loop, name="gpudock-reaper", daemon=True)
        self._reaper.start()

    def stop(self) -> None:
        self._stop.set()
        self._reset()


def _full(name: str) -> str:
    return name if name.startswith("runtimes/") else f"runtimes/{name}"
