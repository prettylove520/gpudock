"""Google sign-in for the Colab API.

For end users sign-in is one step: ``login`` opens the browser, they pick their
Google account and click "Allow". That works because GPUDock ships with the
project's own OAuth client ("Desktop app" type, owned by the GPUDock
maintainers). The client is resolved in this order:

1. ``--client-secrets PATH`` on the command line (power users, self-hosters)
2. ``GPUDOCK_OAUTH_CLIENT_FILE`` (path) or ``GPUDOCK_OAUTH_CLIENT_ID`` +
   ``GPUDOCK_OAUTH_CLIENT_SECRET`` environment variables
3. ``~/.config/gpudock/oauth_client.json`` (written by ``setup-client``; how a
   maintainer or developer installs the project client on their machine)
4. ``gpudock_core/_oauth_client.json`` bundled into release builds by CI
   (``scripts/embed_oauth_client.py``); gitignored, never committed

For installed apps Google does not treat the client secret as confidential, so
shipping it inside the app is the standard practice (gcloud and rclone do the
same). Keeping it out of git just avoids secret-scanning noise and makes forks
use their own client.

Alternatively ``adc`` mode reuses gcloud Application Default Credentials.

We never ship or borrow anyone else's OAuth client id.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import webbrowser
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from .errors import AuthError

COLAB_SCOPE = "https://www.googleapis.com/auth/colaboratory"
COLAB_READONLY_SCOPE = "https://www.googleapis.com/auth/colaboratory.readonly"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
BASE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    COLAB_SCOPE,
]
ADC_LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    COLAB_SCOPE,
]

KEYRING_SERVICE = "gpudock"
KEYRING_USER = "oauth-token"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

ENV_CLIENT_FILE = "GPUDOCK_OAUTH_CLIENT_FILE"
ENV_CLIENT_ID = "GPUDOCK_OAUTH_CLIENT_ID"
ENV_CLIENT_SECRET = "GPUDOCK_OAUTH_CLIENT_SECRET"
BUNDLED_CLIENT = Path(__file__).with_name("_oauth_client.json")

NO_CLIENT_HELP = (
    "这个 GPUDock 副本里没有内置 OAuth 客户端。\n"
    "- 普通用户：请使用官方发布版（发布版内置了项目的客户端，登录只需在浏览器里点“允许”）。\n"
    "- 维护者/开发者：按 docs/m0.md 的“维护者一次性配置”创建桌面应用客户端，然后运行\n"
    "    gpudock-m0 setup-client ~/Downloads/client_secret_xxx.json\n"
    "  这一步每台开发机只做一次。"
)


MISSING_COLAB_SCOPE = (
    "授权时没有勾选 Colab 权限，GPUDock 无法管理你的 Colab 运行时。\n"
    "重新运行 login，在授权页“选择 GPUDock 可以访问的内容”里点“全选”，"
    "或者勾上 Colab 那一项（查看、修改和删除 Colab 资源），再点“继续”。"
)


def granted_scopes(creds: Any) -> set[str]:
    """Scopes the user actually granted (falls back to requested scopes only if unknown)."""
    granted = getattr(creds, "granted_scopes", None)
    if granted is None:
        granted = getattr(creds, "scopes", None) or []
    if isinstance(granted, str):
        granted = granted.split()
    return set(granted)


def config_dir() -> Path:
    base = os.environ.get("GPUDOCK_HOME")
    path = Path(base) if base else Path.home() / ".config" / "gpudock"
    path.mkdir(parents=True, exist_ok=True)
    return path


def local_client_path() -> Path:
    return config_dir() / "oauth_client.json"


# ---- OAuth client resolution -------------------------------------------------
def validate_client_config(data: Any, origin: str) -> dict[str, Any]:
    if not isinstance(data, dict) or "installed" not in data:
        raise AuthError(
            f"{origin} 不是“桌面应用（Desktop app）”类型的 OAuth 客户端。"
            "请在 Cloud Console 新建 Desktop app 类型的客户端后重新下载 JSON。"
        )
    installed = data["installed"]
    for key in ("client_id", "client_secret"):
        if not installed.get(key):
            raise AuthError(f"{origin} 缺少 {key}。")
    installed.setdefault("auth_uri", "https://accounts.google.com/o/oauth2/auth")
    installed.setdefault("token_uri", "https://oauth2.googleapis.com/token")
    installed.setdefault("redirect_uris", ["http://localhost"])
    return data


def _read_client_file(path: Path, origin: str) -> dict[str, Any]:
    if not path.exists():
        raise AuthError(f"找不到 OAuth 客户端 JSON：{path}")
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise AuthError(f"{origin} 不是有效的 JSON：{exc}") from exc
    return validate_client_config(data, origin)


def resolve_client_config(explicit: str | None = None) -> tuple[dict[str, Any], str]:
    """Return (client_config, source) following the order in the module docstring."""
    if explicit:
        path = Path(explicit).expanduser()
        return _read_client_file(path, str(path)), "command-line"
    env_file = os.environ.get(ENV_CLIENT_FILE)
    if env_file:
        path = Path(env_file).expanduser()
        return _read_client_file(path, f"${ENV_CLIENT_FILE}"), "env-file"
    env_id, env_secret = os.environ.get(ENV_CLIENT_ID), os.environ.get(ENV_CLIENT_SECRET)
    if env_id and env_secret:
        data = {"installed": {"client_id": env_id, "client_secret": env_secret}}
        return validate_client_config(data, "环境变量"), "env-vars"
    local = local_client_path()
    if local.exists():
        return _read_client_file(local, str(local)), "local"
    if BUNDLED_CLIENT.exists():
        return _read_client_file(BUNDLED_CLIENT, "内置客户端"), "bundled"
    raise AuthError(NO_CLIENT_HELP)


def install_client(source: str) -> Path:
    """Copy a downloaded Desktop-app client JSON to ~/.config/gpudock/oauth_client.json."""
    src = Path(source).expanduser()
    _read_client_file(src, str(src))  # validate before copying
    dest = local_client_path()
    shutil.copyfile(src, dest)
    os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)
    return dest


# ---- settings and token storage --------------------------------------------
@dataclass
class AuthSettings:
    mode: str = "oauth"  # "oauth" | "adc"
    quota_project: str | None = None
    client_source: str | None = None

    @classmethod
    def load(cls) -> AuthSettings:
        path = config_dir() / "auth.json"
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls(**{k: data.get(k) for k in ("mode", "quota_project", "client_source") if k in data})

    def save(self) -> None:
        path = config_dir() / "auth.json"
        path.write_text(json.dumps(asdict(self), indent=2))


class TokenStore:
    """Stores the authorized-user JSON (contains the refresh token)."""

    def __init__(self) -> None:
        self._file = config_dir() / "token.json"

    def _keyring(self):
        if os.environ.get("GPUDOCK_NO_KEYRING"):
            return None
        try:
            import keyring

            keyring.get_keyring()
            return keyring
        except Exception:  # pragma: no cover - backend availability is host-specific
            return None

    def save(self, payload: str) -> str:
        kr = self._keyring()
        if kr is not None:
            try:
                kr.set_password(KEYRING_SERVICE, KEYRING_USER, payload)
                if self._file.exists():
                    self._file.unlink()
                return "系统钥匙串"
            except Exception:
                pass
        self._file.write_text(payload)
        os.chmod(self._file, stat.S_IRUSR | stat.S_IWUSR)
        return str(self._file)

    def load(self) -> str | None:
        kr = self._keyring()
        if kr is not None:
            try:
                value = kr.get_password(KEYRING_SERVICE, KEYRING_USER)
                if value:
                    return value
            except Exception:
                pass
        if self._file.exists():
            return self._file.read_text()
        return None

    def clear(self) -> None:
        kr = self._keyring()
        if kr is not None:
            try:
                kr.delete_password(KEYRING_SERVICE, KEYRING_USER)
            except Exception:
                pass
        if self._file.exists():
            self._file.unlink()


# ---- login -------------------------------------------------------------------
class _CapturingBrowser(webbrowser.BaseBrowser):
    """Hands the consent URL to a callback (for the GUI), then opens the real browser."""

    def __init__(self, on_url: Callable[[str], None], open_real: bool) -> None:
        super().__init__("gpudock-capture")
        self._on_url = on_url
        self._open_real = open_real

    def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
        self._on_url(url)
        if not self._open_real:
            return True
        try:
            return webbrowser.get().open(url, new=new, autoraise=autoraise)
        except webbrowser.Error:
            return False


def login_oauth(
    client_secrets_path: str | None = None,
    *,
    extra_scopes: list[str] | None = None,
    open_browser: bool = True,
    on_url: Callable[[str], None] | None = None,
) -> tuple[Any, str, str]:
    """Run the installed-app flow (PKCE + loopback). Returns (creds, stored_where, client_source).

    ``on_url`` receives the Google consent URL (the GUI shows it as a fallback link).
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    browser_name = None
    if on_url is not None:
        browser_name = f"gpudock-capture-{id(on_url)}"
        webbrowser.register(browser_name, None, _CapturingBrowser(on_url, open_browser))
        open_browser = True

    config, source = resolve_client_config(client_secrets_path)
    scopes = BASE_SCOPES + list(extra_scopes or [])
    # Google's consent screen lets users untick individual scopes. Without this,
    # oauthlib raises a bare "Scope has changed" Warning; we check below instead.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_config(config, scopes=scopes, autogenerate_code_verifier=True)
    try:
        creds = flow.run_local_server(
            host="localhost",
            bind_addr="127.0.0.1",
            port=0,
            open_browser=open_browser,
            authorization_prompt_message="正在打开浏览器登录 Google。如果没有自动打开，复制这个链接到浏览器：\n{url}\n",
            success_message="GPUDock 登录完成，可以关闭这个页面，回到 GPUDock 了。",
            timeout_seconds=600,
            browser=browser_name,
            prompt="consent",
        )
    except Warning as exc:  # oauthlib signals scope changes with a Warning subclass
        if "Scope has changed" in str(exc):
            raise AuthError(MISSING_COLAB_SCOPE) from None
        raise
    if creds is None:
        raise AuthError("10 分钟内没有完成浏览器授权，登录已取消。重新运行 login 即可。")
    if COLAB_SCOPE not in granted_scopes(creds):
        raise AuthError(MISSING_COLAB_SCOPE)
    where = TokenStore().save(creds.to_json())
    settings = AuthSettings.load()
    settings.mode = "oauth"
    settings.client_source = source
    settings.save()
    return creds, where, source


