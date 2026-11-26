"""M0 experiments. Each one answers a question from proposal §8.

All experiments that create a runtime delete it again in a ``finally`` block
unless ``--keep`` is given, and they only ever delete runtimes this tool created.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..colab_api import ColabApi, Runtime, RuntimeSpec, SpecKey
from ..errors import ColabApiError, KernelError
from ..files import download, remote_sha256_code, sha256_file, upload
from ..jupyter import ConnectionClosed, ExecResult, Kernel, RuntimeConnection
from .recorder import Recorder

Out = Callable[[str], None]

# Community measurement (2026-03, $0.10/CU). Only used to print rough cost hints.
CU_PER_HOUR = {"T4": 1.19, "L4": 1.71, "A100": 5.40, "A100 (HIGHMEM)": 7.52, "G4": 8.71}

GPU_INFO_CODE = r"""
import json as _j, os as _os, shutil as _sh, subprocess as _sp, sys as _sys, platform as _pf
def _gpudock_info():
    info = {"python": _sys.version.split()[0], "cpus": _os.cpu_count(), "platform": _pf.platform()}
    try:
        with open("/proc/meminfo") as f:
            info["ram_gb"] = round(int(f.readline().split()[1]) / 1048576, 1)
    except Exception:
        pass
    try:
        info["disk_free_gb"] = round(_sh.disk_usage("/content").free / 1e9, 1)
    except Exception:
        pass
    try:
        out = _sp.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version,compute_cap",
                       "--format=csv,noheader"], capture_output=True, text=True, timeout=30).stdout.strip()
        if out:
            name, mem, drv, cc = [x.strip() for x in out.splitlines()[0].split(",")]
            info["gpu"] = {"name": name, "memory_total": mem, "driver": drv, "compute_cap": cc}
    except Exception as e:
        info["gpu_error"] = repr(e)
    return info
