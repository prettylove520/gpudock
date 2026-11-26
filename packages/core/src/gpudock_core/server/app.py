"""HTTP API for the GUI. Listens on 127.0.0.1 only and requires a per-launch token.

Two guards against other web pages on the same machine poking the API:
* every request's Host header must be the server's own 127.0.0.1/localhost origin
  (blocks DNS rebinding);
* every /api request must carry ``Authorization: Bearer <token>``; the token is
  only handed to the UI through the URL fragment printed at launch.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..agent.gpudock_agent import (
    GEN_MODELS,
    KOKORO_VOICES,
    LANGS,
    LLM_MODELS,
    MODE_LABELS,
    RECIPES,
)
from ..errors import AuthError, ColabApiError
from .state import Manager
from .tasks import TaskManager

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


class CreateRuntimeBody(BaseModel):
    accelerator: str
    shape: str = "SHAPE_STANDARD"
    auto_retry: bool = True


class ExecBody(BaseModel):
    code: str


class SettingsBody(BaseModel):
    auto_release_minutes: int | None = None
    release_on_exit: bool | None = None


class TaskBody(BaseModel):
    kind: str
    runtime: str  # a runtime id, or "auto" (with accelerator)
    params: dict[str, Any] = {}
    uploads: list[str] = []
    accelerator: str | None = None
    release_after: bool = False


class RetryBody(BaseModel):
    runtime: str | None = None
    overrides: dict[str, Any] = {}


class ReviewBody(BaseModel):
    edits: dict[str, list[dict[str, Any]]]


class HfTokenBody(BaseModel):
    token: str | None = None


class TranslatorBody(BaseModel):
    base_url: str = ""
    model: str = ""
    api_key: str | None = None


def create_app(
    manager: Manager,
    *,
    token: str,
    allowed_hosts: set[str],
    static_dir: Path | None = WEB_DIST,
    tasks: TaskManager | None = None,
) -> FastAPI:
    app = FastAPI(title="GPUDock", docs_url=None, redoc_url=None, openapi_url=None)
    tasks = tasks or TaskManager(manager)
    app.state.tasks = tasks
    expected = f"Bearer {token}"

    # Output files are served outside /api with a per-file signature, so <video>
    # and <img> can stream them (with Range) without an Authorization header.
    def sign(task_id: str, name: str) -> str:
        return hmac.new(token.encode(), f"{task_id}/{name}".encode(), hashlib.sha256).hexdigest()[:32]

    def with_urls(view: dict[str, Any]) -> dict[str, Any]:
        tid = view["id"]
        view["outputs"] = [
            {**o, "url": f"/files/{tid}/{quote(o['name'])}?sig={sign(tid, o['name'])}"} for o in view.get("outputs") or []
        ]
        return view

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"error": "Host not allowed", "code": "host"}, status_code=403)
        if request.url.path.startswith("/api/"):
            if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
                return JSONResponse({"error": "缺少或错误的访问令牌，请用 gpudock 命令打印的链接打开。", "code": "token"}, status_code=401)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AuthError)
    async def on_auth(_req: Request, err: AuthError):
        return JSONResponse({"error": str(err), "code": "login"}, status_code=401)

    @app.exception_handler(ColabApiError)
    async def on_colab(_req: Request, err: ColabApiError):
        if err.http_status in (401,) or err.status == "UNAUTHENTICATED":
            return JSONResponse({"error": str(err), "code": "login"}, status_code=401)
        return JSONResponse({"error": str(err), "code": "colab", "reason": err.reason, "status": err.status}, status_code=502)

    @app.exception_handler(PermissionError)
    async def on_perm(_req: Request, err: PermissionError):
        return JSONResponse({"error": str(err), "code": "forbidden"}, status_code=403)

    @app.exception_handler(LookupError)
    async def on_lookup(_req: Request, err: LookupError):
        return JSONResponse({"error": str(err), "code": "not_found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def on_value(_req: Request, err: ValueError):
        return JSONResponse({"error": str(err), "code": "bad_request"}, status_code=400)

    @app.exception_handler(RuntimeError)
    async def on_runtime(_req: Request, err: RuntimeError):
        return JSONResponse({"error": str(err), "code": "busy"}, status_code=409)

    # ---- auth ---------------------------------------------------------------
    @app.get("/api/auth/status")
    def auth_status() -> dict[str, Any]:
        return manager.auth_status()

    @app.post("/api/auth/login")
    def login() -> dict[str, Any]:
        return manager.start_login()

    @app.post("/api/auth/logout")
    def logout() -> dict[str, Any]:
        manager.logout()
        return {"ok": True}

    @app.get("/api/account")
    def account() -> dict[str, Any]:
        return manager.account()

    # ---- runtimes -----------------------------------------------------------
    @app.get("/api/specs")
    def specs() -> list[dict[str, Any]]:
        return manager.specs()

    @app.get("/api/runtimes")
    def runtimes() -> dict[str, Any]:
        return manager.runtimes()

    @app.post("/api/runtimes")
    def create_runtime(body: CreateRuntimeBody) -> dict[str, Any]:
        return manager.create_runtime(body.accelerator, body.shape, auto_retry=body.auto_retry).to_dict()

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        job = manager.jobs.get(job_id)
        if job is None:
            raise LookupError("没有这个任务")
        return job.to_dict()

    @app.delete("/api/jobs/{job_id}")
    def cancel_job(job_id: str) -> dict[str, Any]:
        job = manager.jobs.get(job_id)
        if job is None:
            raise LookupError("没有这个任务")
        job.cancelled = True
        return {"ok": True}

    @app.delete("/api/runtimes/{runtime_id}")
    def release(runtime_id: str, force: bool = False) -> dict[str, Any]:
        manager.release(runtime_id, force=force)
        return {"ok": True}

    @app.post("/api/runtimes/{runtime_id}/exec")
    def execute(runtime_id: str, body: ExecBody) -> dict[str, Any]:
        if not body.code.strip():
            raise ValueError("代码是空的")
        return manager.execute(runtime_id, body.code).view()

    @app.get("/api/execs/{exec_id}")
    def exec_view(exec_id: str, since: int = 0) -> dict[str, Any]:
        record = manager.execs.get(exec_id)
        if record is None:
            raise LookupError("没有这次执行记录")
        return record.view(since)

    @app.post("/api/runtimes/{runtime_id}/interrupt")
    def interrupt(runtime_id: str) -> dict[str, Any]:
        manager.interrupt(runtime_id)
        return {"ok": True}

    @app.get("/api/runtimes/{runtime_id}/stats")
    def stats(runtime_id: str) -> dict[str, Any]:
        return manager.stats(runtime_id)

    # ---- settings & events ----------------------------------------------------
    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return manager.settings.to_dict()

    @app.put("/api/settings")
    def put_settings(body: SettingsBody) -> dict[str, Any]:
        return manager.update_settings(auto_release_minutes=body.auto_release_minutes, release_on_exit=body.release_on_exit).to_dict()

    @app.get("/api/events")
    def events(since: int = 0) -> dict[str, Any]:
        return manager.events_since(since)

    # ---- models, uploads, tasks --------------------------------------------------
    @app.get("/api/catalog")
    def catalog() -> dict[str, Any]:
        return {
            "recipes": RECIPES,
            "langs": LANGS,
            "kokoro_voices": KOKORO_VOICES,
            "gen_models": GEN_MODELS,
            "mode_labels": MODE_LABELS,
            "llm_models": LLM_MODELS,
        }

    @app.get("/api/runtimes/{runtime_id}/agent")
    def agent_status(runtime_id: str, refresh: bool = False) -> dict[str, Any]:
        return tasks.runtime_status(runtime_id, refresh=refresh)

    @app.post("/api/uploads")
    def uploads(files: Annotated[list[UploadFile], File()]) -> list[dict[str, Any]]:
        return [tasks.uploads.save(f.filename or "file", f.file).view() for f in files]

    @app.post("/api/tasks")
    def create_task(body: TaskBody) -> dict[str, Any]:
        task = tasks.create(body.kind, body.runtime, body.params, body.uploads, accelerator=body.accelerator, release_after=body.release_after)
        return with_urls(task.detail())

    @app.get("/api/tasks")
    def list_tasks(active: bool = False, kind: str | None = None) -> list[dict[str, Any]]:
        return [with_urls(t) for t in tasks.list(active=active, kind=kind)]

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str, log_since: int = 0) -> dict[str, Any]:
        return with_urls(tasks.get(task_id).detail(log_since))

    @app.post("/api/tasks/{task_id}/retry")
    def retry_task(task_id: str, body: RetryBody) -> dict[str, Any]:
        return with_urls(tasks.retry(task_id, runtime=body.runtime, overrides=body.overrides).detail())

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str) -> dict[str, Any]:
        tasks.cancel(task_id)
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/review")
    def review_task(task_id: str, body: ReviewBody) -> dict[str, Any]:
        tasks.submit_review(task_id, body.edits)
        return {"ok": True}

    @app.get("/api/tasks/{task_id}/files/{name:path}")
    def task_file(task_id: str, name: str):
        path = tasks.file_path(task_id, name)
        return FileResponse(path, filename=path.name)

    @app.get("/files/{task_id}/{name:path}")
    def signed_file(task_id: str, name: str, sig: str = "", download: bool = False):
        if not hmac.compare_digest(sig, sign(task_id, name)):
            return JSONResponse({"error": "链接无效或已过期", "code": "token"}, status_code=403)
        path = tasks.file_path(task_id, name)
        return FileResponse(path, filename=path.name, content_disposition_type="attachment" if download else "inline")

    @app.post("/api/tasks/{task_id}/reveal")
    def reveal_task(task_id: str) -> dict[str, Any]:
        tasks.reveal(task_id)
        return {"ok": True}

    @app.get("/api/hf-token")
    def get_hf_token() -> dict[str, Any]:
        return {"has_token": bool(tasks.hf_token())}

    @app.put("/api/hf-token")
    def put_hf_token(body: HfTokenBody) -> dict[str, Any]:
        return tasks.set_hf_token(body.token)

    @app.get("/api/translator")
    def get_translator() -> dict[str, Any]:
        return tasks.translator()

    @app.put("/api/translator")
    def put_translator(body: TranslatorBody) -> dict[str, Any]:
        return tasks.set_translator(body.base_url, body.model, body.api_key)

    # ---- UI -------------------------------------------------------------------
    if static_dir is not None and (static_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    else:

        @app.get("/", response_class=HTMLResponse)
        def no_ui() -> str:
            return (
                "<meta charset='utf-8'><title>GPUDock</title>"
                "<p style='font:16px system-ui;margin:40px'>GPUDock 后端已启动，但前端还没有构建。<br>"
                "运行 <code>cd apps/web &amp;&amp; npm install &amp;&amp; npm run build</code> 后刷新。</p>"
            )

    return app
