"""GUI server end to end: FastAPI app + Manager, with a fake Colab API whose
runtimes are the local jupyter_server (see test_jupyter_integration)."""

import time

import pytest
from fastapi.testclient import TestClient

from gpudock_core.colab_api import RuntimeSpec, SpecKey
from gpudock_core.server.app import create_app
from gpudock_core.server.catalog import build_cards
from gpudock_core.server.state import Manager
from tests.test_experiments_local import FakeApi

pytestmark = pytest.mark.integration
TOKEN = "test-token"
A100 = SpecKey("VARIANT_GPU", "A100", "SHAPE_STANDARD")
A100_HM = SpecKey("VARIANT_GPU", "A100", "SHAPE_HIGHMEM")


class Api(FakeApi):
    def get_subscription(self):
        return "SUBSCRIPTION_TIER_PRO_PLUS"


@pytest.fixture
def env(jupyter, tmp_path, monkeypatch):
    monkeypatch.setenv("GPUDOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GPUDOCK_NO_KEYRING", "1")
    (tmp_path / "home").mkdir()
    url, _root = jupyter
    fake = Api(url)
    manager = Manager(config_dir=tmp_path / "home", api_factory=lambda: fake)
    app = create_app(manager, token=TOKEN, allowed_hosts={"testserver"}, static_dir=None)
    client = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
    yield client, manager, fake
    manager.stop()