_gpudock_info_result = _gpudock_info()
"""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def parse_label(label: str) -> tuple[str, str]:
    """'a100-highmem' -> ('A100', 'SHAPE_HIGHMEM'); 'cpu' -> ('NONE', ...)."""
    text = label.strip().upper().replace(":", "-").replace("_", "-")
    shape = "SHAPE_STANDARD"
    if text.endswith("-HIGHMEM"):
        text = text[: -len("-HIGHMEM")]
        shape = "SHAPE_HIGHMEM"
    if text == "CPU":
        text = "NONE"
    return text, shape


def find_spec(specs: list[RuntimeSpec], label: str) -> RuntimeSpec | None:
    accel, shape = parse_label(label)
    for spec in specs:
        if spec.key.accelerator.upper() == accel and spec.key.shape == shape:
            return spec
    return None


def cost_hint(spec: SpecKey) -> str:
    rate = CU_PER_HOUR.get(spec.label)
    return f"约 {rate} CU/小时（社区 2026-03 实测，仅供参考）" if rate else "单价未知"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Session:
    runtime: Runtime
    conn: RuntimeConnection
    kernel: Kernel
    session_id: str
    created_here: bool
    ready_seconds: float | None


def make_connection(api: ColabApi, runtime: Runtime) -> RuntimeConnection:
    if runtime.connection is None:
        runtime = api.get_runtime(runtime.name)
    assert runtime.connection is not None, "运行时没有返回 connectionInfo"

    def refresh() -> tuple[str, str]:
        fresh = api.get_runtime(runtime.name)
        assert fresh.connection is not None
        runtime.connection = fresh.connection
        return fresh.connection.url, fresh.connection.token

    return RuntimeConnection(runtime.connection.url, runtime.connection.token, refresh=refresh)


def create_runtime(api: ColabApi, rec: Recorder, spec: SpecKey, out: Out, experiment: str) -> tuple[Runtime, float]:
    """Create a runtime we own (remembered for cleanup before the call is made)."""
    runtime_id = f"gpudock-m0-{uuid.uuid4().hex[:8]}"
    name = f"runtimes/{runtime_id}"
    rec.remember_runtime(name)
    out(f"创建运行时 {spec.label}（{cost_hint(spec)}）…")
    started = time.monotonic()
    try:
        try:
            runtime = api.create_runtime(spec, runtime_id=runtime_id, on_progress=out)
        except ColabApiError as err:
            if err.status != "INVALID_ARGUMENT":
                raise
            # The backend may not accept client-chosen ids; let it pick one.
            rec.write(experiment, "note", message=f"runtimeId rejected: {err.message[:200]}")
            rec.forget_runtime(name)
            runtime = api.create_runtime(spec, on_progress=out)
            rec.remember_runtime(runtime.name)
    except Exception:
        rec.forget_runtime(name)
        raise
    if runtime.name != name:
        rec.forget_runtime(name)
        rec.remember_runtime(runtime.name)
    elapsed = time.monotonic() - started
    out(f"就绪：{runtime.name}（{elapsed:.0f}s）")
    return runtime, elapsed


def release_runtime(api: ColabApi, rec: Recorder, runtime: Runtime, out: Out, experiment: str) -> None:
    try:
        api.delete_runtime(runtime.name, allow_missing=True)
        rec.forget_runtime(runtime.name)
        rec.write(experiment, "deleted", runtime=runtime.name)
        out(f"已释放 {runtime.name}")
    except Exception as exc:  # keep going; the user can run cleanup
        rec.write(experiment, "error", error=repr(exc), stage="delete")
        out(f"⚠️ 释放 {runtime.name} 失败：{exc}。稍后运行 `gpudock-m0 cleanup`。")


@contextlib.contextmanager
def open_session(
    api: ColabApi,
    rec: Recorder,
    *,
    experiment: str,
    gpu: str | None,
    runtime_name: str | None,
    keep: bool,
    out: Out,
    allow_ineligible: bool = False,
) -> Iterator[Session]:
    created_here = False
    ready_seconds = None
    if runtime_name:
        runtime = api.get_runtime(runtime_name)
        out(f"复用运行时 {runtime.name}（{runtime.spec.label if runtime.spec else '?'}）")
    else:
        spec = find_spec(api.list_specs(), gpu or "T4")
        if spec is None:
            raise SystemExit(f"规格列表里没有 {gpu}。先运行 `gpudock-m0 probe` 查看可用规格。")
        if not spec.eligible and not allow_ineligible:
            raise SystemExit(f"当前账号对 {spec.key.label} 不可申请（eligible=false）。")
        runtime, ready_seconds = create_runtime(api, rec, spec.key, out, experiment)
        created_here = True
        rec.write(experiment, "created", runtime=runtime.name, spec=spec.key.label, ready_seconds=round(ready_seconds, 1))
    conn = make_connection(api, runtime)
    kernel = None
    session_id = ""
    try:
        session_id, kernel_id = conn.create_session(name=f"m0-{experiment}")
        kernel = Kernel(conn, kernel_id).connect()
        yield Session(runtime, conn, kernel, session_id, created_here, ready_seconds)
    finally:
        if kernel is not None:
            kernel.close()
        if session_id:
            with contextlib.suppress(Exception):
                conn.delete_session(session_id)
        conn.close()
        if created_here and not keep:
            release_runtime(api, rec, runtime, out, experiment)
        elif created_here:
            out(f"按 --keep 保留运行时 {runtime.name}，用完记得 `gpudock-m0 cleanup`。")


def gpu_info(kernel: Kernel) -> dict:
    return kernel.eval_json("_gpudock_info_result", setup=GPU_INFO_CODE, idle_timeout=120)


# --------------------------------------------------------------------------
# experiments
# --------------------------------------------------------------------------
def probe(api: ColabApi, rec: Recorder, out: Out) -> dict:
    tier = api.get_subscription()
    specs = api.list_specs()
    runtimes = api.list_runtimes()
    out(f"订阅等级：{tier}")
    out(f"已有运行时：{len(runtimes)} 个")
    for r in runtimes:
        out(f"  - {r.name}  {r.spec.label if r.spec else '?'}")
    out("")
    out(f"{'规格':<16}{'variant':<14}{'shape':<16}可申请")
    for s in specs:
        out(f"{s.key.label:<16}{s.key.variant:<14}{s.key.shape:<16}{'✅' if s.eligible else '❌'}")
    record = rec.write(
        "probe",
        "result",
        tier=tier,
        runtime_count=len(runtimes),
        runtimes=[{"name": r.name, "spec": r.spec.label if r.spec else None} for r in runtimes],
        specs=[
            {"label": s.key.label, "variant": s.key.variant, "accelerator": s.key.accelerator, "shape": s.key.shape, "eligible": s.eligible}
            for s in specs
        ],
    )
    return record


def capacity(api: ColabApi, rec: Recorder, labels: list[str], out: Out, pause_s: float = 15) -> list[dict]:
    """Try to get each GPU once: create -> nvidia-smi -> delete."""
    specs = api.list_specs()
    results = []
    for label in labels:
        spec = find_spec(specs, label)
        if spec is None:
            out(f"[{label}] 规格列表里没有，跳过")
            results.append(rec.write("capacity", "skipped", spec=label, why="not listed"))
            continue
        if not spec.eligible:
            out(f"[{spec.key.label}] 不可申请，跳过")
            results.append(rec.write("capacity", "skipped", spec=spec.key.label, why="not eligible"))
            continue
        runtime = None
        try:
            runtime, ready = create_runtime(api, rec, spec.key, out, "capacity")
            conn = make_connection(api, runtime)
            try:
                session_id, kernel_id = conn.create_session(name="m0-capacity")
                with Kernel(conn, kernel_id) as kernel:
                    info = gpu_info(kernel)
                conn.delete_session(session_id)
            finally:
                conn.close()
            out(f"[{spec.key.label}] ✅ {info.get('gpu', {}).get('name')} {info.get('gpu', {}).get('memory_total')}，就绪 {ready:.0f}s")
            results.append(
                rec.write("capacity", "created", spec=spec.key.label, ready_seconds=round(ready, 1), gpu=info.get("gpu"), machine=info)
            )
        except ColabApiError as err:
            event = "refused" if err.status == "FAILED_PRECONDITION" else "error"
            out(f"[{spec.key.label}] ❌ {err}")
            results.append(rec.write("capacity", event, spec=spec.key.label, error=err.to_record()))
        except Exception as exc:
            out(f"[{spec.key.label}] ❌ {exc!r}")
            results.append(rec.write("capacity", "error", spec=spec.key.label, error={"message": repr(exc)}))
        finally:
            if runtime is not None:
                release_runtime(api, rec, runtime, out, "capacity")
        time.sleep(pause_s)
    return results


def e2e(
    api: ColabApi,
    rec: Recorder,
    out: Out,
    *,
    gpu: str,
    size_mb: int,
    keep: bool,
    runtime_name: str | None,
    chunk_mb: int = 16,
    remote_root: str = "/",
) -> dict:
    """``remote_root`` is the Jupyter server's root dir ("/" on Colab)."""
    summary: dict = {"gpu": gpu, "size_mb": size_mb}
    with open_session(api, rec, experiment="e2e", gpu=gpu, runtime_name=runtime_name, keep=keep, out=out) as s:
        summary["ready_seconds"] = s.ready_seconds and round(s.ready_seconds, 1)
        status = s.conn.status()
        summary["jupyter_status_ok"] = bool(status)

        res = s.kernel.execute("print('hello from colab'); 1 + 1")
        summary["hello_ok"] = res.ok and "hello from colab" in res.stdout and res.result == "2"
        summary["hello_ms"] = round(res.elapsed * 1000)
        out(f"执行 hello：{'✅' if summary['hello_ok'] else '❌'}（{summary['hello_ms']} ms 往返）")

        info = gpu_info(s.kernel)
        summary["machine"] = info
        out(f"机器：GPU={info.get('gpu')} RAM={info.get('ram_gb')}GB 磁盘剩余={info.get('disk_free_gb')}GB")

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "payload.bin"
            with src.open("wb") as fh:
                for _ in range(size_mb):
                    fh.write(os.urandom(1024 * 1024))
            local_sha = sha256_file(src)
            remote = "content/gpudock-m0/payload.bin"

            up = upload(s.conn, src, remote, chunk_size=chunk_mb * 1024 * 1024)
            out(f"上传 {size_mb} MB：{up.seconds:.1f}s，{up.mb_per_s:.2f} MB/s，{up.chunks} 片")
            rec.write("e2e", "upload", **up.to_record())

            s.kernel.execute(remote_sha256_code(remote, root=remote_root), idle_timeout=300)
            remote_sha = s.kernel.eval_json("_gpudock_sha_result")
            summary["upload_integrity"] = remote_sha == local_sha

            dst = Path(tmp) / "back.bin"
            down = download(s.conn, remote, dst)
            out(f"下载 {size_mb} MB：{down.seconds:.1f}s，{down.mb_per_s:.2f} MB/s")
            rec.write("e2e", "download", **down.to_record())
            summary["download_integrity"] = sha256_file(dst) == local_sha
            summary["upload_mb_s"] = round(up.mb_per_s, 2)
            summary["download_mb_s"] = round(down.mb_per_s, 2)
            out(f"完整性：上传 {'✅' if summary['upload_integrity'] else '❌'}，下载 {'✅' if summary['download_integrity'] else '❌'}")

        # Interrupt: start a long sleep, interrupt it, expect KeyboardInterrupt.
        msg_id = s.kernel.send_execute("import time; time.sleep(120)")
        time.sleep(3)
        s.conn.interrupt(s.kernel.kernel_id)
        ires = s.kernel.wait(msg_id, idle_timeout=60)
        summary["interrupt_ok"] = bool(ires.error and ires.error.get("ename") == "KeyboardInterrupt")
        out(f"中断：{'✅' if summary['interrupt_ok'] else '❌'} {ires.error_text()}")
    rec.write("e2e", "summary", **summary)
    return summary