class Authenticator:
    """Hands out fresh request headers for colaboratory.googleapis.com."""

    def __init__(self, settings: AuthSettings | None = None) -> None:
        self.settings = settings or AuthSettings.load()
        self._creds = None

    def _load(self):
        if self._creds is not None:
            return self._creds
        if self.settings.mode == "adc":
            import google.auth

            try:
                creds, _project = google.auth.default(scopes=ADC_LOGIN_SCOPES)
            except Exception as exc:  # google.auth.exceptions.DefaultCredentialsError
                raise AuthError(
                    "没有找到 gcloud ADC 凭据。先运行：\n  gcloud auth application-default login "
                    f"--scopes={','.join(ADC_LOGIN_SCOPES)}"
                ) from exc
        else:
            from google.oauth2.credentials import Credentials

            payload = TokenStore().load()
            if not payload:
                raise AuthError("还没有登录：先运行 `gpudock-m0 login`。")
            creds = Credentials.from_authorized_user_info(json.loads(payload))
        if self.settings.quota_project and hasattr(creds, "with_quota_project"):
            creds = creds.with_quota_project(self.settings.quota_project)
        self._creds = creds
        return creds

    def headers(self) -> dict[str, str]:
        from google.auth.transport.requests import Request

        creds = self._load()
        if not creds.valid:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise AuthError(f"登录已过期或被撤销（{exc}）。重新运行 `gpudock-m0 login`。") from exc
            if self.settings.mode == "oauth" and hasattr(creds, "to_json"):
                TokenStore().save(creds.to_json())
        headers: dict[str, str] = {}
        creds.apply(headers)
        return headers

    def token_info(self) -> dict[str, Any]:
        """Ask Google which scopes/audience the current access token has."""
        headers = {k.lower(): v for k, v in self.headers().items()}
        token = headers["authorization"].split(" ", 1)[1]
        resp = httpx.post(TOKENINFO_URL, data={"access_token": token}, timeout=30)
        resp.raise_for_status()
        return resp.json()
