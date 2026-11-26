"""Run the GUI against a fake Colab account, for UI work without a Google login.

A local jupyter_server stands in for every runtime (as in the tests), the
account is "logged in" with a dummy token in a throwaway GPUDOCK_HOME, and
the spec list covers every card. Nothing here talks to Google.

    cd packages/core && uv run python scripts/demo_ui.py   # http://127.0.0.1:8765/#token=demo

Set GPUDOCK_DEMO_LOGGED_OUT=1 to see the login page, GPUDOCK_DEMO_PORT to change the port.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

PORT = int(os.environ.get("GPUDOCK_DEMO_PORT", "8765"))
TOKEN = "demo"

home = Path(tempfile.mkdtemp(prefix="gpudock-demo-"))
os.environ["GPUDOCK_HOME"] = str(home / "home")
os.environ["GPUDOCK_WORKSPACE"] = str(home / "outputs")
os.environ["GPUDOCK_NO_KEYRING"] = "1"
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
(home / "home").mkdir()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from gpudock_core.auth import TokenStore  # noqa: E402
from gpudock_core.colab_api import ConnectionInfo, Runtime, RuntimeSpec, SpecKey  # noqa: E402
from gpudock_core.server.app import create_app  # noqa: E402
from gpudock_core.server.state import Manager  # noqa: E402
from gpudock_core.server.tasks import TaskManager  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_jupyter(root: Path) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "jupyter_server",
            f"--ServerApp.port={port}", "--ServerApp.ip=127.0.0.1",
            "--IdentityProvider.token=", "--ServerApp.password=",
            "--ServerApp.disable_check_xsrf=True", "--ServerApp.open_browser=False",
            f"--ServerApp.root_dir={root}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/"
    for _ in range(200):
        try:
            if httpx.get(url + "api/status", timeout=2).status_code == 200:
                return proc, url
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    proc.kill()
    raise SystemExit("jupyter_server did not start")


def spec(accel: str, shape: str = "SHAPE_STANDARD", variant: str = "VARIANT_GPU") -> SpecKey:
    return SpecKey(variant, accel, shape)


class DemoApi:
    """The parts of ColabApi the GUI uses, backed by one local Jupyter server."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.live: dict[str, Runtime] = {}

    def _conn(self) -> ConnectionInfo:
        return ConnectionInfo(self.url, "tok", datetime.now(UTC) + timedelta(hours=1))

    def get_subscription(self) -> str:
        return "SUBSCRIPTION_TIER_PRO"

    def list_specs(self) -> list[RuntimeSpec]:
        return [
            RuntimeSpec(spec("T4"), True),
            RuntimeSpec(spec("T4", "SHAPE_HIGHMEM"), True),
            RuntimeSpec(spec("L4"), True),
            RuntimeSpec(spec("L4", "SHAPE_HIGHMEM"), True),
            RuntimeSpec(spec("A100"), True),
            RuntimeSpec(spec("A100", "SHAPE_HIGHMEM"), True),
            RuntimeSpec(spec("G4"), True),
            RuntimeSpec(spec("H100"), False),
            RuntimeSpec(spec("V5E1", variant="VARIANT_TPU"), True),
            RuntimeSpec(spec("NONE", variant="VARIANT_CPU"), True),
        ]

    def list_runtimes(self) -> list[Runtime]:
        return list(self.live.values())

    def get_runtime(self, name: str) -> Runtime:
        return Runtime(name, self.live[name].spec, None, self._conn())

    def create_runtime(self, spec, *, runtime_id=None, on_progress=None, **_):
        time.sleep(2)
        rt = Runtime(f"runtimes/{runtime_id}", spec, None, self._conn())
        self.live[rt.name] = rt
        return rt

    def delete_runtime(self, name, allow_missing=True):
        self.live.pop(name, None)

    def close(self) -> None:
        pass


def main() -> None:
    root = home / "runtime"
    root.mkdir()
    proc, url = start_jupyter(root)
    if not os.environ.get("GPUDOCK_DEMO_LOGGED_OUT"):
        TokenStore().save('{"refresh_token": "demo"}')
    api = DemoApi(url)
    manager = Manager(config_dir=home / "home", api_factory=lambda: api, runtime_fs_root=str(root))
    allowed = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
    app = create_app(manager, token=TOKEN, allowed_hosts=allowed, tasks=TaskManager(manager))
    print(f"GPUDock demo: http://127.0.0.1:{PORT}/#token={TOKEN}  (data in {home})", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    finally:
        manager.stop()
        proc.terminate()


if __name__ == "__main__":
    main()