def token_expiry(
    api: ColabApi,
    rec: Recorder,
    out: Out,
    *,
    gpu: str,
    minutes: int,
    keep: bool,
    runtime_name: str | None,
    rest_probe_every_s: int = 300,
) -> dict:
    """Keep one execution streaming past the proxy token's expiry.

    Measures: does the open WebSocket survive the token's expireTime? When does
    the *old* token stop working for REST? After a reconnect with a refreshed
    token, does the same execution's output resume, and were missed messages
    replayed?
    """
    summary: dict = {"gpu": gpu, "minutes": minutes}
    with open_session(api, rec, experiment="token-expiry", gpu=gpu, runtime_name=runtime_name, keep=keep, out=out) as s:
        conn_info = s.runtime.connection or api.get_runtime(s.runtime.name).connection
        assert conn_info is not None
        expire_at = conn_info.expire_time
        old_token = conn_info.token
        left = conn_info.seconds_left()
        summary["token_seconds_left_at_start"] = round(left) if left is not None else None
        out(f"运行时代理令牌还有 {left/60 if left else float('nan'):.1f} 分钟过期（{expire_at}）")
        rec.write("token-expiry", "start", token_expire_at=str(expire_at), seconds_left=left)

        # A REST client frozen on the original token (no refresh).
        frozen = RuntimeConnection(s.conn.base, old_token)
        beats = minutes * 2
        code = (
            "import time, json\n"
            f"for i in range({beats}):\n"
            "    print(json.dumps({'i': i, 't': time.time()}), flush=True)\n"
            "    time.sleep(30)\n"
        )
        seen: list[int] = []

        def on_output(kind: str, text: str) -> None:
            for line in text.splitlines():
                with contextlib.suppress(Exception):
                    seen.append(json.loads(line)["i"])

        def rel_to_expiry() -> float | None:
            if expire_at is None:
                return None
            return round((datetime.now(UTC) - expire_at).total_seconds())

        msg_id = s.kernel.send_execute(code)
        result = ExecResult(msg_id=msg_id, started=time.monotonic())
        last_probe = 0.0
        reconnects = 0
        old_token_failed_at = None
        ws_closed_at = None
        resumed = None
        missing_after_reconnect = None
        deadline = time.monotonic() + minutes * 60 + 300
        while time.monotonic() < deadline:
            if time.monotonic() - last_probe > rest_probe_every_s:
                last_probe = time.monotonic()
                resp = frozen.request("GET", "api/status", retry_auth=False)
                rel = rel_to_expiry()
                rec.write("token-expiry", "rest-probe", old_token_http=resp.status_code, rel_to_expiry_s=rel, beats_seen=len(seen))
                out(f"[{now_iso()}] 旧令牌 REST → HTTP {resp.status_code}；距过期 {rel}s；心跳 {len(seen)}")
                if resp.status_code != 200 and old_token_failed_at is None:
                    old_token_failed_at = rel
            try:
                # Heartbeats arrive every 30s, so bound the wait by wall time to
                # get back to the REST probe regularly.
                result = s.kernel.wait(
                    msg_id, on_output=on_output, idle_timeout=90, total_timeout=rest_probe_every_s, result=result
                )
                break  # execution finished
            except TimeoutError:
                continue  # probe tick, or 90s of silence; keep listening
            except ConnectionClosed as exc:
                ws_closed_at = rel_to_expiry()
                before = len(seen)
                last_i = seen[-1] if seen else -1
                rec.write("token-expiry", "ws-closed", rel_to_expiry_s=ws_closed_at, code=getattr(exc.rcvd, "code", None), beats_seen=before)
                out(f"WebSocket 断开（距过期 {ws_closed_at}s），刷新令牌后重连…")
                reconnects += 1
                s.kernel.reconnect(refresh_token=True)
                try:
                    result = s.kernel.wait(msg_id, on_output=on_output, idle_timeout=90, result=result)
                    break
                except TimeoutError:
                    pass
                finally:
                    after = [i for i in seen if i > last_i]
                    resumed = bool(after)
                    if after:
                        missing_after_reconnect = after[0] - last_i - 1
                    rec.write("token-expiry", "reconnected", resumed=resumed, first_new_beat=after[0] if after else None, last_beat_before=last_i)
        frozen.close()
        summary.update(
            {
                "beats_expected": beats,
                "beats_seen": len(set(seen)),
                "ws_closed_rel_to_expiry_s": ws_closed_at,
                "ws_survived_past_expiry": ws_closed_at is None and (rel_to_expiry() or -1) > 0,
                "old_token_rest_failed_rel_to_expiry_s": old_token_failed_at,
                "reconnects": reconnects,
                "reconnect_resumed_output": resumed,
                "beats_missed_across_reconnect": missing_after_reconnect,
                "execution_status": result.status,
            }
        )
    rec.write("token-expiry", "summary", **summary)
    out(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def idle(
    api: ColabApi,
    rec: Recorder,
    out: Out,
    *,
    gpu: str,
    max_minutes: int,
    poll_minutes: int,
    close_session: bool,
) -> dict:
    """Leave a runtime idle (no execution, no client) and watch when it is reclaimed.

    Polls only the Colab API (runtimes.list), never the runtime itself, so the
    polling cannot count as activity.
    """
    spec = find_spec(api.list_specs(), gpu)
    if spec is None or not spec.eligible:
        raise SystemExit(f"{gpu} 不可用，先运行 probe。")
    runtime, ready = create_runtime(api, rec, spec.key, out, "idle")
    conn = make_connection(api, runtime)
    try:
        session_id, kernel_id = conn.create_session(name="m0-idle")
        with Kernel(conn, kernel_id) as k:
            k.execute("x = 1")
        if close_session:
            conn.delete_session(session_id)
    finally:
        conn.close()
    idle_since = time.monotonic()
    out(f"运行时已进入空闲：{runtime.name}。每 {poll_minutes} 分钟查询一次，最长 {max_minutes} 分钟。")
    rec.write("idle", "idle-start", runtime=runtime.name, spec=spec.key.label, close_session=close_session)
    reclaimed_after = None
    try:
        while (time.monotonic() - idle_since) / 60 < max_minutes:
            time.sleep(poll_minutes * 60)
            names = {r.name for r in api.list_runtimes()}
            minutes_idle = round((time.monotonic() - idle_since) / 60, 1)
            alive = runtime.name in names
            rec.write("idle", "poll", minutes_idle=minutes_idle, alive=alive)
            out(f"[{now_iso()}] 空闲 {minutes_idle} 分钟：{'仍存在' if alive else '已被回收'}")
            if not alive:
                reclaimed_after = minutes_idle
                rec.forget_runtime(runtime.name)
                break
    finally:
        if reclaimed_after is None:
            release_runtime(api, rec, runtime, out, "idle")
    summary = {
        "spec": spec.key.label,
        "close_session": close_session,
        "reclaimed_after_min": reclaimed_after,
        "still_alive_at_max": reclaimed_after is None,
        "max_minutes": max_minutes,
        "poll_minutes": poll_minutes,
    }
    rec.write("idle", "summary", **summary)
    return summary


def long_exec(api: ColabApi, rec: Recorder, out: Out, *, gpu: str, hours: float, keep: bool) -> dict:
    """Run one execution for ``hours`` with proactive token refresh; record when it ends."""
    summary: dict = {"gpu": gpu, "hours_requested": hours}
    with open_session(api, rec, experiment="long-exec", gpu=gpu, runtime_name=None, keep=keep, out=out) as s:
        beats = int(hours * 60)
        code = (
            "import time, json\n"
            f"for i in range({beats}):\n"
            "    print(json.dumps({'i': i, 't': time.time()}), flush=True)\n"
            "    time.sleep(60)\n"
        )
        started = time.monotonic()
        last_beat = [started]

        def on_output(kind: str, text: str) -> None:
            last_beat[0] = time.monotonic()

        msg_id = s.kernel.send_execute(code)
        result = ExecResult(msg_id=msg_id, started=started)
        ended_by = "completed"
        while True:
            info = s.runtime.connection
            if info and (info.seconds_left() or 0) < 600:
                s.conn.refresh_token()
            try:
                result = s.kernel.wait(msg_id, on_output=on_output, idle_timeout=300, total_timeout=600, result=result)
                break
            except TimeoutError:
                if time.monotonic() - last_beat[0] > 900:
                    ended_by = "silent>15min"
                    break
            except ConnectionClosed:
                try:
                    s.kernel.reconnect(refresh_token=True)
                except Exception as exc:
                    ended_by = f"reconnect-failed: {exc!r}"
                    break
            except KernelError as exc:
                ended_by = f"kernel: {exc}"
                break
            rec.write("long-exec", "tick", hours=round((time.monotonic() - started) / 3600, 2))
        alive = s.runtime.name in {r.name for r in api.list_runtimes()}
        summary.update(
            {
                "hours_run": round((time.monotonic() - started) / 3600, 2),
                "ended_by": ended_by,
                "runtime_still_listed": alive,
                "execution_status": result.status,
            }
        )
    rec.write("long-exec", "summary", **summary)
    return summary


def cleanup(api: ColabApi, rec: Recorder, out: Out, *, yes: bool, ask: Callable[[str], bool]) -> list[str]:
    mine = set(rec.created_runtimes())
    live = {r.name for r in api.list_runtimes()}
    targets = sorted(mine & live)
    for gone in mine - live:
        rec.forget_runtime(gone)
    others = sorted(live - mine)
    if others:
        out(f"以下运行时不是本工具创建的，不会动：{', '.join(others)}")
    if not targets:
        out("没有需要清理的运行时。")
        return []
    out(f"将删除本工具创建的运行时：{', '.join(targets)}")
    if not yes and not ask("确认删除？"):
        out("已取消。")
        return []
    for name in targets:
        api.delete_runtime(name, allow_missing=True)
        rec.forget_runtime(name)
        rec.write("cleanup", "deleted", runtime=name)
        out(f"已删除 {name}")
    return targets
