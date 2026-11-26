"""`gpudock-m0`: command line for the M0 technical validation.

Read-only commands (probe, runtimes, whoami, report) cost nothing. Commands that
create a runtime spend the account's Colab quota/compute units and ask first
unless ``--yes`` is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..auth import (
    ADC_LOGIN_SCOPES,
    DRIVE_FILE_SCOPE,
    Authenticator,
    AuthSettings,
    TokenStore,
    install_client,
    login_oauth,
)
from ..colab_api import ColabApi
from ..errors import AuthError, ColabApiError
from . import experiments as ex
from .recorder import Recorder, build_report


def out(text: str = "") -> None:
    print(text, flush=True)


def ask(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def confirm_spend(args: argparse.Namespace, what: str) -> None:
    if getattr(args, "yes", False):
        return
    out(f"⚠️ {what}")
    out("   这会消耗你账号的 Colab 免费额度或计算单元。")
    if not ask("继续？"):
        raise SystemExit("已取消。")


def make_api(args: argparse.Namespace) -> ColabApi:
    settings = AuthSettings.load()
    if args.quota_project:
        settings.quota_project = args.quota_project
    auth = Authenticator(settings)
    return ColabApi(auth.headers)


# ---- commands -----------------------------------------------------------
def cmd_login(args: argparse.Namespace) -> None:
    settings = AuthSettings.load()
    if args.quota_project is not None:
        settings.quota_project = args.quota_project or None
    if args.adc:
        settings.mode = "adc"
        settings.save()
        out("已切换到 gcloud ADC 模式。如果还没登录，请运行：")
        out(f"  gcloud auth application-default login --scopes={','.join(ADC_LOGIN_SCOPES)}")
        if not settings.quota_project:
            out("  并用 --quota-project 指定一个已启用 Colab API 的 GCP 项目。")
        return
    settings.save()
    extra = [DRIVE_FILE_SCOPE] if args.drive else []
    _creds, where, source = login_oauth(args.client_secrets, extra_scopes=extra, open_browser=not args.no_browser)
    out(f"✅ 登录成功（客户端来源：{source}），令牌保存在：{where}")


def cmd_setup_client(args: argparse.Namespace) -> None:
    dest = install_client(args.path)
    out(f"✅ 已安装 OAuth 客户端到 {dest}")
    out("以后在这台机器上只需运行 `gpudock-m0 login`，不用再带任何参数。")
    out("发布版会在构建时由 CI 内置同一个客户端（见 scripts/embed_oauth_client.py），最终用户无需这一步。")


def cmd_logout(args: argparse.Namespace) -> None:
    TokenStore().clear()
    out("已清除本地保存的令牌。若要彻底撤销授权，请到 https://myaccount.google.com/permissions 移除该应用。")


def cmd_whoami(args: argparse.Namespace) -> None:
    settings = AuthSettings.load()
    if args.quota_project:
        settings.quota_project = args.quota_project
    info = Authenticator(settings).token_info()
    out(f"模式：{settings.mode}  quota project：{settings.quota_project or '（未设置）'}")
    out(f"账号：{info.get('email', '?')}")
    out(f"客户端：{info.get('aud') or info.get('azp')}")
    out(f"令牌剩余：{info.get('expires_in')} 秒")
    out("授权范围：")
    for scope in sorted((info.get("scope") or "").split()):
        out(f"  - {scope}")


def cmd_probe(args: argparse.Namespace) -> None:
    ex.probe(make_api(args), Recorder(), out)


def cmd_runtimes(args: argparse.Namespace) -> None:
    api = make_api(args)
    mine = set(Recorder().created_runtimes())
    for r in api.list_runtimes():
        left = r.connection.seconds_left() if r.connection else None
        tag = "（本工具创建）" if r.name in mine else ""
        out(f"{r.name}  {r.spec.label if r.spec else '?'}  令牌剩余 {left and round(left)}s {tag}")


def cmd_capacity(args: argparse.Namespace) -> None:
    labels = [x.strip() for x in args.gpus.split(",") if x.strip()]
    confirm_spend(args, f"将依次创建并立即释放这些运行时：{', '.join(labels)}（每个约几分钟）。")
    ex.capacity(make_api(args), Recorder(), labels, out, pause_s=args.pause)


def cmd_e2e(args: argparse.Namespace) -> None:
    if not args.runtime:
        confirm_spend(args, f"将创建一个 {args.gpu} 运行时，做执行/上传/下载/中断测试（约 5–10 分钟）。")
    summary = ex.e2e(
        make_api(args), Recorder(), out, gpu=args.gpu, size_mb=args.size_mb, keep=args.keep, runtime_name=args.runtime, chunk_mb=args.chunk_mb
    )
    out(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def cmd_token_expiry(args: argparse.Namespace) -> None:
    if not args.runtime:
        confirm_spend(args, f"将创建一个 {args.gpu} 运行时并持续执行 {args.minutes} 分钟。")
    ex.token_expiry(make_api(args), Recorder(), out, gpu=args.gpu, minutes=args.minutes, keep=args.keep, runtime_name=args.runtime)


def cmd_idle(args: argparse.Namespace) -> None:
    confirm_spend(args, f"将创建一个 {args.gpu} 运行时并让它空闲最多 {args.max_minutes} 分钟（空闲期间同样计费）。")
    summary = ex.idle(
        make_api(args), Recorder(), out, gpu=args.gpu, max_minutes=args.max_minutes, poll_minutes=args.poll_minutes, close_session=args.close_session
    )
    out(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_long_exec(args: argparse.Namespace) -> None:
    confirm_spend(args, f"将创建一个 {args.gpu} 运行时并持续执行 {args.hours} 小时（这是最贵的实验）。")
    summary = ex.long_exec(make_api(args), Recorder(), out, gpu=args.gpu, hours=args.hours, keep=args.keep)
    out(json.dumps(summary, ensure_ascii=False, indent=2))


def cmd_exec(args: argparse.Namespace) -> None:
    from ..jupyter import Kernel

    api = make_api(args)
    runtime = api.get_runtime(args.runtime)
    conn = ex.make_connection(api, runtime)
    code = Path(args.file).read_text() if args.file else args.code
    try:
        session_id, kernel_id = conn.create_session(name="m0-exec")
        with Kernel(conn, kernel_id) as k:
            res = k.execute(code, on_output=lambda kind, text: sys.stdout.write(text if kind != "result" else text + "\n"), idle_timeout=None)
        conn.delete_session(session_id)
    finally:
        conn.close()
    if not res.ok:
        raise SystemExit(f"执行失败：{res.error_text()}")


def cmd_cleanup(args: argparse.Namespace) -> None:
    ex.cleanup(make_api(args), Recorder(), out, yes=args.yes, ask=ask)


def cmd_note(args: argparse.Namespace) -> None:
    Recorder().write("note", "note", key=args.key, value=" ".join(args.value))
    out(f"已记录 {args.key}")


def cmd_report(args: argparse.Namespace) -> None:
    rec = Recorder()
    text = build_report(rec.records())
    path = rec.dir / "REPORT.md"
    path.write_text(text, encoding="utf-8")
    out(text)
    out(f"已写入 {path}")


# ---- parser ---------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gpudock-m0", description="GPUDock M0：Colab API 技术验证")
    p.add_argument("--quota-project", help="把 API 配额计到这个 GCP 项目（ADC 模式通常必需）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="用 Google 登录：打开浏览器，点“允许”即可")
    s.add_argument("--client-secrets", help="高级：改用自己的 Desktop app OAuth 客户端 JSON")
    s.add_argument("--adc", action="store_true", help="高级：改用 gcloud Application Default Credentials")
    s.add_argument("--drive", action="store_true", help="同时申请 drive.file 权限（M0 不需要）")
    s.add_argument("--no-browser", action="store_true", help="不自动打开浏览器，只打印链接")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("setup-client", help="维护者/开发者：在本机安装项目的 OAuth 客户端（一次性）")
    s.add_argument("path", help="从 Cloud Console 下载的 Desktop app 客户端 JSON")
    s.set_defaults(func=cmd_setup_client)

    sub.add_parser("logout", help="清除本地令牌").set_defaults(func=cmd_logout)
    sub.add_parser("whoami", help="查看当前令牌的账号与授权范围").set_defaults(func=cmd_whoami)
    sub.add_parser("probe", help="只读：订阅等级、规格列表、已有运行时（不花钱）").set_defaults(func=cmd_probe)
    sub.add_parser("runtimes", help="只读：列出已有运行时").set_defaults(func=cmd_runtimes)

    s = sub.add_parser("capacity", help="② 逐个尝试拿 GPU：创建→nvidia-smi→释放")
    s.add_argument("--gpus", default="T4,L4,A100,G4", help="逗号分隔，如 T4,L4,A100,A100-HIGHMEM,G4,H100")
    s.add_argument("--pause", type=float, default=15, help="两次尝试之间的间隔秒数")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_capacity)

    s = sub.add_parser("e2e", help="⑤ 端到端：执行、上传、下载、完整性、中断")
    s.add_argument("--gpu", default="T4")
    s.add_argument("--runtime", help="复用已有运行时（runtimes/xxx），不新建")
    s.add_argument("--size-mb", type=int, default=64)
    s.add_argument("--chunk-mb", type=int, default=16)
    s.add_argument("--keep", action="store_true", help="结束后不释放运行时")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_e2e)

    s = sub.add_parser("token-expiry", help="③ 令牌过期后 WebSocket 是否可用")
    s.add_argument("--gpu", default="T4")
    s.add_argument("--runtime")
    s.add_argument("--minutes", type=int, default=80, help="持续执行的分钟数，需超过令牌剩余时间（约 60 分钟）")
    s.add_argument("--keep", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_token_expiry)

    s = sub.add_parser("idle", help="④ 空闲多久会被回收")
    s.add_argument("--gpu", default="T4")
    s.add_argument("--max-minutes", type=int, default=240)
    s.add_argument("--poll-minutes", type=int, default=5)
    s.add_argument("--close-session", action="store_true", help="空闲前关闭 Jupyter 会话")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_idle)

    s = sub.add_parser("long-exec", help="④ 持续执行能跑多久")
    s.add_argument("--gpu", default="T4")
    s.add_argument("--hours", type=float, default=13)
    s.add_argument("--keep", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_long_exec)

    s = sub.add_parser("exec", help="在已有运行时上执行代码")
    s.add_argument("--runtime", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--code")
    g.add_argument("--file")
    s.set_defaults(func=cmd_exec)

    s = sub.add_parser("cleanup", help="删除本工具创建、仍在运行的运行时")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_cleanup)

    s = sub.add_parser("note", help="手动记录结论，例如：note scope_classification sensitive")
    s.add_argument("key")
    s.add_argument("value", nargs="+")
    s.set_defaults(func=cmd_note)

    sub.add_parser("report", help="生成 m0-results/REPORT.md").set_defaults(func=cmd_report)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (AuthError, ColabApiError) as err:
        Recorder().write(args.cmd, "error", error=err.to_record() if isinstance(err, ColabApiError) else {"message": str(err)})
        raise SystemExit(f"❌ {err}") from None
    except KeyboardInterrupt:
        raise SystemExit("已中断。如果创建过运行时，运行 `gpudock-m0 cleanup` 检查。") from None


if __name__ == "__main__":
    main()
