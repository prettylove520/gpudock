"""Talk to the Jupyter server inside a Colab runtime through the runtime proxy.

The Colab API hands out ``connectionInfo = {url, token, expireTime}``. Every
request to ``url`` carries ``X-Colab-Runtime-Proxy-Token: <token>``; the kernel
WebSocket additionally gets ``colab-runtime-proxy-token=<token>`` as a query
parameter (that is what Google's own CLI sends). Beyond that it is the standard
Jupyter Server REST API and the JSON (legacy, no subprotocol) kernel protocol.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from .errors import KernelError

PROXY_TOKEN_HEADER = "X-Colab-Runtime-Proxy-Token"
PROXY_TOKEN_PARAM = "colab-runtime-proxy-token"
JSON_SENTINEL = "__GPUDOCK_JSON__"

OutputCallback = Callable[[str, str], None]


class RuntimeConnection:
    """REST access to one runtime's Jupyter server."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        refresh: Callable[[], tuple[str, str]] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base = url.rstrip("/")
        self.token = token
        self._refresh = refresh
        self._extra = dict(extra_headers or {})
        self.client = client or httpx.Client(timeout=timeout)

    # ---- auth -------------------------------------------------------------
    def headers(self) -> dict[str, str]:
        return {PROXY_TOKEN_HEADER: self.token, **self._extra}

    def update_token(self, url: str, token: str) -> None:
        self.base = url.rstrip("/")
        self.token = token

    def refresh_token(self) -> bool:
        if self._refresh is None:
            return False
        url, token = self._refresh()
        self.update_token(url, token)
        return True

    # ---- REST -------------------------------------------------------------
    def url_for(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def request(self, method: str, path: str, *, retry_auth: bool = True, **kwargs: Any) -> httpx.Response:
        headers = {**self.headers(), **kwargs.pop("headers", {})}
        resp = self.client.request(method, self.url_for(path), headers=headers, **kwargs)
        # An expired/missing proxy token shows up at the proxy as 401/403, or as a
        # 404 that is not Jupyter's own JSON 404. Refresh once and retry.
        auth_failure = resp.status_code in (401, 403) or (
            resp.status_code == 404 and not _looks_like_jupyter(resp)
        )
        if auth_failure and retry_auth and self._refresh is not None:
            self.refresh_token()
            headers = {**headers, **self.headers()}
            resp = self.client.request(method, self.url_for(path), headers=headers, **kwargs)
        return resp

    def status(self) -> dict[str, Any]:
        resp = self.request("GET", "api/status")
        resp.raise_for_status()
        return resp.json()

    def list_kernels(self) -> list[dict[str, Any]]:
        resp = self.request("GET", "api/kernels")
        resp.raise_for_status()
        return resp.json()

    def list_sessions(self) -> list[dict[str, Any]]:
        resp = self.request("GET", "api/sessions")
        resp.raise_for_status()
        return resp.json()

    def create_session(self, name: str = "gpudock", kernel_name: str = "python3") -> tuple[str, str]:
        """Open a notebook session; returns (session_id, kernel_id)."""
        body = {
            "path": f"gpudock/{name}.ipynb",
            "name": f"{name}.ipynb",
            "type": "notebook",
            "kernel": {"name": kernel_name},
        }
        resp = self.request("POST", "api/sessions", json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["id"], data["kernel"]["id"]

    def delete_session(self, session_id: str) -> None:
        resp = self.request("DELETE", f"api/sessions/{session_id}")
        if resp.status_code not in (204, 404):
            resp.raise_for_status()

    def interrupt(self, kernel_id: str) -> None:
        resp = self.request("POST", f"api/kernels/{kernel_id}/interrupt")
        resp.raise_for_status()

    def ws_url(self, kernel_id: str, client_session: str) -> str:
        base = self.base
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        query = urlencode({"session_id": client_session, PROXY_TOKEN_PARAM: self.token})
        return f"{base}/api/kernels/{quote(kernel_id)}/channels?{query}"

    def close(self) -> None:
        self.client.close()


def _looks_like_jupyter(resp: httpx.Response) -> bool:
    """Jupyter's own 404s are JSON with a 'message'; the proxy's are not."""
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "message" in body


@dataclass
class ExecResult:
    msg_id: str
    status: str = "unknown"  # ok | error | aborted | unknown
    stdout: str = ""
    stderr: str = ""
    result: str | None = None
    displays: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    started: float = 0.0
    finished: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def elapsed(self) -> float:
        return self.finished - self.started

    def json_lines(self) -> list[Any]:
        """Objects printed with the JSON sentinel (see ``emit_json_snippet``)."""
        out = []
        for line in self.stdout.splitlines():
            if line.startswith(JSON_SENTINEL):
                out.append(json.loads(line[len(JSON_SENTINEL) :]))
        return out

    def error_text(self) -> str:
        if not self.error:
            return self.stderr
        return f"{self.error.get('ename')}: {self.error.get('evalue')}"


def emit_json_snippet(expr: str) -> str:
    """Python code that prints ``expr`` as one sentinel-prefixed JSON line."""
    return f"import json as _j; print({JSON_SENTINEL!r} + _j.dumps({expr}, default=str))"


class Kernel:
    """A WebSocket connection to one kernel, JSON protocol."""

    def __init__(
        self,
        conn: RuntimeConnection,
        kernel_id: str,
        *,
        client_session: str | None = None,
        open_timeout: float = 60.0,
        username: str = "gpudock",
    ) -> None:
        self.conn = conn
        self.kernel_id = kernel_id
        self.client_session = client_session or uuid.uuid4().hex
        self.open_timeout = open_timeout
        self.username = username
        self.ws: ClientConnection | None = None
        self._stack: contextlib.ExitStack | None = None
        self.connected_at: float | None = None

    # ---- connection -------------------------------------------------------
    def connect(self) -> Kernel:
        self.close()
        # websockets wants connect() used as a context manager; hold it open in
        # an ExitStack so the connection can outlive this call.
        stack = contextlib.ExitStack()
        self.ws = stack.enter_context(
            connect(
                self.conn.ws_url(self.kernel_id, self.client_session),
                additional_headers=self.conn.headers(),
                open_timeout=self.open_timeout,
                max_size=None,
                ping_interval=20,
                ping_timeout=60,
            )
        )
        self._stack = stack
        self.connected_at = time.monotonic()
        return self

    def reconnect(self, *, refresh_token: bool = True) -> Kernel:
        if refresh_token:
            self.conn.refresh_token()
        return self.connect()

    def close(self) -> None:
        if self._stack is not None:
            with contextlib.suppress(Exception):
                self._stack.close()
            self._stack = None
        self.ws = None

    def __enter__(self) -> Kernel:
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- messaging --------------------------------------------------------
    def _message(self, msg_type: str, content: dict[str, Any], channel: str = "shell") -> dict[str, Any]:
        return {
            "header": {
                "msg_id": uuid.uuid4().hex,
                "username": self.username,
                "session": self.client_session,
                "msg_type": msg_type,
                "version": "5.3",
                "date": datetime.now(UTC).isoformat(),
            },
            "parent_header": {},
            "metadata": {},
            "content": content,
            "channel": channel,
            "buffers": [],
        }

    def send_execute(self, code: str, *, silent: bool = False) -> str:
        if self.ws is None:
            self.connect()
        msg = self._message(
            "execute_request",
            {
                "code": code,
                "silent": silent,
                "store_history": False,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
        )
        assert self.ws is not None
        self.ws.send(json.dumps(msg))
        return msg["header"]["msg_id"]

    def wait(
        self,
        msg_id: str,
        *,
        on_output: OutputCallback | None = None,
        idle_timeout: float | None = 120.0,
        total_timeout: float | None = None,
        result: ExecResult | None = None,
    ) -> ExecResult:
        """Collect output for ``msg_id`` until the kernel reports it finished.

        ``idle_timeout`` is the longest silence we accept (None = forever).
        Raises ``TimeoutError`` on silence (the code may still be running),
        ``KernelError`` if the kernel dies, ``ConnectionClosed`` if the socket drops.
        """
        res = result or ExecResult(msg_id=msg_id, started=time.monotonic())
        deadline = time.monotonic() + total_timeout if total_timeout else None
        got_reply = False
        got_idle = False
        reply_at: float | None = None
        assert self.ws is not None, "call connect() first"
        while True:
            wait_for = idle_timeout
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"执行超过 {total_timeout}s")
                wait_for = left if wait_for is None else min(wait_for, left)
            if got_reply and not got_idle:
                # Some proxies drop the trailing idle status; don't hang on it.
                grace = 5.0 - (time.monotonic() - (reply_at or time.monotonic()))
                if grace <= 0:
                    break
                wait_for = grace if wait_for is None else min(wait_for, grace)
            try:
                raw = self.ws.recv(timeout=wait_for)
            except TimeoutError:
                if got_reply:
                    break
                raise TimeoutError(f"内核 {idle_timeout}s 没有任何输出") from None
            if isinstance(raw, bytes):
                continue  # binary frames are not used by the JSON protocol
            msg = json.loads(raw)
            msg_type = msg.get("header", {}).get("msg_type") or msg.get("msg_type")
            content = msg.get("content") or {}
            parent = (msg.get("parent_header") or {}).get("msg_id")
            if msg_type == "status" and content.get("execution_state") in ("dead", "restarting"):
                raise KernelError(f"内核状态变为 {content.get('execution_state')}")
            if parent != msg_id:
                continue
            if msg_type == "stream":
                text = content.get("text", "")
                if content.get("name") == "stderr":
                    res.stderr += text
                    kind = "stderr"
                else:
                    res.stdout += text
                    kind = "stdout"
                if on_output:
                    on_output(kind, text)
            elif msg_type == "execute_result":
                res.result = (content.get("data") or {}).get("text/plain")
                if on_output and res.result is not None:
                    on_output("result", res.result)
            elif msg_type == "display_data":
                text = (content.get("data") or {}).get("text/plain", "")
                res.displays.append(text)
                if on_output:
                    on_output("display", text)
            elif msg_type == "error":
                res.error = {
                    "ename": content.get("ename"),
                    "evalue": content.get("evalue"),
                    "traceback": content.get("traceback", []),
                }
                if on_output:
                    on_output("error", f"{content.get('ename')}: {content.get('evalue')}")
            elif msg_type == "execute_reply":
                res.status = content.get("status", "unknown")
                if content.get("status") == "error" and res.error is None:
                    res.error = {"ename": content.get("ename"), "evalue": content.get("evalue"), "traceback": content.get("traceback", [])}
                got_reply = True
                reply_at = time.monotonic()
            elif msg_type == "status" and content.get("execution_state") == "idle":
                got_idle = True
            if got_reply and got_idle:
                break
        res.finished = time.monotonic()
        return res

    def execute(
        self,
        code: str,
        *,
        on_output: OutputCallback | None = None,
        idle_timeout: float | None = 120.0,
        total_timeout: float | None = None,
    ) -> ExecResult:
        started = time.monotonic()
        msg_id = self.send_execute(code)
        return self.wait(
            msg_id,
            on_output=on_output,
            idle_timeout=idle_timeout,
            total_timeout=total_timeout,
            result=ExecResult(msg_id=msg_id, started=started),
        )

    def eval_json(self, expr: str, *, setup: str = "", idle_timeout: float | None = 120.0) -> Any:
        """Run ``setup`` then return ``expr`` evaluated in the kernel, via JSON."""
        code = (setup + "\n" if setup else "") + emit_json_snippet(expr)
        res = self.execute(code, idle_timeout=idle_timeout)
        if not res.ok:
            raise KernelError(f"执行失败：{res.error_text()}")
        lines = res.json_lines()
        if not lines:
            raise KernelError("内核没有返回 JSON 结果")
        return lines[-1]


__all__ = [
    "ConnectionClosed",
    "ExecResult",
    "Kernel",
    "RuntimeConnection",
    "emit_json_snippet",
]
