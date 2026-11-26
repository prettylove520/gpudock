"""`gpudock`: start the local GUI server and open it in the browser."""

from __future__ import annotations

import argparse
import os
import secrets
import signal
import socket
import threading
import webbrowser

from ..auth import config_dir
from ..proxy import use_system_proxy
from .app import create_app
from .state import Manager
from .tasks import TaskManager


def _free_port(preferred: int) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 20), 0]:
        with socket.socket() as s:
            # uvicorn binds with SO_REUSEADDR, so a port in TIME_WAIT is usable.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise SystemExit("找不到可用端口")


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    use_system_proxy()

    p = argparse.ArgumentParser(prog="gpudock", description="GPUDock：Google Colab 的图形界面")
    p.add_argument("--port", type=int, default=int(os.environ.get("GPUDOCK_PORT", 8765)))
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.add_argument("--dev", action="store_true", help="允许 Vite 开发服务器（localhost:5173）代理访问")
    p.add_argument("--token", help="开发用：固定访问令牌（默认每次启动随机生成）")
    p.add_argument("--window", action="store_true", help="在独立的桌面窗口里打开（需要 pywebview）")
    p.add_argument("--keep-runtimes", action="store_true", help="这次退出时不释放显卡（覆盖“退出时释放”设置）")
    args = p.parse_args(argv)

    token = args.token or os.environ.get("GPUDOCK_TOKEN") or secrets.token_urlsafe(24)
    port = _free_port(args.port)
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    manager = Manager(config_dir=config_dir())
    manager.start_reaper()
    app = create_app(manager, token=token, allowed_hosts=allowed, tasks=TaskManager(manager))

    url = f"http://127.0.0.1:{port}/#token={token}"
    on_exit = "退出时会释放 GPUDock 开的显卡" if manager.settings.release_on_exit and not args.keep_runtimes else "退出时不会释放显卡，记得手动释放"
    print(f"\nGPUDock 已启动：{url}\n（只在本机可访问；按 Ctrl+C 或关闭这个窗口即退出，{on_exit}。）\n", flush=True)
    if args.dev:
        print(f"开发模式：前端用 http://localhost:5173/#token={token}\n", flush=True)
    # Closing the terminal sends SIGHUP, which would kill the process without any
    # cleanup; turn it into the same graceful shutdown as Ctrl+C.
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: os.kill(os.getpid(), signal.SIGTERM))
    try:
        if args.window:
            _run_window(app, port, url)
        else:
            if not args.no_browser:
                threading.Timer(1.0, lambda: webbrowser.open(url)).start()
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        _shutdown(manager, keep=args.keep_runtimes)


def _say(text: str) -> None:
    try:
        print(text, flush=True)
    except OSError:  # the terminal is already gone
        pass


def _shutdown(manager: Manager, *, keep: bool) -> None:
    """The idle auto-release only runs while GPUDock does, so release on the way out."""
    owned = [m for m in manager.owned.all().values() if not m.get("pending")]
    if owned and manager.settings.release_on_exit and not keep:
        _say(f"正在释放 {len(owned)} 张 GPUDock 开的显卡…")
        try:
            released = manager.release_all_owned()
            _say("已释放：" + "、".join(released) if released else "没有释放成功，请到 Colab 网页里检查。")
        except Exception as err:
            _say(f"释放失败：{err}。请到 Colab 网页里手动释放，避免继续计费。")
    elif owned:
        _say(f"注意：还有 {len(owned)} 张 GPUDock 开的显卡在运行并计费。下次打开 GPUDock 会继续按空闲时间自动释放。")
    manager.stop()


def _run_window(app, port: int, url: str) -> None:
    """Serve in a background thread and show the UI in a native window."""
    import time

    import uvicorn

    try:
        import webview
    except ImportError:
        raise SystemExit("桌面窗口模式需要 pywebview：uv pip install 'gpudock-core[desktop]'，或去掉 --window 用浏览器打开。") from None
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    webview.create_window("GPUDock", url, width=1360, height=900, min_size=(900, 640), background_color="#f1eee7")
    webview.start()
    server.should_exit = True
    thread.join(timeout=5)


if __name__ == "__main__":
    main()
