"""Batch jobs (deploy a model, transcribe, read aloud, dub) on a Colab runtime.

A task uploads its inputs, makes sure the models it needs are deployed, then
drives the runtime agent step by step, mapping each step's progress onto one
0–100% bar. Results are downloaded to ``~/GPUDock/outputs/<task>/``.
Tasks on the same runtime run one at a time.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..agent.gpudock_agent import (
    GEN_MODELS,
    KOKORO_VOICES,
    LANGS,
    RECIPES,
    batches,
    parse_translations,
    shorten_messages,
    translation_messages,
)
from ..files import download, sha256_file, upload
from . import catalog
from .state import AgentBusy, AgentError, Manager, RuntimeHandle, now

KINDS = {
    "install": "部署模型",
    "transcribe": "批量转写",
    "tts": "批量朗读",
    "tts_preview": "试听",
    "dub": "批量配音",
    "chat": "对话",
    "generate": "创作",
}
# images each generation mode takes: (min, max)
GEN_IMAGES = {"i2v": (1, 1), "flf2v": (2, 2), "r2v": (1, 9), "edit": (1, 1)}
MAX_TTS_LINES = 500
ACTIVE = ("queued", "running", "waiting_review")
MEDIA_KINDS = {".mp4": "video", ".mov": "video", ".mkv": "video", ".webm": "video", ".wav": "audio", ".mp3": "audio", ".m4a": "audio", ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".srt": "subtitle", ".vtt": "subtitle", ".txt": "text", ".json": "data"}
_SAFE = re.compile(r"[^\w.\-一-鿿]+")


def workspace() -> Path:
    return Path(os.environ.get("GPUDOCK_WORKSPACE") or Path.home() / "GPUDock")


def safe_name(name: str) -> str:
    base = _SAFE.sub("_", Path(name).name).strip("._") or "file"
    stem, ext = os.path.splitext(base)
    return stem[:80] + ext[:10]


def simulate_placement(
    segs: list[dict], durations: dict[str, float], total: float, *, drift: float = 0.8, max_tempo: float = 1.35
) -> list[tuple[dict, float]]:
    """Mirror the renderer's timeline (late starts, borrowed time, tempo cap) and
    return (segment, needed_speedup) for every line."""
    out: list[tuple[dict, float]] = []
    cursor = 0.0
    for i, s in enumerate(segs):
        start = max(float(s["start"]), cursor)
        window_end = float(segs[i + 1]["start"]) + drift if i + 1 < len(segs) else total
        slot = max(0.25, window_end - start - 0.05)
        dur = float(durations.get(s.get("tts", ""), 0.0))
        out.append((s, dur / slot if slot else 0.0))
        played = min(dur / min(max(dur / slot, 1.0), max_tempo) if dur > slot * 1.02 else dur, slot)
        cursor = start + played + 0.08
    return out


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# uploads and secrets
# ---------------------------------------------------------------------------
@dataclass
class Upload:
    id: str
    name: str
    path: Path
    size: int

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "size": self.size}

    @property
    def sha256(self) -> str:
        """Content hash, kept next to the file (computed once for older uploads)."""
        side = self.path.parent / ".sha256"
        if side.exists():
            return side.read_text().strip()
        digest = sha256_file(self.path)
        side.write_text(digest)
        return digest


class UploadStore:
    KEEP_DAYS = 14

    def __init__(self, root: Path) -> None:
        self.root = root / "uploads"

    def save(self, filename: str, stream) -> Upload:
        uid = uuid.uuid4().hex[:12]
        folder = self.root / uid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / safe_name(filename)
        h = hashlib.sha256()
        with path.open("wb") as fh:
            while block := stream.read(4 * 1024 * 1024):
                h.update(block)
                fh.write(block)
        (folder / ".sha256").write_text(h.hexdigest())
        return Upload(uid, path.name, path, path.stat().st_size)

    def get(self, uid: str) -> Upload:
        if not re.fullmatch(r"[0-9a-f]{12}", uid or ""):
            raise LookupError("上传文件不存在")
        folder = self.root / uid
        files = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")] if folder.exists() else []
        if not files:
            raise LookupError("上传文件不存在或已被清理（超过 14 天的上传会自动删除），请重新上传")
        return Upload(uid, files[0].name, files[0], files[0].stat().st_size)

    def cleanup(self, keep: set[str] | None = None) -> int:
        """Delete uploads older than KEEP_DAYS (results live in outputs/, not here)."""
        if not self.root.exists():
            return 0
        cutoff = time.time() - self.KEEP_DAYS * 86400
        removed = 0
        for folder in self.root.iterdir():
            if folder.is_dir() and folder.name not in (keep or set()) and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed


class SecretStore:
    """Small secrets (the translation API key): OS keyring, else a 0600 file."""

    SERVICE = "gpudock"

    def __init__(self, config_dir: Path) -> None:
        self.dir = config_dir

    def _keyring(self):
        if os.environ.get("GPUDOCK_NO_KEYRING"):
            return None
        try:
            import keyring

            keyring.get_keyring()
            return keyring
        except Exception:
            return None

    def set(self, name: str, value: str | None) -> None:
        kr = self._keyring()
        path = self.dir / f"secret-{name}"
        if kr is not None:
            try:
                if value:
                    kr.set_password(self.SERVICE, name, value)
                else:
                    kr.delete_password(self.SERVICE, name)
                path.unlink(missing_ok=True)
                return
            except Exception:
                pass
        if value:
            path.write_text(value)
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        else:
            path.unlink(missing_ok=True)

    def get(self, name: str) -> str | None:
        kr = self._keyring()
        if kr is not None:
            try:
                value = kr.get_password(self.SERVICE, name)
                if value:
                    return value
            except Exception:
                pass
        path = self.dir / f"secret-{name}"
        return path.read_text() if path.exists() else None


# ---------------------------------------------------------------------------
# task model
# ---------------------------------------------------------------------------
@dataclass
class Task:
    id: str
    kind: str
    title: str
    runtime: str
    params: dict[str, Any]
    uploads: list[str]
    out_dir: str
    status: str = "queued"  # queued | running | waiting_review | done | error | cancelled | interrupted
    stage: str = "排队中"
    progress: float = 0.0
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    log_offset: int = 0
    outputs: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] | None = None
    created: float = field(default_factory=now)
    started: float | None = None
    finished: float | None = None
    cancel_requested: bool = False
    review_event: threading.Event = field(default_factory=threading.Event, repr=False)
    review_edits: dict[str, Any] | None = None
    auto_accel: str | None = None  # "start / reuse a runtime of this kind" instead of a fixed runtime
    auto_job: str | None = None
    release_after: bool = False
    accelerator: str | None = None
    cu_per_hour: float | None = None
    retry_of: str | None = None
    restorable: bool = True  # False for history from before GPUDock kept task parameters

    def log(self, line: str) -> None:
        self.logs.append(line)
        if len(self.logs) > 600:
            drop = len(self.logs) - 600
            self.logs = self.logs[drop:]
            self.log_offset += drop

    @property
    def est_cu(self) -> float | None:
        if not self.started or self.cu_per_hour is None:
            return None
        return round(((self.finished or now()) - self.started) / 3600 * self.cu_per_hour, 3)

    def summary(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "kind": self.kind,
            "kind_label": KINDS.get(self.kind, self.kind),
            "title": self.title,
            "runtime": self.runtime,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 4),
            "error": self.error,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "outputs": self.outputs,
            "out_dir": self.out_dir,
            "result": self.result,
            "accelerator": self.accelerator or self.auto_accel,
            "cu_per_hour": self.cu_per_hour,
            "est_cu": self.est_cu,
            "release_after": self.release_after,
            "retry_of": self.retry_of,
            "can_retry": self.restorable and self.status not in ACTIVE,
        }
        if self.kind == "generate" and self.restorable:
            out["gen"] = {k: self.params.get(k) for k in ("model", "mode", "prompt", "width", "height", "seconds", "fast", "seed")}
        return out

    def record(self) -> dict[str, Any]:
        """What the history file keeps: the summary plus what a retry needs."""
        return {**self.summary(), "params": self.params, "uploads": self.uploads, "auto_accel": self.auto_accel}

    def detail(self, log_since: int = 0) -> dict[str, Any]:
        start = max(0, log_since - self.log_offset)
        return {
            **self.summary(),
            "params": {k: v for k, v in self.params.items() if k != "text"},
            "items": list(self.items.values()),
            "logs": self.logs[start:],
            "log_next": self.log_offset + len(self.logs),
            "review": self.review if self.status == "waiting_review" else None,
        }


# ---------------------------------------------------------------------------
# the task manager
# ---------------------------------------------------------------------------
class TaskManager:
    def __init__(self, manager: Manager, root: Path | None = None) -> None:
        self.m = manager
        self.root = root or workspace()
        self.uploads = UploadStore(self.root)
        self.secrets = SecretStore(manager.config_dir)
        self.tasks: dict[str, Task] = {}
        self._runtime_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self._auto_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self.history_path = manager.config_dir / "tasks.json"
        self.translator_path = manager.config_dir / "translator.json"
        self._load_history()
        threading.Thread(target=self._cleanup_uploads, name="gpudock-cleanup", daemon=True).start()

    def _cleanup_uploads(self) -> None:
        recent = {u for t in self.tasks.values() if t.created > time.time() - UploadStore.KEEP_DAYS * 86400 for u in t.uploads}
        try:
            self.uploads.cleanup(keep=recent)
        except OSError:
            pass

    # ---- history ------------------------------------------------------------
    def _load_history(self) -> None:
        if not self.history_path.exists():
            return
        try:
            for s in json.loads(self.history_path.read_text(encoding="utf-8")):
                interrupted = s["status"] not in ("done", "error", "cancelled")
                t = Task(
                    id=s["id"], kind=s["kind"], title=s["title"], runtime=s["runtime"], params=s.get("params") or {},
                    uploads=s.get("uploads") or [], out_dir=s["out_dir"],
                    status="interrupted" if interrupted else s["status"],
                    stage="GPUDock 重启时任务中断" if interrupted else s.get("stage", ""),
                    progress=s.get("progress", 0), outputs=s.get("outputs", []),
                    error=s.get("error"), result=s.get("result") or {}, created=s["created"], started=s.get("started"),
                    finished=s.get("finished") or (s.get("started") if interrupted else None),
                    auto_accel=s.get("auto_accel"), release_after=bool(s.get("release_after")),
                    accelerator=s.get("accelerator"), cu_per_hour=s.get("cu_per_hour"), retry_of=s.get("retry_of"),
                    restorable="params" in s,
                )
                self.tasks[t.id] = t
        except (ValueError, KeyError):
            pass

    def _save_history(self) -> None:
        with self._history_lock:
            done = sorted(self.tasks.values(), key=lambda t: t.created)[-200:]
            tmp = self.history_path.with_suffix(".tmp")
            tmp.write_text(json.dumps([t.record() for t in done], ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.history_path)

    # ---- public API -----------------------------------------------------------
    def list(self, *, active: bool = False, kind: str | None = None) -> list[dict[str, Any]]:
        tasks = sorted(self.tasks.values(), key=lambda t: t.created, reverse=True)
        return [t.summary() for t in tasks if (not active or t.status in ACTIVE) and (not kind or t.kind == kind)]

    def get(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise LookupError("没有这个任务")
        return task

    def create(
        self,
        kind: str,
        runtime_id: str,
        params: dict[str, Any],
        upload_ids: list[str] | None = None,
        *,
        accelerator: str | None = None,
        release_after: bool = False,
        retry_of: str | None = None,
    ) -> Task:
        """``runtime_id`` "auto" means: reuse a running GPUDock runtime with this
        ``accelerator``, else wait for one being started, else start one."""
        if kind not in KINDS:
            raise ValueError(f"不支持的任务类型：{kind}")
        auto_accel = None
        if runtime_id == "auto":
            auto_accel = (accelerator or "").upper()
            if catalog.CATALOG.get(auto_accel, {}).get("kind") != "gpu":
                raise ValueError("自动申请需要指定一种 GPU")
            runtime = ""
        else:
            runtime = runtime_id if runtime_id.startswith("runtimes/") else f"runtimes/{runtime_id}"
        uploads = [self.uploads.get(u) for u in (upload_ids or [])]
        self._validate(kind, params, uploads)
        if "voxcpm" in self._needs(kind, params):
            self._check_ram(runtime, auto_accel)
        task_id = time.strftime("%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        title = self._title(kind, params, uploads)
        out_dir = self.root / "outputs" / f"{task_id}-{KINDS[kind]}"
        task = Task(
            id=task_id, kind=kind, title=title, runtime=runtime, params=dict(params), uploads=[u.id for u in uploads], out_dir=str(out_dir),
            auto_accel=auto_accel, release_after=release_after, retry_of=retry_of,
        )
        if auto_accel:
            task.stage = f"等待 {auto_accel}"
        for u in uploads:
            task.items[u.name] = {"name": u.name, "status": "waiting", "detail": ""}
        self.tasks[task.id] = task
        threading.Thread(target=self._run, args=(task,), name=f"gpudock-task-{task.id}", daemon=True).start()
        return task

    def cancel(self, task_id: str) -> None:
        task = self.get(task_id)
        if task.status not in ("queued", "running", "waiting_review"):
            return
        task.cancel_requested = True
        task.review_event.set()
        if task.status == "running" and task.runtime:
            try:
                self.m.handle(task.runtime).interrupt_worker()
            except Exception:
                pass

    def retry(self, task_id: str, *, runtime: str | None = None, overrides: dict[str, Any] | None = None) -> Task:
        """Run a finished task again with the same inputs. A dub reuses the
        transcript and the (reviewed) translation of the earlier run."""
        old = self.get(task_id)
        if old.status in ACTIVE:
            raise RuntimeError("这个任务还在进行中")
        if not old.restorable:
            raise ValueError("这个任务是旧版本 GPUDock 创建的，没有保存参数，请重新填写后提交")
        params = {**old.params, **(overrides or {})}
        params = {k: v for k, v in params.items() if v is not None}
        if old.kind == "dub" and (Path(old.out_dir) / ".work" / "dub_segments.json").exists():
            params["resume_from"] = old.id
        accel = old.accelerator or old.auto_accel
        if runtime is None:
            live = {r.name for r in self.m.live_runtimes()}
            runtime = old.runtime if old.runtime in live else "auto"
        if runtime == "auto" and not accel:
            raise ValueError("原来的显卡已经释放了，请先选一张显卡")
        return self.create(old.kind, runtime, params, old.uploads, accelerator=accel, release_after=old.release_after, retry_of=old.id)

    def submit_review(self, task_id: str, edits: dict[str, Any]) -> None:
        task = self.get(task_id)
        if task.status != "waiting_review":
            raise RuntimeError("这个任务现在不需要校对")
        task.review_edits = edits
        task.review_event.set()

    def file_path(self, task_id: str, name: str) -> Path:
        task = self.get(task_id)
        base = Path(task.out_dir).resolve()
        path = (base / name).resolve()
        if base not in path.parents or not path.is_file():
            raise LookupError("文件不存在")
        return path

    def reveal(self, task_id: str) -> None:
        folder = Path(self.get(task_id).out_dir)
        folder.mkdir(parents=True, exist_ok=True)
        system = platform.system()
        cmd = ["open", str(folder)] if system == "Darwin" else ["explorer", str(folder)] if system == "Windows" else ["xdg-open", str(folder)]
        subprocess.Popen(cmd)

    def runtime_status(self, runtime_id: str, refresh: bool = False) -> dict[str, Any]:
        handle = self.m.handle(runtime_id)
        if handle.agent_status is None or refresh:
            try:
                handle.agent_status = handle.run_agent("status", {}, lambda e: None, blocking=False)
            except AgentBusy:
                return {**(handle.agent_status or {}), "busy": True}
        return {**handle.agent_status, "busy": bool(handle.busy_task)}

    # ---- translator settings -----------------------------------------------
    def translator(self) -> dict[str, Any]:
        cfg = {"base_url": "", "model": ""}
        if self.translator_path.exists():
            try:
                cfg.update(json.loads(self.translator_path.read_text(encoding="utf-8")))
            except ValueError:
                pass
        return {**cfg, "has_key": bool(self.secrets.get("translator-api-key"))}

    def set_translator(self, base_url: str, model: str, api_key: str | None) -> dict[str, Any]:
        base_url = base_url.strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            raise ValueError("API 地址要以 http:// 或 https:// 开头")
        self.translator_path.write_text(json.dumps({"base_url": base_url, "model": model.strip()}, ensure_ascii=False), encoding="utf-8")
        if api_key is not None:
            self.secrets.set("translator-api-key", api_key.strip() or None)
        return self.translator()

    def translate_api(self, segments: list[dict], src: str, tgt: str, on_progress: Callable[[float], None] | None = None) -> dict[int, str]:
        return self._llm_api(segments, lambda chunk: translation_messages(chunk, src, tgt), on_progress)

    def shorten_api(self, items: list[dict], lang: str) -> dict[int, str]:
        return self._llm_api(items, lambda chunk: shorten_messages(chunk, lang), None)

    def _llm_api(self, segments: list[dict], build: Callable[[list[dict]], list[dict]], on_progress: Callable[[float], None] | None) -> dict[int, str]:
        cfg = self.translator()
        key = self.secrets.get("translator-api-key")
        if not cfg["base_url"] or not cfg["model"] or not key:
            raise ValueError("还没有配置外部翻译 API（地址、模型和密钥）")
        url = cfg["base_url"] + "/chat/completions"
        out: dict[int, str] = {}
        chunks = list(batches(segments, 25))
        with httpx.Client(timeout=180) as client:
            for i, chunk in enumerate(chunks):
                for attempt in range(3):
                    try:
                        r = client.post(
                            url,
                            headers={"Authorization": f"Bearer {key}"},
                            json={"model": cfg["model"], "messages": build(chunk), "temperature": 0.3},
                        )
                        if r.status_code >= 400:
                            raise RuntimeError(f"翻译 API 返回 HTTP {r.status_code}：{r.text[:300]}")
                        out.update(parse_translations(r.json()["choices"][0]["message"]["content"]))
                        break
                    except (ValueError, KeyError, httpx.HTTPError, RuntimeError):
                        if attempt == 2:
                            raise
                        time.sleep(2 * (attempt + 1))
                if on_progress:
                    on_progress((i + 1) / len(chunks))
        return out

    @staticmethod
    def _needs(kind: str, p: dict[str, Any]) -> set[str]:
        if kind == "install":
            return {p.get("recipe", "")}
        if kind in ("tts", "tts_preview"):
            return {p.get("engine", "kokoro")}
        if kind == "dub":
            return {"voxcpm" if p.get("voice_mode") == "clone" else "kokoro", "whisper"}
        return set()

    def _check_ram(self, runtime: str, auto_accel: str | None = None) -> None:
        """Colab's T4 (standard shape) has ~12.7 GB RAM; VoxCPM2 peaks around 10 GB while loading."""
        if auto_accel:
            if auto_accel in ("T4", "NONE"):
                raise ValueError("声音克隆（VoxCPM2）加载时需要约 10 GB 内存，T4 标准版不够：请换 L4、A100，或 T4 高内存版。")
            return
        try:
            spec = self.m.api.get_runtime(runtime).spec
        except Exception:
            return
        if spec and spec.accelerator in ("T4", "NONE") and spec.shape != "SHAPE_HIGHMEM":
            raise ValueError("声音克隆（VoxCPM2）加载时需要约 10 GB 内存，T4 标准版不够：请换 L4、A100，或 T4 高内存版。")

    # ---- validation ---------------------------------------------------------
    def _validate(self, kind: str, p: dict[str, Any], uploads: list[Upload]) -> None:
        if kind == "install" and p.get("recipe") not in RECIPES:
            raise ValueError("未知的模型")
        if kind == "install" and any(g not in GEN_MODELS for g in p.get("gen_models") or []):
            raise ValueError("未知的生成模型")
        if kind == "install" and any(GEN_MODELS[g].get("gated") for g in p.get("gen_models") or []) and not self.hf_token():
            raise ValueError("这个模型的权重需要 Hugging Face 授权：请先在“创作”页填写 Token")
        if kind == "generate":
            self._validate_generate(p, uploads)
        if kind in ("transcribe", "dub") and not uploads:
            raise ValueError("请先选择要处理的文件")
        if kind in ("tts", "tts_preview"):
            if not (p.get("text") or "").strip():
                raise ValueError("请输入要朗读的文字")
            n = sum(1 for line in (p.get("text") or "").splitlines() if line.strip())
            if n > MAX_TTS_LINES:
                raise ValueError(f"一次最多 {MAX_TTS_LINES} 行，现在有 {n} 行，请分成几批提交")
            if p.get("engine") == "voxcpm" and uploads and not p.get("consent"):
                raise ValueError("使用参考音色克隆前，请先确认你拥有该声音的使用授权")
            if p.get("engine", "kokoro") == "kokoro" and not LANGS.get(p.get("lang", "zh"), {}).get("kokoro"):
                raise ValueError("Kokoro 不支持这种语言，请改用 VoxCPM2")
        if kind == "dub":
            if p.get("tgt_lang") not in LANGS:
                raise ValueError("请选择目标语言")
            if p.get("voice_mode") == "clone" and not p.get("consent"):
                raise ValueError("克隆原声前，请先确认你拥有视频中声音的使用授权")
            if p.get("voice_mode", "preset") == "preset" and not LANGS[p["tgt_lang"]].get("kokoro"):
                raise ValueError(f"预置音色不支持{LANGS[p['tgt_lang']]['name']}，请改用“克隆原声”")
            if p.get("translator") == "api":
                cfg = self.translator()
                if not (cfg["base_url"] and cfg["model"] and cfg["has_key"]):
                    raise ValueError("选择了外部翻译 API，但还没有配置地址、模型和密钥")
        if kind == "chat" and not (p.get("prompt") or "").strip():
            raise ValueError("请输入内容")

    def _validate_generate(self, p: dict[str, Any], uploads: list[Upload]) -> None:
        spec = GEN_MODELS.get(p.get("model") or "")
        if spec is None:
            raise ValueError("未知的生成模型")
        mode = p.get("mode")
        if mode not in spec["templates"]:
            raise ValueError(f"{spec['name']} 不支持这种模式")
        if not (p.get("prompt") or "").strip():
            raise ValueError("请输入提示词")
        for key in ("width", "height"):
            v = p.get(key)
            if not isinstance(v, int) or not 256 <= v <= 4096 or v % 16:
                raise ValueError("画面尺寸要在 256–4096 之间，且是 16 的倍数")
        if spec["kind"] == "video":
            sec = p.get("seconds")
            if not isinstance(sec, (int, float)) or not 1 <= sec <= spec.get("max_seconds", 5):
                raise ValueError(f"时长要在 1–{spec.get('max_seconds', 5)} 秒之间")
        lo, hi = GEN_IMAGES.get(mode, (0, 0))
        if not lo <= len(uploads) <= hi:
            raise ValueError(f"这个模式需要 {lo} 张图片" if lo == hi else f"这个模式需要 {lo}–{hi} 张图片")
        if any(not u.name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) for u in uploads):
            raise ValueError("只支持 PNG、JPG、WebP 图片")
        if spec.get("gated") and not self.hf_token():
            raise ValueError("这个模型的权重需要 Hugging Face 授权：请先填写 Token")

    def _title(self, kind: str, p: dict[str, Any], uploads: list[Upload]) -> str:
        if kind == "install" and p.get("gen_models"):
            return "下载 " + "、".join(GEN_MODELS[g]["name"] for g in p["gen_models"])
        if kind == "install":
            return f"部署 {RECIPES[p['recipe']]['name']}"
        if kind == "generate":
            return f"{GEN_MODELS[p['model']]['name']} · {p['prompt'].strip()[:20]}"
        if uploads:
            first = uploads[0].name
            return f"{KINDS[kind]} · {first}" + (f" 等 {len(uploads)} 个文件" if len(uploads) > 1 else "")
        if kind in ("tts", "tts_preview"):
            line = (p.get("text") or "").strip().splitlines()[0]
            return f"{KINDS[kind]} · {line[:20]}"
        if kind == "chat":
            return f"对话 · {p['prompt'][:20]}"
        return KINDS[kind]

    # ---- runner -----------------------------------------------------------------
    def _run(self, task: Task) -> None:
        if not task.runtime:
            try:
                self._resolve_runtime(task)
            except Exception as err:
                self._fail(task, err)
                self._finish(task)
                return
        lock = self._runtime_locks.setdefault(task.runtime, threading.Lock())
        with lock:
            handle = self.m.handle(task.runtime)
            task.status, task.started, task.stage = "running", now(), "准备中"
            task.accelerator, task.cu_per_hour = self._rate(task.runtime)
            handle.busy_task = task.id
            self.m._touch(handle)
            try:
                self._check(task)
                if handle.orphan_interrupted:
                    handle.orphan_interrupted = False
                    task.log("已停止 GPUDock 上次退出时留在运行时上的任务")
                getattr(self, f"_run_{task.kind}")(task, handle)
                task.status, task.stage, task.progress = "done", "完成", 1.0
            except Exception as err:
                self._fail(task, err)
            finally:
                task.finished = now()
                handle.busy_task = None
                self.m._touch(handle)
                if task.kind == "install":
                    handle.agent_status = None
        self._maybe_release(task)
        self._finish(task)

    @staticmethod
    def _fail(task: Task, err: Exception) -> None:
        if isinstance(err, Cancelled) or task.cancel_requested:
            task.status, task.stage = "cancelled", "已取消"
        else:
            task.status, task.error = "error", str(err)
            task.log(f"[错误] {err}")

    def _finish(self, task: Task) -> None:
        task.finished = task.finished or now()
        self._write_log(task)
        self._save_history()
        labels = {"done": "完成", "error": "失败", "cancelled": "已取消"}
        self.m._event(f"task_{task.status}", f"{task.title}：{labels.get(task.status, task.status)}", task=task.id)

    def _rate(self, runtime: str) -> tuple[str | None, float | None]:
        meta = self.m.owned.get(runtime)
        if meta and meta.get("accelerator"):
            accel, shape = meta["accelerator"], meta.get("shape", "SHAPE_STANDARD")
        else:
            try:
                spec = next((r.spec for r in self.m.live_runtimes() if r.name == runtime and r.spec), None)
            except Exception:
                spec = None
            if spec is None:
                return None, None
            accel, shape = spec.accelerator, spec.shape
        return accel, catalog.info_for(accel, shape).get("cu_per_hour")

    def _resolve_runtime(self, task: Task) -> None:
        """Pick the runtime for an "auto" task: a running GPUDock runtime of the
        wanted kind, else one being started, else start one. Tasks that end up on
        the same runtime then run one after another."""
        accel = task.auto_accel or ""
        with self._auto_lock:
            self._check(task)
            for r in self.m.live_runtimes(max_age=0):
                meta = self.m.owned.get(r.name)
                if meta and not meta.get("pending") and r.spec and r.spec.accelerator.upper() == accel:
                    task.runtime = r.name
                    task.log(f"使用已经在运行的 {meta.get('label', accel)}")
                    return
            job = next((j for j in self.m.jobs.values() if j.kind == "create" and j.status == "running" and j.accelerator == accel and not j.cancelled), None)
            if job is None:
                job = self.m.create_runtime(accel, "SHAPE_STANDARD", auto=True)
                task.log(f"已向 Colab 申请 {job.label}，拿到后自动开始")
            else:
                task.log(f"等待正在申请的 {job.label}")
            task.auto_job = job.id
        while job.status == "running":
            if task.cancel_requested:
                waiting = [t for t in self.tasks.values() if t is not task and t.auto_job == job.id and not t.runtime and not t.cancel_requested]
                if job.auto and not waiting:
                    job.cancelled = True
                raise Cancelled()
            task.stage = f"申请 {job.label} · {job.message}"
            time.sleep(1)
        if job.status != "done" or not job.result:
            raise RuntimeError(f"申请 {job.label} 没有成功：{job.error or job.status}")
        task.runtime = job.result["name"]
        task.log(f"{job.label} 已就绪")

    def _maybe_release(self, task: Task) -> None:
        """Release the runtime after a task that asked for it, unless more work is lined up for it."""
        if not task.release_after or not task.runtime or task.status == "waiting_review":
            return
        others = [
            t for t in self.tasks.values()
            if t is not task and t.status in ACTIVE and (t.runtime == task.runtime or (not t.runtime and t.auto_accel == task.accelerator))
        ]
        handle = self.m.handles.get(task.runtime)
        if others:
            task.log(f"还有 {len(others)} 个任务要用这张卡，暂不释放")
            return
        if handle and handle.busy_exec:
            task.log("控制台还在执行代码，暂不释放")
            return
        try:
            self.m.release(task.runtime, reason="task")
            task.log("已按设置释放运行时，计费停止")
        except PermissionError:
            task.log("这个运行时不是 GPUDock 创建的，没有自动释放")
        except Exception as err:
            task.log(f"释放运行时失败：{err}（空闲自动释放仍会兜底）")

    def _write_log(self, task: Task) -> None:
        if task.kind in ("chat", "tts_preview") and task.status == "done":
            return
        folder = Path(task.out_dir)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "task.log").write_text("\n".join(task.logs), encoding="utf-8")

    def _check(self, task: Task) -> None:
        if task.cancel_requested:
            raise Cancelled()

    def _agent(self, task: Task, handle: RuntimeHandle, cmd: str, args: dict[str, Any], base: float, span: float) -> dict[str, Any]:
        self._check(task)

        def on_event(evt: dict[str, Any]) -> None:
            kind = evt.get("type")
            if kind == "progress":
                task.progress = max(task.progress, base + span * float(evt.get("value") or 0))
                if evt.get("stage"):
                    task.stage = evt["stage"] + (f" · {evt['item']}" if evt.get("item") else "")
                handle.last_activity = now()
            elif kind in ("log", "raw", "error"):
                task.log(str(evt.get("message", "")))
            elif kind == "item":
                name = evt.get("name")
                if name in task.items:
                    task.items[name]["status"] = evt.get("status", "running")

        try:
            return handle.run_agent(cmd, args, on_event)
        except AgentError as err:
            if task.cancel_requested:
                raise Cancelled() from None
            raise RuntimeError(f"{cmd} 步骤失败：{err}") from None

    def _upload_inputs(self, task: Task, handle: RuntimeHandle, base: float, span: float) -> list[dict[str, Any]]:
        files = []
        uploads = [self.uploads.get(u) for u in task.uploads]
        total = sum(u.size for u in uploads) or 1
        done = 0
        conn = handle._connection()
        skipped = 0
        for u in uploads:
            self._check(task)
            task.stage = f"上传 · {u.name}"
            task.items[u.name]["status"] = "uploading"
            # content-addressed, so the same file is sent to a runtime only once
            remote = handle.remote("inputs", u.sha256[:24], u.name)
            if self._remote_size(conn, remote) == u.size:
                skipped += 1
            else:
                upload(conn, u.path, remote, on_progress=lambda d, t, _done=done: setattr(task, "progress", base + span * (_done + d) / total))
            done += u.size
            task.progress = base + span * done / total
            task.items[u.name]["status"] = "uploaded"
            files.append({"name": u.name, "path": handle.absolute(remote), "remote": remote, "size": u.size})
        task.log(f"已上传 {len(files) - skipped} 个文件到运行时" + (f"，{skipped} 个已在运行时上，跳过" if skipped else ""))
        return files

    @staticmethod
    def _remote_size(conn, remote: str) -> int | None:
        try:
            resp = conn.request("GET", "api/contents/" + quote(remote, safe="/"), params={"content": "0"})
            if resp.status_code == 200:
                return resp.json().get("size")
        except (httpx.HTTPError, ValueError):
            pass
        return None

    def _ensure(self, task: Task, handle: RuntimeHandle, recipes: list[str], base: float, span: float, models: dict[str, str] | None = None) -> None:
        if handle.agent_status is None:
            # on a fresh runtime this starts the worker kernel first: tens of seconds
            task.stage = "检查运行时环境"
        status = self.runtime_status(task.runtime) if handle.agent_status is None else handle.agent_status
        have = (status or {}).get("installed", {})
        def has(recipe: str) -> bool:
            if recipe not in have:
                return False
            want = (models or {}).get(recipe)
            if not want:
                return True
            info = have[recipe]
            return want == info.get("model") or want in info.get("models", []) or info.get("model") is None

        missing = [r for r in dict.fromkeys(recipes) if not has(r)]
        for i, recipe in enumerate(missing):
            task.log(f"部署 {RECIPES[recipe]['name']}（约 {RECIPES[recipe]['install_minutes']} 分钟，只在这个运行时第一次使用时需要）")
            args = {"recipe": recipe}
            if models and models.get(recipe):
                args["model"] = models[recipe]
            self._agent(task, handle, "install", args, base + span * i / len(missing), span / len(missing))
        if missing:
            handle.agent_status = None

    def _fetch(self, task: Task, handle: RuntimeHandle, abs_path: str, local_name: str | None = None) -> Path:
        remote = posixpath.relpath(abs_path, handle.fs_root)
        local = Path(task.out_dir) / (local_name or posixpath.basename(abs_path))
        download(handle._connection(), remote, local)
        kind = MEDIA_KINDS.get(local.suffix.lower(), "file")
        task.outputs.append({"name": local.name, "size": local.stat().st_size, "kind": kind})
        return local

    def _fetch_json(self, task: Task, handle: RuntimeHandle, abs_path: str) -> Any:
        tmp = Path(task.out_dir) / ".work" / posixpath.basename(abs_path)
        download(handle._connection(), posixpath.relpath(abs_path, handle.fs_root), tmp)
        data = json.loads(tmp.read_text(encoding="utf-8"))
        return data

    def _work(self, handle: RuntimeHandle, task: Task, *parts: str) -> str:
        return handle.absolute(handle.remote("tasks", task.id, *parts))

    # ---- kinds ------------------------------------------------------------------
    def _run_install(self, task: Task, handle: RuntimeHandle) -> None:
        p = task.params
        args: dict[str, Any] = {"recipe": p["recipe"]}
        if p.get("model"):
            args["model"] = p["model"]
        if p.get("gen_models"):
            args["gen_models"] = list(p["gen_models"])
            args["_secrets"] = {"hf_token": self.hf_token()}
        self._agent(task, handle, "install", args, 0.0, 1.0)
        task.result = {"recipe": p["recipe"]}

    def _run_generate(self, task: Task, handle: RuntimeHandle) -> None:
        p = task.params
        spec = GEN_MODELS[p["model"]]
        files = self._upload_inputs(task, handle, 0.0, 0.03) if task.uploads else []
        self._ensure(task, handle, ["comfyui"], 0.03, 0.07)
        task.log(f"{spec['name']} · {p['width']}×{p['height']}" + (f" · {p['seconds']} 秒" if p.get("seconds") else ""))
        args = {
            "model": p["model"],
            "mode": p["mode"],
            "prompt": p["prompt"],
            "width": p["width"],
            "height": p["height"],
            "seconds": p.get("seconds"),
            "seed": p.get("seed"),
            "fast": bool(p.get("fast", True)),
            "images": [f["path"] for f in files],
            "out_dir": self._work(handle, task, "out"),
            "_secrets": {"hf_token": self.hf_token()},
        }
        res = self._agent(task, handle, "generate", args, 0.1, 0.86)
        task.stage = "下载结果"
        for f in res.get("files", []):
            self._fetch(task, handle, f["path"], f["name"])
        task.result = {"seed": res.get("seed"), "template": res.get("template"), "gen_seconds": res.get("gen_seconds")}

    def hf_token(self) -> str | None:
        return self.secrets.get("hf-token")

    def set_hf_token(self, token: str | None) -> dict[str, Any]:
        self.secrets.set("hf-token", (token or "").strip() or None)
        return {"has_token": bool(self.hf_token())}

    def _run_chat(self, task: Task, handle: RuntimeHandle) -> None:
        model = task.params.get("model") or "qwen3:8b"
        self._ensure(task, handle, ["qwen"], 0.0, 0.6, {"qwen": model})
        res = self._agent(task, handle, "chat", {"prompt": task.params["prompt"], "model": task.params.get("model")}, 0.6, 0.4)
        task.result = {"reply": res.get("reply", ""), "model": res.get("model")}

    def _run_transcribe(self, task: Task, handle: RuntimeHandle) -> None:
        p = task.params
        model = p.get("model") or "large-v3-turbo"
        files = self._upload_inputs(task, handle, 0.0, 0.15)
        self._ensure(task, handle, ["whisper"], 0.15, 0.15, {"whisper": model})
        res = self._agent(
            task,
            handle,
            "transcribe",
            {
                "inputs": [{"path": f["path"], "name": f["name"]} for f in files],
                "out_dir": self._work(handle, task, "out"),
                "model": model,
                "language": p.get("language") or "auto",
                "vad": p.get("vad", True),
                "formats": p.get("formats") or ["srt", "txt"],
            },
            0.3,
            0.6,
        )
        task.stage = "下载结果"
        for item in res.get("items", []):
            for out in item.get("outputs", []):
                self._fetch(task, handle, out)
            task.items[item["name"]].update(status="done", detail=f"{item.get('language', '?')} · {round(item.get('duration', 0))} 秒")
        task.progress = 1.0

    def _tts_params(self, task: Task, handle: RuntimeHandle) -> dict[str, Any]:
        p = task.params
        engine = p.get("engine", "kokoro")
        args: dict[str, Any] = {"engine": engine, "lang": p.get("lang", "zh"), "speed": float(p.get("speed") or 1.0)}
        if engine == "kokoro":
            code = LANGS[args["lang"]]["kokoro"]
            args["voice"] = p.get("voice") or KOKORO_VOICES[code][0]["id"]
        return args

    def _run_tts(self, task: Task, handle: RuntimeHandle, preview: bool = False) -> None:
        p = task.params
        lines = [line.strip() for line in p["text"].splitlines() if line.strip()][: (1 if preview else 500)]
        args = self._tts_params(task, handle)
        ref_files = self._upload_inputs(task, handle, 0.0, 0.05) if task.uploads else []
        if ref_files:
            args["reference"] = {"path": ref_files[0]["path"], "start": 0, "duration": 15}
        self._ensure(task, handle, [args["engine"]], 0.05, 0.3)
        outs = [self._work(handle, task, "tts", f"{i + 1:04d}.wav") for i in range(len(lines))]
        args["items"] = [{"text": t, "out": o, "label": f"{i + 1}/{len(lines)}"} for i, (t, o) in enumerate(zip(lines, outs, strict=True))]
        self._agent(task, handle, "tts", args, 0.35, 0.5)
        task.stage = "下载结果"
        if preview:
            self._fetch(task, handle, outs[0], "preview.wav")
            return
        if p.get("merge") and len(outs) > 1:
            merged = self._work(handle, task, "tts", "all.wav")
            self._agent(task, handle, "concat", {"inputs": outs, "out": merged}, 0.85, 0.05)
            self._fetch(task, handle, merged, "全部合并.wav")
        for i, (text, out) in enumerate(zip(lines, outs, strict=True)):
            label = _SAFE.sub("_", text[:16]).strip("_") or "line"
            self._fetch(task, handle, out, f"{i + 1:04d}-{label}.wav")
            task.progress = 0.9 + 0.1 * (i + 1) / len(outs)

    def _fit_lines(
        self,
        task: Task,
        handle: RuntimeHandle,
        f: dict[str, Any],
        segs: list[dict],
        durations: dict[str, float],
        tts_args: dict[str, Any],
        translator: str,
        tgt: str,
        base: float,
        span: float,
        drift: float = 0.8,
    ) -> None:
        """Lines whose speech runs >15% past their slot get shortened (via the
        translator) or, without one, re-voiced faster; the renderer handles the rest."""
        long = [(s, r) for s, r in simulate_placement(segs, durations, f["duration"], drift=drift) if r > 1.15]
        if not long:
            return
        task.log(f"{f['name']}：{len(long)} 句配音偏长，自动精简后重新配音")
        shortened = False
        if translator in ("runtime", "api"):
            items = [{"id": s["id"], "text": s["text"], "max_chars": max(2, int(len(s["text"]) / r * 0.95))} for s, r in long]
            try:
                if translator == "api":
                    new = self.shorten_api(items, tgt)
                else:
                    res = self._agent(task, handle, "shorten", {"items": items, "lang": tgt}, base, span * 0.3)
                    new = {int(k): v for k, v in res.get("translations", {}).items()}
                for s, _ in long:
                    if new.get(s["id"]):
                        s["text"] = new[s["id"]].strip()
                shortened = bool(new)
            except Exception as err:  # keep going with the long lines
                task.log(f"精简失败，改为提速：{err}")
        redo = []
        base_speed = float(tts_args.get("speed") or 1.0)
        for s, r in long:
            item = {"text": s["text"], "out": s["tts"], "label": f"重配 {s['id']}"}
            if tts_args.get("engine") == "kokoro" and not shortened:
                item["speed"] = round(base_speed * min(1.25, r / 1.1), 2)
            redo.append(item)
        self._agent(task, handle, "tts", {**tts_args, "items": redo}, base + span * 0.3, span * 0.7)

    def _run_tts_preview(self, task: Task, handle: RuntimeHandle) -> None:
        self._run_tts(task, handle, preview=True)

    def _run_dub(self, task: Task, handle: RuntimeHandle) -> None:
        p = task.params
        src = p.get("src_lang") or "auto"
        tgt = p["tgt_lang"]
        translator = p.get("translator", "runtime")
        clone = p.get("voice_mode") == "clone"
        engine = "voxcpm" if clone else "kokoro"
        keep_bgm = bool(p.get("keep_bgm", True))
        separate = keep_bgm or clone
        same_lang = src == tgt
        needs = ["whisper", engine] + (["demucs"] if separate else []) + (["qwen"] if translator == "runtime" and not same_lang else [])

        files = self._upload_inputs(task, handle, 0.0, 0.08)
        resumed = self._load_dub_segments(p.get("resume_from"), [f["name"] for f in files])
        if resumed:
            needs.remove("whisper")
        self._ensure(task, handle, needs, 0.08, 0.22)

        info = {i["path"]: i for i in self._agent(task, handle, "probe", {"paths": [f["path"] for f in files]}, 0.3, 0.01)["items"]}
        for f in files:
            f.update(duration=info[f["path"]]["duration"], has_video=info[f["path"]]["has_video"])
            task.items[f["name"]]["detail"] = f"{round(f['duration'])} 秒" + (" · 视频" if f["has_video"] else " · 音频")

        stems: dict[str, dict[str, str]] = {}
        if separate:
            sep = self._agent(task, handle, "separate", {"inputs": [{"path": f["path"], "name": f["name"]} for f in files], "out_dir": self._work(handle, task, "sep")}, 0.31, 0.09)
            stems = {i["name"]: i for i in sep["items"]}

        if resumed:
            segs_by_item, langs = resumed
            task.log("沿用上次运行已识别、已翻译（含你的校对）的字幕，跳过识别和翻译")
            task.progress = 0.6
        else:
            segs_by_item, langs = self._dub_transcribe_translate(task, handle, files, stems, separate, src, tgt, translator, same_lang)

        self._dub_speak_render(task, handle, files, stems, segs_by_item, engine, clone, keep_bgm, separate, translator, same_lang, tgt)

    def _load_dub_segments(self, old_id: str | None, names: list[str]) -> tuple[dict, dict] | None:
        if not old_id or old_id not in self.tasks:
            return None
        path = Path(self.tasks[old_id].out_dir) / ".work" / "dub_segments.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not all(n in data.get("items", {}) for n in names):
            return None
        return {n: data["items"][n] for n in names}, {n: data.get("langs", {}).get(n) for n in names}

    def _dub_transcribe_translate(self, task: Task, handle: RuntimeHandle, files, stems, separate, src, tgt, translator, same_lang):
        p = task.params
        asr = self._agent(
            task,
            handle,
            "transcribe",
            {
                "inputs": [{"path": stems[f["name"]]["vocals"] if separate else f["path"], "name": f["name"]} for f in files],
                "out_dir": self._work(handle, task, "asr"),
                "model": p.get("asr_model") or "large-v3-turbo",
                "language": src,
                "vad": True,
                "formats": ["json"],
            },
            0.4,
            0.12,
        )
        segs_by_item: dict[str, list[dict]] = {}
        langs: dict[str, str] = {}
        for item in asr["items"]:
            data = self._fetch_json(task, handle, item["segments_path"])
            segs_by_item[item["name"]] = data["segments"]
            langs[item["name"]] = data.get("language") or src
            task.log(f"{item['name']}：识别到 {len(data['segments'])} 句（{data.get('language')}）")

        # translate
        task.stage = "翻译字幕"
        for n, name in enumerate(segs_by_item):
            segs = segs_by_item[name]
            source_lang = langs[name]
            todo = [{"id": s["id"], "text": s["text"], "seconds": s["end"] - s["start"]} for s in segs]
            if same_lang or translator == "none" or source_lang == tgt:
                trans = {s["id"]: s["text"] for s in segs}
            elif translator == "api":
                base = 0.52 + 0.08 * n / len(segs_by_item)
                trans = self.translate_api(todo, source_lang, tgt, lambda v, b=base: setattr(task, "progress", b + 0.08 * v / len(segs_by_item)))
            else:
                res = self._agent(task, handle, "translate", {"segments": todo, "src": source_lang, "tgt": tgt}, 0.52 + 0.08 * n / len(segs_by_item), 0.08 / len(segs_by_item))
                trans = {int(k): v for k, v in res["translations"].items()}
            for s in segs:
                s["source"] = s["text"]
                s["text"] = trans.get(s["id"]) or s["text"]
        task.progress = 0.6

        if p.get("review"):
            task.review = {"items": [{"name": name, "segments": segs} for name, segs in segs_by_item.items()]}
            task.status, task.stage = "waiting_review", "等待你校对译文"
            task.log("译文已生成，等待校对")
            task.review_event.wait()
            self._check(task)
            edits = task.review_edits or {}
            for name, segs in segs_by_item.items():
                changed = {int(e["id"]): e["text"] for e in edits.get(name, [])}
                for s in segs:
                    if s["id"] in changed:
                        s["text"] = changed[s["id"]].strip()
            task.status, task.review = "running", None

        # keep the transcript and the reviewed translation, so a retry can skip straight to speech
        work = Path(task.out_dir) / ".work"
        work.mkdir(parents=True, exist_ok=True)
        (work / "dub_segments.json").write_text(
            json.dumps({"items": {n: [{k: s[k] for k in ("id", "start", "end", "source", "text")} for s in segs] for n, segs in segs_by_item.items()}, "langs": langs}, ensure_ascii=False),
            encoding="utf-8",
        )
        return segs_by_item, langs

    def _dub_speak_render(self, task: Task, handle: RuntimeHandle, files, stems, segs_by_item, engine, clone, keep_bgm, separate, translator, same_lang, tgt):
        p = task.params
        # speech
        tts_base = 0.62
        for n, f in enumerate(files):
            name = f["name"]
            segs = segs_by_item[name]
            stem = Path(name).stem
            span = 0.22 / len(files)
            args = {"engine": engine, "lang": tgt, "speed": float(p.get("speed") or 1.0)}
            if clone:
                ref = self._agent(task, handle, "reference", {"segments": segs}, tts_base + span * n, 0.0)
                args["reference"] = {"path": stems[name]["vocals"], "start": ref["start"], "duration": ref["duration"]}
            else:
                args["voice"] = p.get("voice") or KOKORO_VOICES[LANGS[tgt]["kokoro"]][0]["id"]
            for s in segs:
                s["tts"] = self._work(handle, task, "tts", stem, f"{s['id']:05d}.wav")
            args["items"] = [{"text": s["text"], "out": s["tts"], "label": f"{name} {s['id']}/{len(segs)}"} for s in segs]
            task.items[name]["status"] = "speaking"
            res = self._agent(task, handle, "tts", args, tts_base + span * n, span * 0.75)
            durations = {i["out"]: i["duration"] for i in res.get("items", [])}
            self._fit_lines(task, handle, f, segs, durations, args, translator if not same_lang else "none", tgt, tts_base + span * (n + 0.75), span * 0.25)

        # render and download
        for n, f in enumerate(files):
            name = f["name"]
            task.items[name]["status"] = "rendering"
            res = self._agent(
                task,
                handle,
                "dub_render",
                {
                    "name": name,
                    "duration": f["duration"],
                    "segments": segs_by_item[name],
                    "accompaniment": stems[name]["accompaniment"] if keep_bgm and separate else None,
                    "video": f["path"] if f["has_video"] else None,
                    "out_dir": self._work(handle, task, "out"),
                    "max_tempo": float(p.get("max_tempo") or 1.35),
                    "bgm_volume": float(p.get("bgm_volume") or 0.9),
                },
                0.84 + 0.08 * n / len(files),
                0.08 / len(files),
            )
            task.stage = f"下载结果 · {name}"
            for out in res["outputs"]:
                self._fetch(task, handle, out)
            seg_file = Path(task.out_dir) / f"{Path(name).stem}.segments.json"
            seg_file.write_text(
                json.dumps([{k: s[k] for k in ("id", "start", "end", "source", "text")} for s in segs_by_item[name]], ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            task.outputs.append({"name": seg_file.name, "size": seg_file.stat().st_size, "kind": "data"})
            if res.get("sped_up") or res.get("trimmed"):
                task.log(f"{name}：{res.get('sped_up', 0)} 句加速对齐，{res.get('trimmed', 0)} 句超长被截断")
            task.items[name].update(status="done")
            task.progress = 0.92 + 0.08 * (n + 1) / len(files)
