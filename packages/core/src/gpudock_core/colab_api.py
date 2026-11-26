"""Client for colaboratory.googleapis.com (v1beta runtimes + v1 operations).

Shapes follow the discovery documents (revision 20261123):

* ``GET  v1beta/runtimespecs``          -> {"runtimeSpecs": [{"key": Key, "eligible": bool}]}
* ``GET  v1beta/subscription``          -> {"name": "subscription", "tier": "SUBSCRIPTION_TIER_*"}
* ``GET  v1beta/runtimes``              -> {"runtimes": [Runtime]}
* ``GET  v1beta/runtimes/{id}``         -> Runtime (also refreshes connectionInfo.token)
* ``POST v1beta/runtimes?requestId&runtimeId`` body Runtime -> Operation
* ``DELETE v1beta/runtimes/{id}?allowMissing``
* ``GET  v1/operations/{id}:wait?timeout=60s`` -> Operation

Key = {"variant": VARIANT_CPU|GPU|TPU, "accelerator": "T4"|..., "shape": SHAPE_STANDARD|HIGHMEM}
"""

from __future__ import annotations

import random
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from .errors import ColabApiError

API_ROOT = "https://colaboratory.googleapis.com/"
RETRYABLE_HTTP = {429, 500, 502, 503, 504}
RETRYABLE_STATUS = {"RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL", "ABORTED"}

_FRACTION = re.compile(r"\.(\d{6})\d+")


def parse_time(value: str | None) -> datetime | None:
    """Parse RFC 3339 timestamps, including nanosecond fractions and 'Z'."""
    if not value:
        return None
    value = _FRACTION.sub(r".\1", value).replace("Z", "+00:00")
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class SpecKey:
    variant: str
    accelerator: str
    shape: str = "SHAPE_STANDARD"

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SpecKey:
        return cls(
            variant=data.get("variant", "VARIANT_UNSPECIFIED"),
            accelerator=data.get("accelerator", "NONE"),
            shape=data.get("shape", "SHAPE_STANDARD"),
        )

    def to_json(self) -> dict[str, str]:
        return {"variant": self.variant, "accelerator": self.accelerator, "shape": self.shape}

    @property
    def label(self) -> str:
        name = "CPU" if self.accelerator in ("", "NONE") else self.accelerator
        return f"{name} (HIGHMEM)" if self.shape == "SHAPE_HIGHMEM" else name

    @property
    def is_gpu(self) -> bool:
        return self.variant == "VARIANT_GPU"


@dataclass(frozen=True)
class RuntimeSpec:
    key: SpecKey
    eligible: bool

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RuntimeSpec:
        return cls(key=SpecKey.from_json(data.get("key") or {}), eligible=bool(data.get("eligible")))


@dataclass
class ConnectionInfo:
    url: str
    token: str = field(repr=False)
    expire_time: datetime | None

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> ConnectionInfo | None:
        if not data or not data.get("url"):
            return None
        return cls(url=data["url"], token=data.get("token", ""), expire_time=parse_time(data.get("expireTime")))

    def seconds_left(self, now: datetime | None = None) -> float | None:
        if self.expire_time is None:
            return None
        now = now or datetime.now(UTC)
        return (self.expire_time - now).total_seconds()


@dataclass
class Runtime:
    name: str
    spec: SpecKey | None
    version: str | None
    connection: ConnectionInfo | None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Runtime:
        spec = data.get("runtimeSpec")
        return cls(
            name=data.get("name", ""),
            spec=SpecKey.from_json(spec) if spec else None,
            version=data.get("version"),
            connection=ConnectionInfo.from_json(data.get("connectionInfo")),
        )

    @property
    def id(self) -> str:
        return self.name.split("/", 1)[-1]


