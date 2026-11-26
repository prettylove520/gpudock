"""Batch tasks end to end against a local jupyter_server running the agent in
fake-model mode (GPUDOCK_AGENT_FAKE=1): real uploads, real ffmpeg, real
mixing/muxing, stand-in models."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from gpudock_core.server.app import create_app
from gpudock_core.server.state import Manager
from gpudock_core.server.tasks import TaskManager
from tests.test_experiments_local import FakeApi

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg"),
]
TOKEN = "t"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def fake_runtime(tmp_path_factory):
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    root = tmp_path_factory.mktemp("rt")
    port = _free_port()
    env = dict(os.environ, GPUDOCK_AGENT_FAKE="1")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "jupyter_server", f"--ServerApp.port={port}", "--ServerApp.ip=127.0.0.1",
            "--IdentityProvider.token=", "--ServerApp.password=", "--ServerApp.disable_check_xsrf=True",
            "--ServerApp.open_browser=False", f"--ServerApp.root_dir={root}",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    url = f"http://127.0.0.1:{port}/"
    for _ in range(200):
        try:
            if httpx.get(url + "api/status", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.3)
    yield url, root
    proc.terminate()
    proc.wait(timeout=20)


class Api(FakeApi):
    def get_subscription(self):
        return "SUBSCRIPTION_TIER_PRO"


@pytest.fixture
def env(fake_runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("GPUDOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GPUDOCK_NO_KEYRING", "1")
    (tmp_path / "home").mkdir()
    url, root = fake_runtime
    fake = Api(url)
    manager = Manager(config_dir=tmp_path / "home", api_factory=lambda: fake, runtime_fs_root=str(root))
    tasks = TaskManager(manager, root=tmp_path / "ws")
    app = create_app(manager, token=TOKEN, allowed_hosts={"testserver"}, static_dir=None, tasks=tasks)
    client = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})
    rid = fake.user_runtime.id
    yield client, tasks, rid, tmp_path
    manager.stop()


def media(path, *, video: bool, seconds: int = 6):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}"]
    if video:
        cmd += ["-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    cmd += ["-shortest", str(path)]
    subprocess.run(cmd, check=True)
    return path


def upload(client, *paths):
    files = [("files", (p.name, p.read_bytes())) for p in paths]
    r = client.post("/api/uploads", files=files)
    assert r.status_code == 200, r.text
    return [u["id"] for u in r.json()]


def wait_task(client, task_id, until=("done", "error", "cancelled"), timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = client.get(f"/api/tasks/{task_id}").json()
        if t["status"] in until:
            return t
        time.sleep(0.3)
    raise AssertionError(f"task stuck: {t['status']} {t['stage']} {t['logs'][-5:]}")


def duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], capture_output=True, text=True)
    return float(json.loads(out.stdout)["format"]["duration"])


def test_catalog_and_agent_status(env):
    client, _, rid, _ = env
    cat = client.get("/api/catalog").json()
    assert {"whisper", "demucs", "kokoro", "voxcpm", "qwen"} <= set(cat["recipes"])
    assert cat["kokoro_voices"]["z"][0]["id"].startswith("zf_")
    status = client.get(f"/api/runtimes/{rid}/agent?refresh=true").json()
    assert status["agent"] and status["ffmpeg"] is True


def test_install_marks_recipe(env):
    client, _, rid, _ = env
    t = client.post("/api/tasks", json={"kind": "install", "runtime": rid, "params": {"recipe": "kokoro"}}).json()
    done = wait_task(client, t["id"])
    assert done["status"] == "done", done
    assert "kokoro" in client.get(f"/api/runtimes/{rid}/agent?refresh=true").json()["installed"]


@pytest.mark.posix_runtime
def test_batch_transcribe(env):
    client, tasks, rid, tmp = env
    ids = upload(client, media(tmp / "talk one.wav", video=False), media(tmp / "talk2.mp4", video=True))
    t = client.post(
        "/api/tasks", json={"kind": "transcribe", "runtime": rid, "params": {"language": "zh", "formats": ["srt", "txt", "vtt"]}, "uploads": ids}
    ).json()
    done = wait_task(client, t["id"])
    assert done["status"] == "done", done["logs"][-10:]
    names = sorted(o["name"] for o in done["outputs"])
    assert names == ["talk2.srt", "talk2.txt", "talk2.vtt", "talk_one.srt", "talk_one.txt", "talk_one.vtt"]
    srt = client.get(f"/api/tasks/{t['id']}/files/talk2.srt")
    assert srt.status_code == 200 and "00:00:00,000 --> 00:00:02,000" in srt.text
    assert all(i["status"] == "done" for i in done["items"])
    assert client.get(f"/api/tasks/{t['id']}/files/../../secret").status_code == 404


def test_batch_tts_with_merge(env):
    client, _, rid, _ = env
    t = client.post(
        "/api/tasks",
        json={"kind": "tts", "runtime": rid, "params": {"engine": "kokoro", "lang": "zh", "voice": "zf_xiaobei", "text": "第一句\n\n第二句话\n第三句", "merge": True}},
    ).json()
    done = wait_task(client, t["id"])
    assert done["status"] == "done", done["logs"][-10:]
    names = [o["name"] for o in done["outputs"]]
    assert names[0] == "全部合并.wav" and len(names) == 4 and names[1].startswith("0001-")


def test_voice_cloning_requires_consent(env):
    client, _, rid, tmp = env
    ids = upload(client, media(tmp / "ref.wav", video=False, seconds=3))
    r = client.post("/api/tasks", json={"kind": "tts", "runtime": rid, "params": {"engine": "voxcpm", "lang": "en", "text": "hi"}, "uploads": ids})
    assert r.status_code == 400 and "授权" in r.json()["error"]
    r = client.post("/api/tasks", json={"kind": "dub", "runtime": rid, "params": {"tgt_lang": "en", "voice_mode": "clone"}, "uploads": ids})
    assert r.status_code == 400 and "授权" in r.json()["error"]


@pytest.mark.posix_runtime
def test_dub_video_with_review(env):
    client, tasks, rid, tmp = env
    ids = upload(client, media(tmp / "clip.mp4", video=True, seconds=6))
    params = {"src_lang": "zh", "tgt_lang": "en", "translator": "runtime", "voice_mode": "preset", "voice": "af_heart", "keep_bgm": True, "review": True}
    t = client.post("/api/tasks", json={"kind": "dub", "runtime": rid, "params": params, "uploads": ids}).json()
    waiting = wait_task(client, t["id"], until=("waiting_review", "error"))
    assert waiting["status"] == "waiting_review", waiting["logs"][-10:]
    segs = waiting["review"]["items"][0]["segments"]
    assert segs[0]["text"].startswith("[en]") and segs[0]["source"].startswith("第1句")
    r = client.post(f"/api/tasks/{t['id']}/review", json={"edits": {"clip.mp4": [{"id": 1, "text": "Hello there, edited."}]}})
    assert r.status_code == 200
    done = wait_task(client, t["id"])
    assert done["status"] == "done", done["logs"][-10:]
    names = {o["name"] for o in done["outputs"]}
    assert {"clip.dubbed.mp4", "clip.dubbed.wav", "clip.dubbed.srt", "clip.bilingual.srt", "clip.segments.json"} <= names
    out = tasks.file_path(t["id"], "clip.dubbed.mp4")
    assert abs(duration(out) - 6.0) < 0.6
    srt = client.get(f"/api/tasks/{t['id']}/files/clip.dubbed.srt").text
    assert "Hello there, edited." in srt
    bi = client.get(f"/api/tasks/{t['id']}/files/clip.bilingual.srt").text
    assert "Hello there, edited.\n第1句测试内容" in bi


@pytest.mark.posix_runtime
def test_dub_cancel_while_waiting_for_review(env):
    client, _, rid, tmp = env
    ids = upload(client, media(tmp / "a.wav", video=False, seconds=4))
    params = {"src_lang": "zh", "tgt_lang": "zh", "voice_mode": "preset", "keep_bgm": False, "review": True}
    t = client.post("/api/tasks", json={"kind": "dub", "runtime": rid, "params": params, "uploads": ids}).json()
    wait_task(client, t["id"], until=("waiting_review", "error"))
    client.post(f"/api/tasks/{t['id']}/cancel")
    done = wait_task(client, t["id"])
    assert done["status"] == "cancelled"
    assert any(x["id"] == t["id"] for x in client.get("/api/tasks").json())


@respx.mock
def test_external_translator(env):
    client, tasks, _, _ = env
    assert client.put("/api/translator", json={"base_url": "ftp://x", "model": "m"}).status_code == 400
    cfg = client.put("/api/translator", json={"base_url": "https://api.example.com/v1/", "model": "deepseek-chat", "api_key": "sk-test"}).json()
    assert cfg == {"base_url": "https://api.example.com/v1", "model": "deepseek-chat", "has_key": True}
    route = respx.post("https://api.example.com/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": '```json\n{"translations": [{"id": 1, "text": "你好"}, {"id": 2, "text": "世界"}]}\n```'}}]}
    )
    out = tasks.translate_api([{"id": 1, "text": "hello", "seconds": 1}, {"id": 2, "text": "world", "seconds": 1}], "en", "zh")
    assert out == {1: "你好", 2: "世界"}
    sent = route.calls[0].request
    assert sent.headers["authorization"] == "Bearer sk-test"
    assert "中文" in json.loads(sent.content)["messages"][0]["content"]
    assert "sk-test" not in client.get("/api/translator").text


@pytest.mark.posix_runtime
def test_dub_shortens_lines_that_do_not_fit(env):
    client, tasks, rid, tmp = env
    ids = upload(client, media(tmp / "long.mp4", video=True, seconds=6))
    params = {"src_lang": "zh", "tgt_lang": "en", "translator": "runtime", "voice_mode": "preset", "keep_bgm": False, "review": True}
    t = client.post("/api/tasks", json={"kind": "dub", "runtime": rid, "params": params, "uploads": ids}).json()
    wait_task(client, t["id"], until=("waiting_review", "error"))
    long_line = "This line is written to be far too long for its two second slot in the video."
    client.post(f"/api/tasks/{t['id']}/review", json={"edits": {"long.mp4": [{"id": 1, "text": long_line}]}})
    done = wait_task(client, t["id"])
    assert done["status"] == "done", done["logs"][-10:]
    assert any("配音偏长" in line for line in done["logs"])
    segs = json.loads(tasks.file_path(t["id"], "long.segments.json").read_text())
    first = next(s for s in segs if s["id"] == 1)
    assert len(first["text"]) < len(long_line)  # shortened by the (fake) LLM pass
    assert abs(duration(tasks.file_path(t["id"], "long.dubbed.mp4")) - 6.0) < 0.6


@respx.mock
def test_external_api_shortening(env):
    client, tasks, _, _ = env
    client.put("/api/translator", json={"base_url": "https://api.example.com/v1", "model": "m", "api_key": "sk-x"})
    route = respx.post("https://api.example.com/v1/chat/completions").respond(
        json={"choices": [{"message": {"content": '{"translations": [{"id": 3, "text": "短句。"}]}'}}]}
    )
    assert tasks.shorten_api([{"id": 3, "text": "一句很长很长的话。", "max_chars": 4}], "zh") == {3: "短句。"}
    assert "max_chars" in json.loads(route.calls[0].request.content)["messages"][1]["content"]


def test_voice_cloning_refuses_small_runtimes(env):
    client, _, rid, _ = env  # the fake runtime is a standard T4
    r = client.post("/api/tasks", json={"kind": "tts", "runtime": rid, "params": {"engine": "voxcpm", "lang": "zh", "text": "hi", "consent": True}})
    assert r.status_code == 400 and "内存" in r.json()["error"]
    r = client.post("/api/tasks", json={"kind": "install", "runtime": rid, "params": {"recipe": "voxcpm"}})
    assert r.status_code == 400


def _png(path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=orange:s=64x64", "-frames:v", "1", str(path)], check=True)
    return path


@pytest.mark.posix_runtime
def test_generate_image_and_video(env):
    client, _, rid, tmp = env
    t = client.post(
        "/api/tasks",
        json={"kind": "generate", "runtime": rid, "params": {"model": "z_image_turbo", "mode": "t2i", "prompt": "一只橘猫", "width": 1024, "height": 1024, "seed": 7}},
    ).json()
    done = wait_task(client, t["id"])
    assert done["status"] == "done", done["logs"][-10:]
    assert [o["kind"] for o in done["outputs"]] == ["image"] and done["outputs"][0]["name"].endswith("-7.png")
    assert done["result"]["seed"] == 7 and done["result"]["template"] == "image_z_image_turbo"

    ids = upload(client, _png(tmp / "first.png"))
    t = client.post(
        "/api/tasks",
        json={
            "kind": "generate", "runtime": rid, "uploads": ids,
            "params": {"model": "wan22_5b", "mode": "i2v", "prompt": "镜头缓缓推进", "width": 1280, "height": 704, "seconds": 3},
        },
    ).json()
    done = wait_task(client, t["id"])
    assert done["status"] == "done", done["logs"][-10:]
    assert done["outputs"][0]["kind"] == "video" and isinstance(done["result"]["seed"], int)


def test_generate_validation(env):
    client, _, rid, tmp = env

    def create(params, uploads=()):
        return client.post("/api/tasks", json={"kind": "generate", "runtime": rid, "params": params, "uploads": list(uploads)})

    base = {"model": "wan22_14b", "prompt": "x", "width": 1280, "height": 720, "seconds": 3}
    assert "图片" in create({**base, "mode": "i2v"}).json()["error"]
    one = upload(client, _png(tmp / "a.png"))
    assert "2 张" in create({**base, "mode": "flf2v"}, one).json()["error"]
    assert create({**base, "mode": "edit"}).status_code == 400
    assert "秒" in create({**base, "mode": "t2v", "seconds": 30}).json()["error"]
    assert "16 的倍数" in create({**base, "mode": "t2v", "width": 1000}).json()["error"]
    assert create({**base, "model": "nope", "mode": "t2v"}).status_code == 400
    r = create({"model": "ltx25", "mode": "t2v", "prompt": "x", "width": 1280, "height": 704, "seconds": 5})
    assert r.status_code == 400 and "Hugging Face" in r.json()["error"]


@pytest.mark.posix_runtime
def test_generate_keeps_hf_token_off_disk(env, fake_runtime):
    client, _, rid, _ = env
    _, root = fake_runtime
    secret = "hf_SECRET_test_value_123"
    assert client.put("/api/hf-token", json={"token": secret}).json() == {"has_token": True}
    t = client.post(
        "/api/tasks",
        json={"kind": "generate", "runtime": rid, "params": {"model": "ltx25", "mode": "t2v", "prompt": "海浪", "width": 1280, "height": 704, "seconds": 5}},
    ).json()
    done = wait_task(client, t["id"])
    assert done["status"] == "done", done["logs"][-10:]
    leaked = [p for p in root.rglob("*") if p.is_file() and secret.encode() in p.read_bytes()]
    assert leaked == []
    assert secret not in json.dumps(client.get("/api/tasks").json())
    client.put("/api/hf-token", json={"token": ""})


@pytest.mark.posix_runtime
def test_auto_runtime_is_shared_and_released_after_the_last_task(env):
    client, tasks, _, tmp = env
    fake = tasks.m.api
    ids = upload(client, media(tmp / "talk.wav", video=False, seconds=3))
    body = {"kind": "transcribe", "runtime": "auto", "accelerator": "L4", "release_after": True, "params": {"formats": ["srt"]}, "uploads": ids}
    first = client.post("/api/tasks", json=body).json()
    second = client.post("/api/tasks", json=body).json()
    a, b = wait_task(client, first["id"]), wait_task(client, second["id"])
    assert a["status"] == b["status"] == "done", (a["logs"][-6:], b["logs"][-6:])
    assert a["runtime"] == b["runtime"] and a["runtime"].startswith("runtimes/gpudock-")
    assert a["accelerator"] == "L4" and a["est_cu"] is not None
    # released once, after the second task; the user's own runtime is never touched
    wait_for_release = time.time() + 10
    while a["runtime"] not in fake.deleted and time.time() < wait_for_release:
        time.sleep(0.2)
    assert fake.deleted.count(a["runtime"]) == 1
    assert "runtimes/users-own-notebook" in fake.live
    later = [t for t in (a, b) if any("释放" in line for line in t["logs"])]
    assert later, "one of the tasks should log the release"


@pytest.mark.posix_runtime
def test_retry_skips_known_uploads_and_applies_overrides(env):
    client, _, rid, _ = env
    t = client.post(
        "/api/tasks",
        json={"kind": "generate", "runtime": rid, "params": {"model": "z_image_turbo", "mode": "t2i", "prompt": "橘猫", "width": 1024, "height": 1024, "seed": 3}},
    ).json()
    done = wait_task(client, t["id"])
    assert done["can_retry"] and done["gen"]["prompt"] == "橘猫" and done["result"]["gen_seconds"] is not None
    again = client.post(f"/api/tasks/{t['id']}/retry", json={"overrides": {"seed": 9}}).json()
    again = wait_task(client, again["id"])
    assert again["status"] == "done" and again["retry_of"] == t["id"] and again["result"]["seed"] == 9

    tmp = Path(done["out_dir"]).parent
    ids = upload(client, media(tmp / "same.wav", video=False, seconds=2))
    first = wait_task(client, client.post("/api/tasks", json={"kind": "transcribe", "runtime": rid, "params": {}, "uploads": ids}).json()["id"])
    retried = wait_task(client, client.post(f"/api/tasks/{first['id']}/retry", json={}).json()["id"])
    assert retried["status"] == "done"
    assert any("跳过" in line for line in retried["logs"]), retried["logs"]


@pytest.mark.posix_runtime
def test_dub_retry_reuses_the_reviewed_translation(env):
    client, _, rid, tmp = env
    ids = upload(client, media(tmp / "re.wav", video=False, seconds=4))
    params = {"src_lang": "zh", "tgt_lang": "en", "translator": "runtime", "voice_mode": "preset", "voice": "af_heart", "keep_bgm": False, "review": True}
    t = client.post("/api/tasks", json={"kind": "dub", "runtime": rid, "params": params, "uploads": ids}).json()
    wait_task(client, t["id"], until=("waiting_review", "error"))
    client.post(f"/api/tasks/{t['id']}/review", json={"edits": {"re.wav": [{"id": 1, "text": "Edited by hand."}]}})
    assert wait_task(client, t["id"])["status"] == "done"
    again = wait_task(client, client.post(f"/api/tasks/{t['id']}/retry", json={}).json()["id"], until=("done", "error", "waiting_review"))
    assert again["status"] == "done", again["logs"][-8:]
    assert any("沿用上次" in line for line in again["logs"])
    assert "Edited by hand." in client.get(f"/api/tasks/{again['id']}/files/re.dubbed.srt").text


def test_signed_file_urls_stream_without_the_token(env):
    client, _, rid, _ = env
    t = client.post("/api/tasks", json={"kind": "tts", "runtime": rid, "params": {"text": "第一句\n第二句", "merge": False}}).json()
    done = wait_task(client, t["id"])
    url = done["outputs"][0]["url"]
    bare = TestClient(client.app)
    r = bare.get(url, headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and len(r.content) == 10
    assert bare.get(url.replace("sig=", "sig=x")).status_code == 403
    assert bare.get(url + "&download=1").headers["content-disposition"].startswith("attachment")


def test_tts_rejects_too_many_lines(env):
    client, _, rid, _ = env
    text = "\n".join(f"第 {i} 行" for i in range(501))
    r = client.post("/api/tasks", json={"kind": "tts", "runtime": rid, "params": {"text": text}})
    assert r.status_code == 400 and "500" in r.json()["error"]


@pytest.mark.posix_runtime
def test_restart_stops_the_orphaned_worker_command(env, fake_runtime):
    client, tasks, rid, tmp = env
    url, root = fake_runtime
    handle = tasks.m.handle(rid)
    kernel = handle.kernel(handle.WORKER)
    kernel.send_execute("import time; time.sleep(120)")
    time.sleep(1.5)
    # a new GPUDock process: it must not queue behind the orphan
    fresh = Manager(config_dir=tmp / "home", api_factory=lambda: tasks.m.api, runtime_fs_root=str(root))
    try:
        started = time.time()
        status = TaskManager(fresh, root=tmp / "ws").runtime_status(rid, refresh=True)
        assert status.get("agent") and time.time() - started < 30
        assert fresh.handle(rid).orphan_interrupted
    finally:
        fresh.stop()
