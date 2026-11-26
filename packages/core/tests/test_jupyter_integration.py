"""Run the Jupyter channel and file transfer against a real local jupyter_server.

The Colab runtime is "a Jupyter server behind a proxy", so this exercises the
same REST + WebSocket protocol our client speaks to Colab, minus the proxy.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import respx

from gpudock_core.files import download, remote_sha256_code, sha256_file, upload
from gpudock_core.jupyter import (
    PROXY_TOKEN_HEADER,
    PROXY_TOKEN_PARAM,
    Kernel,
    RuntimeConnection,
)

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def jupyter(tmp_path_factory):
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    root = tmp_path_factory.mktemp("jroot")
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
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if httpx.get(url + "api/status", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("jupyter_server did not start")
    yield url, root
    proc.terminate()
    proc.wait(timeout=20)


@pytest.fixture
def conn(jupyter):
    url, _ = jupyter
    c = RuntimeConnection(url, "fake-proxy-token")
    yield c
    c.close()


@pytest.fixture
def kernel(conn):
    session_id, kernel_id = conn.create_session(name="pytest")
    k = Kernel(conn, kernel_id).connect()
    yield k
    k.close()
    conn.delete_session(session_id)


def test_status_and_sessions(conn):
    assert "started" in conn.status()
    sid, kid = conn.create_session(name="listing")
    try:
        assert any(s["id"] == sid for s in conn.list_sessions())
        assert any(k["id"] == kid for k in conn.list_kernels())
    finally:
        conn.delete_session(sid)


def test_ws_url_carries_proxy_token(conn):
    url = conn.ws_url("kid", "sess")
    assert url.startswith("ws://127.0.0.1:") and "/api/kernels/kid/channels?" in url
    assert f"{PROXY_TOKEN_PARAM}=fake-proxy-token" in url
    assert conn.headers()[PROXY_TOKEN_HEADER] == "fake-proxy-token"


def test_execute_stdout_result_and_error(kernel):
    seen = []
    res = kernel.execute("import sys\nprint('hi')\nprint('warn', file=sys.stderr)\n6 * 7", on_output=lambda k, t: seen.append(k))
    assert res.ok and res.stdout == "hi\n" and "warn" in res.stderr and res.result == "42"
    assert {"stdout", "stderr", "result"} <= set(seen)

    bad = kernel.execute("1 / 0")
    assert bad.status == "error" and bad.error["ename"] == "ZeroDivisionError"


def test_eval_json(kernel):
    assert kernel.eval_json("{'a': [1, 2], 'b': x}", setup="x = 'y'") == {"a": [1, 2], "b": "y"}


@pytest.mark.posix_runtime
def test_interrupt(conn, kernel):
    msg_id = kernel.send_execute("import time; time.sleep(30)")
    time.sleep(1.0)
    conn.interrupt(kernel.kernel_id)
    res = kernel.wait(msg_id, idle_timeout=20)
    assert res.error and res.error["ename"] == "KeyboardInterrupt"


def test_idle_timeout_raises_but_execution_continues(kernel):
    msg_id = kernel.send_execute("import time; time.sleep(2); print('done')")
    with pytest.raises(TimeoutError):
        kernel.wait(msg_id, idle_timeout=0.5)
    res = kernel.wait(msg_id, idle_timeout=10)
    assert "done" in res.stdout


def test_reconnect_resumes_inflight_execution(kernel):
    """Drop the socket mid-execution, reconnect with the same client session."""
    code = "import time, json\nfor i in range(12):\n    print(json.dumps({'i': i}), flush=True)\n    time.sleep(0.25)\n"
    beats = []

    def on_output(kind, text):
        for line in text.splitlines():
            beats.append(json.loads(line)["i"])

    msg_id = kernel.send_execute(code)
    with pytest.raises(TimeoutError):
        kernel.wait(msg_id, on_output=on_output, idle_timeout=5, total_timeout=0.8)
    kernel.close()
    time.sleep(1.2)  # outputs produced while nobody is connected
    kernel.connect()
    res = kernel.wait(msg_id, on_output=on_output, idle_timeout=10)
    assert res.ok
    # jupyter_server replays messages buffered for the same session while disconnected.
    assert sorted(set(beats)) == list(range(12)), beats


def test_upload_download_roundtrip_chunked_and_resume(jupyter, conn, kernel, tmp_path):
    _, root = jupyter
    src = tmp_path / "data.bin"
    src.write_bytes(os.urandom(300_000))
    progress = []
    st = upload(conn, src, "content/gpudock/data.bin", chunk_size=64 * 1024, on_progress=lambda d, t: progress.append(d))
    assert st.chunks == 5 and progress[-1] == 300_000
    assert sha256_file(Path(root) / "content/gpudock/data.bin") == sha256_file(src)

    kernel.execute(remote_sha256_code("content/gpudock/data.bin", root=str(root)))
    assert kernel.eval_json("_gpudock_sha_result") == sha256_file(src)

    small = tmp_path / "small.txt"
    small.write_text("hello 你好", encoding="utf-8")
    assert upload(conn, small, "/content/gpudock/small.txt").chunks == 1

    dst = tmp_path / "back.bin"
    full = download(conn, "content/gpudock/data.bin", dst)
    assert full.bytes == 300_000 and sha256_file(dst) == sha256_file(src)

    # Simulate an interrupted download: keep the first 100 KB as .part, resume.
    dst2 = tmp_path / "resume.bin"
    Path(str(dst2) + ".part").write_bytes(src.read_bytes()[:100_000])
    resumed = download(conn, "content/gpudock/data.bin", dst2)
    assert resumed.resumed_from == 100_000
    assert sha256_file(dst2) == sha256_file(src)


@respx.mock
def test_rest_refreshes_proxy_token_on_401():
    refreshed = []

    def refresh():
        refreshed.append(True)
        return "https://rt.example/base/", "new-token"

    route = respx.get("https://rt.example/base/api/status").mock(
        side_effect=lambda req: httpx.Response(200, json={"ok": 1})
        if req.headers[PROXY_TOKEN_HEADER] == "new-token"
        else httpx.Response(401)
    )
    c = RuntimeConnection("https://rt.example/base/", "old-token", refresh=refresh)
    assert c.status() == {"ok": 1}
    assert refreshed and route.call_count == 2 and c.token == "new-token"