class ColabApi:
    def __init__(
        self,
        auth_headers: Callable[[], dict[str, str]],
        *,
        root: str = API_ROOT,
        client: httpx.Client | None = None,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 90.0,
    ) -> None:
        self._auth_headers = auth_headers
        self._root = root if root.endswith("/") else root + "/"
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or self._new_client()
        self._max_retries = max_retries
        self._sleep = sleep

    def _new_client(self) -> httpx.Client:
        # short connect timeout: after the laptop sleeps or the network/proxy
        # changes, fail fast and reconnect instead of waiting out the read timeout
        return httpx.Client(timeout=httpx.Timeout(self._timeout, connect=15.0))

    def _reconnect(self) -> None:
        """Drop pooled connections (they may be dead after sleep / a proxy restart)."""
        if not self._owns_client:
            return
        old, self._client = self._client, self._new_client()
        try:
            old.close()
        except Exception:
            pass

    # ---- low level -------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        url = self._root + path
        attempt = 0
        while True:
            headers = {"Accept": "application/json", **self._auth_headers()}
            kwargs: dict[str, Any] = {"params": params, "headers": headers}
            if json is not None:
                kwargs["json"] = json
            if timeout is not None:
                kwargs["timeout"] = timeout
            try:
                resp = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise ColabApiError(None, None, "UNAVAILABLE", f"网络错误：{exc}") from exc
                self._reconnect()
                self._backoff(attempt)
                attempt += 1
                continue
            if resp.status_code < 400:
                return resp.json() if resp.content else {}
            err = self._parse_error(resp)
            if attempt < self._max_retries and (resp.status_code in RETRYABLE_HTTP or err.status in RETRYABLE_STATUS):
                self._backoff(attempt)
                attempt += 1
                continue
            raise err

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(60.0, (2**attempt)) + random.uniform(0, 1))

    @staticmethod
    def _parse_error(resp: httpx.Response) -> ColabApiError:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            return ColabApiError.from_status(body["error"], http_status=resp.status_code)
        return ColabApiError(resp.status_code, None, None, resp.text[:500] or resp.reason_phrase)

    # ---- API surface -----------------------------------------------------
    def get_subscription(self) -> str:
        return self._request("GET", "v1beta/subscription").get("tier", "SUBSCRIPTION_TIER_UNSPECIFIED")

    def list_specs(self) -> list[RuntimeSpec]:
        data = self._request("GET", "v1beta/runtimespecs")
        return [RuntimeSpec.from_json(s) for s in data.get("runtimeSpecs", [])]

    def list_runtimes(self) -> list[Runtime]:
        data = self._request("GET", "v1beta/runtimes")
        return [Runtime.from_json(r) for r in data.get("runtimes", [])]

    def get_runtime(self, name: str) -> Runtime:
        return Runtime.from_json(self._request("GET", f"v1beta/{_runtime_name(name)}"))

    def delete_runtime(self, name: str, *, allow_missing: bool = True) -> None:
        self._request(
            "DELETE",
            f"v1beta/{_runtime_name(name)}",
            params={"allowMissing": "true" if allow_missing else "false"},
        )

    def get_operation(self, name: str) -> dict[str, Any]:
        return self._request("GET", f"v1/{name}")

    def wait_operation(self, name: str, *, timeout_s: float = 900, poll_s: int = 60) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ColabApiError(None, 4, "DEADLINE_EXCEEDED", f"等待 {name} 超时（{timeout_s:.0f}s）")
            wait = max(1, min(poll_s, int(remaining)))
            op = self._request("GET", f"v1/{name}:wait", params={"timeout": f"{wait}s"}, timeout=wait + 30)
            if op.get("done"):
                return op

    def create_runtime(
        self,
        spec: SpecKey,
        *,
        runtime_id: str | None = None,
        request_id: str | None = None,
        version: str | None = None,
        timeout_s: float = 900,
        on_progress: Callable[[str], None] | None = None,
    ) -> Runtime:
        """Create a runtime and wait until it is ready.

        Retries with the same ``requestId`` on DEADLINE_EXCEEDED so the backend
        keeps waiting on the same assignment instead of creating a second one.
        """
        request_id = request_id or str(uuid.uuid4())
        params: dict[str, Any] = {"requestId": request_id}
        if runtime_id:
            params["runtimeId"] = runtime_id
        body: dict[str, Any] = {"runtimeSpec": spec.to_json()}
        if version:
            body["version"] = version

        deadline = time.monotonic() + timeout_s
        while True:
            try:
                op = self._request("POST", "v1beta/runtimes", params=params, json=body, timeout=180)
                break
            except ColabApiError as err:
                if err.status == "DEADLINE_EXCEEDED" and time.monotonic() < deadline:
                    if on_progress:
                        on_progress("仍在创建，继续等待…")
                    continue
                raise
        if not op.get("done"):
            if on_progress:
                on_progress(f"已提交（{op.get('name', '?')}），等待运行时就绪…")
            op = self.wait_operation(op["name"], timeout_s=max(1.0, deadline - time.monotonic()))
        if op.get("error"):
            raise ColabApiError.from_status(op["error"])
        runtime = Runtime.from_json(op.get("response") or {})
        if runtime.connection is None and runtime.name:
            runtime = self.get_runtime(runtime.name)
        return runtime

    def close(self) -> None:
        self._client.close()


def _runtime_name(name: str) -> str:
    return name if name.startswith("runtimes/") else f"runtimes/{name}"