def wait_for(fn, timeout=30, every=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(every)
    raise AssertionError("timed out")


def test_guard_requires_token_and_local_host(env):
    client, _, _ = env
    assert client.get("/api/auth/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/auth/status", headers={"host": "evil.example"}).status_code == 403
    assert client.get("/api/auth/status").status_code == 200
    # Without a built UI the root explains how to build it.
    assert "npm run build" in client.get("/").text


def test_auth_status_without_login_or_client(env):
    client, _, _ = env
    status = client.get("/api/auth/status").json()
    assert status["logged_in"] is False and status["client_ready"] is False
    assert "setup-client" in status["client_error"]


def test_login_runs_in_background_and_reports_url(env, monkeypatch):
    client, manager, _ = env

    def fake_login(on_url):
        on_url("https://accounts.google.com/o/oauth2/auth?x=1")
        time.sleep(0.3)

    manager._login_fn = fake_login
    state = client.post("/api/auth/login").json()
    assert state["status"] == "running"
    done = wait_for(lambda: (s := client.get("/api/auth/status").json()["login"])["status"] == "done" and s)
    assert done["url"].startswith("https://accounts.google.com/")


def test_specs_are_grouped_into_cards():
    cards = build_cards(
        [
            RuntimeSpec(SpecKey("VARIANT_GPU", "T4"), True),
            RuntimeSpec(A100_HM, False),
            RuntimeSpec(A100, True),
            RuntimeSpec(SpecKey("VARIANT_TPU", "V6E1"), True),
        ]
    )
    assert [c["accelerator"] for c in cards] == ["T4", "A100", "V6E1"]
    a100 = cards[1]
    assert [o["shape"] for o in a100["options"]] == ["SHAPE_STANDARD", "SHAPE_HIGHMEM"]
    assert a100["options"][1]["vram_gb"] == 80 and a100["options"][1]["eligible"] is False
    assert a100["eligible"] is True and cards[2]["kind"] == "tpu"


def test_full_runtime_lifecycle(env):
    client, manager, fake = env
    assert client.get("/api/account").json()["tier"] == "SUBSCRIPTION_TIER_PRO_PLUS"

    # The user's own notebook runtime is listed but not owned.
    listing = client.get("/api/runtimes").json()
    assert [r["owned"] for r in listing["runtimes"]] == [False]
    assert client.delete("/api/runtimes/users-own-notebook").status_code == 403

    # Ineligible spec is refused up front.
    assert client.post("/api/runtimes", json={"accelerator": "H100"}).status_code == 403

    job = client.post("/api/runtimes", json={"accelerator": "T4"}).json()
    done = wait_for(lambda: (j := client.get(f"/api/jobs/{job['id']}").json())["status"] != "running" and j)
    assert done["status"] == "done", done
    rid = done["result"]["id"]
    rt = next(r for r in client.get("/api/runtimes").json()["runtimes"] if r["id"] == rid)
    assert rt["owned"] and rt["display_name"] == "NVIDIA T4" and rt["auto_release_in_s"] > 0

    # Execute and stream output.
    ex = client.post(f"/api/runtimes/{rid}/exec", json={"code": "print('hi from gpudock')\n21 * 2"}).json()
    view = wait_for(lambda: (v := client.get(f"/api/execs/{ex['id']}").json())["status"] != "running" and v)
    text = "".join(c["text"] for c in view["chunks"])
    assert view["status"] == "ok" and "hi from gpudock" in text and "42" in text

    # A second exec while one runs is refused; interrupt stops the first.
    long = client.post(f"/api/runtimes/{rid}/exec", json={"code": "import time; time.sleep(30)"}).json()
    time.sleep(0.8)
    assert client.post(f"/api/runtimes/{rid}/exec", json={"code": "1"}).status_code == 409
    assert client.post(f"/api/runtimes/{rid}/interrupt").json() == {"ok": True}
    view = wait_for(lambda: (v := client.get(f"/api/execs/{long['id']}").json())["status"] != "running" and v)
    assert view["status"] == "error" and view["error"]["ename"] == "KeyboardInterrupt"

    # Variables persist in the console kernel across executions.
    ex = client.post(f"/api/runtimes/{rid}/exec", json={"code": "x = 5"}).json()
    wait_for(lambda: client.get(f"/api/execs/{ex['id']}").json()["status"] == "ok")
    ex = client.post(f"/api/runtimes/{rid}/exec", json={"code": "print(x + 1)"}).json()
    view = wait_for(lambda: (v := client.get(f"/api/execs/{ex['id']}").json())["status"] != "running" and v)
    assert "6" in "".join(c["text"] for c in view["chunks"])

    # Stats come from a separate monitor kernel; "gpu" appears only if this machine has one.
    stats = client.get(f"/api/runtimes/{rid}/stats").json()
    assert "disk" in stats
    if "gpu" in stats:
        assert {"name", "mem_used_mb", "mem_total_mb"} <= set(stats["gpu"])

    # Release.
    assert client.delete(f"/api/runtimes/{rid}").json() == {"ok": True}
    assert f"runtimes/{rid}" in fake.deleted
    assert rid not in [r["id"] for r in client.get("/api/runtimes").json()["runtimes"]]
    events = client.get("/api/events").json()["events"]
    assert [e["kind"] for e in events][-2:] == ["created", "released"]


def test_create_failure_is_reported_and_not_kept(env):
    client, manager, fake = env
    fake.refuse.add("L4")
    job = client.post("/api/runtimes", json={"accelerator": "L4"}).json()
    done = wait_for(lambda: (j := client.get(f"/api/jobs/{job['id']}").json())["status"] != "running" and j)
    assert done["status"] == "error" and "FAILED_PRECONDITION" in done["error"]
    assert manager.owned.all() == {}


def test_auto_release_only_touches_idle_owned_runtimes(env):
    client, manager, fake = env
    job = client.post("/api/runtimes", json={"accelerator": "T4"}).json()
    done = wait_for(lambda: (j := client.get(f"/api/jobs/{job['id']}").json())["status"] == "done" and j)
    name = done["result"]["name"]

    assert client.put("/api/settings", json={"auto_release_minutes": 99999}).status_code == 400
    assert client.put("/api/settings", json={"auto_release_minutes": 1}).json()["auto_release_minutes"] == 1
    assert manager.reap_once() == []  # just created: not idle yet

    manager.owned.update(name, last_activity=time.time() - 120)
    assert manager.reap_once() == [name]
    assert name in fake.deleted and "runtimes/users-own-notebook" in fake.live
    assert client.get("/api/events").json()["events"][-1]["kind"] == "auto_released"


def test_out_of_stock_is_queued_retried_and_cancellable(env):
    from gpudock_core.errors import ColabApiError

    client, manager, fake = env
    manager.retry_wait_s = 0.2
    original = fake.create_runtime
    calls = {"n": 0}

    def out_of_stock():
        return ColabApiError(None, 9, "FAILED_PRECONDITION", "CreateAssignment: generic::resource_exhausted", reason="NO_RUNTIMES")

    def flaky(spec, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise out_of_stock()
        return original(spec, **kw)

    fake.create_runtime = flaky
    job = client.post("/api/runtimes", json={"accelerator": "T4"}).json()
    done = wait_for(lambda: (j := client.get(f"/api/jobs/{job['id']}").json())["status"] != "running" and j)
    assert done["status"] == "done" and done["attempts"] == 3
    assert manager.owned.get(done["result"]["name"])["pending"] is False

    def never(spec, **kw):
        raise out_of_stock()

    fake.create_runtime = never
    job = client.post("/api/runtimes", json={"accelerator": "T4"}).json()
    wait_for(lambda: "排队" in client.get(f"/api/jobs/{job['id']}").json()["message"])
    assert client.delete(f"/api/jobs/{job['id']}").json() == {"ok": True}
    done = wait_for(lambda: (j := client.get(f"/api/jobs/{job['id']}").json())["status"] != "running" and j)
    assert done["status"] == "cancelled"
    assert all(not m.get("pending") for m in manager.owned.all().values())

    job = client.post("/api/runtimes", json={"accelerator": "T4", "auto_retry": False}).json()
    done = wait_for(lambda: (j := client.get(f"/api/jobs/{job['id']}").json())["status"] != "running" and j)
    assert done["status"] == "error" and done["attempts"] == 1


def test_spending_survives_release_and_exit_releases_the_rest(env):
    client, manager, fake = env
    names = []
    for _ in range(2):
        job = client.post("/api/runtimes", json={"accelerator": "T4"}).json()
        done = wait_for(lambda j=job: (d := client.get(f"/api/jobs/{j['id']}").json())["status"] == "done" and d)
        names.append(done["result"]["name"])
    # pretend the first one ran for an hour
    manager.owned.update(names[0], created_at=time.time() - 3600)
    client.delete(f"/api/runtimes/{names[0].split('/')[-1]}")
    usage = client.get("/api/runtimes").json()["usage"]
    assert usage["today_cu"] >= 1.19 or usage["month_cu"] >= 1.19

    assert manager.release_all_owned() == ["T4"]
    assert names[1] in fake.deleted and "runtimes/users-own-notebook" in fake.live
    assert client.put("/api/settings", json={"release_on_exit": False}).json()["release_on_exit"] is False
    assert Manager(config_dir=manager.config_dir, api_factory=lambda: fake).settings.release_on_exit is False


def test_pending_marks_from_a_previous_run_are_cleared(env):
    _, manager, fake = env
    manager.owned.add("runtimes/gpudock-left", label="L4", accelerator="L4", shape="SHAPE_STANDARD", pending=True)
    fresh = Manager(config_dir=manager.config_dir, api_factory=lambda: fake)
    assert fresh.owned.get("runtimes/gpudock-left")["pending"] is False


def test_events_and_console_output_are_capped():
    from gpudock_core.server.state import ExecRecord

    m = Manager.__new__(Manager)
    m.events, m.events_offset = [], 0
    for i in range(Manager.MAX_EVENTS + 100):
        m._event("info", f"e{i}")
    assert len(m.events) == Manager.MAX_EVENTS
    tail = m.events_since(Manager.MAX_EVENTS + 99)
    assert [e["message"] for e in tail["events"]] == [f"e{Manager.MAX_EVENTS + 99}"] and tail["next"] == Manager.MAX_EVENTS + 100

    rec = ExecRecord(id="x", runtime="r", code="")
    for i in range(ExecRecord.MAX_CHUNKS + 10):
        rec.add("stdout", str(i))
    view = rec.view(0)
    assert view["truncated"] and len(view["chunks"]) == ExecRecord.MAX_CHUNKS and view["next"] == ExecRecord.MAX_CHUNKS + 10
    assert rec.view(view["next"] - 1)["chunks"][0]["text"] == str(ExecRecord.MAX_CHUNKS + 9)


def test_cards_say_what_each_gpu_can_run():
    cards = build_cards([RuntimeSpec(SpecKey("VARIANT_GPU", "T4"), True), RuntimeSpec(A100, True)])
    t4, a100 = cards
    assert "转写" in t4["can_run"] and "Wan2.2 A14B" in t4["cannot_run"]
    assert "Wan2.2 A14B" in a100["can_run"] and a100["cannot_run"] == []
