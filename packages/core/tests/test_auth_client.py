import json
import os
import stat

import pytest

from gpudock_core import auth
from gpudock_core.errors import AuthError

CLIENT = {"installed": {"client_id": "id-{}.apps.googleusercontent.com", "client_secret": "s-{}"}}


def client_json(tmp_path, tag):
    data = json.loads(json.dumps(CLIENT).replace("{}", tag))
    path = tmp_path / f"client_{tag}.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GPUDOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GPUDOCK_NO_KEYRING", "1")
    for var in (auth.ENV_CLIENT_FILE, auth.ENV_CLIENT_ID, auth.ENV_CLIENT_SECRET):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(auth, "BUNDLED_CLIENT", tmp_path / "bundled_missing.json")


def client_id(config):
    return config["installed"]["client_id"]


def test_no_client_anywhere_explains_both_audiences():
    with pytest.raises(AuthError) as info:
        auth.resolve_client_config()
    text = str(info.value)
    assert "官方发布版" in text and "setup-client" in text


def test_resolution_order(tmp_path, monkeypatch):
    bundled = client_json(tmp_path, "bundled")
    monkeypatch.setattr(auth, "BUNDLED_CLIENT", bundled)
    cfg, src = auth.resolve_client_config()
    assert src == "bundled" and client_id(cfg).startswith("id-bundled")

    auth.install_client(str(client_json(tmp_path, "local")))
    cfg, src = auth.resolve_client_config()
    assert src == "local" and client_id(cfg).startswith("id-local")

    monkeypatch.setenv(auth.ENV_CLIENT_ID, "id-envvars")
    monkeypatch.setenv(auth.ENV_CLIENT_SECRET, "s-envvars")
    cfg, src = auth.resolve_client_config()
    assert src == "env-vars" and client_id(cfg) == "id-envvars"
    assert cfg["installed"]["token_uri"] == "https://oauth2.googleapis.com/token"

    monkeypatch.setenv(auth.ENV_CLIENT_FILE, str(client_json(tmp_path, "envfile")))
    _, src = auth.resolve_client_config()
    assert src == "env-file"

    cfg, src = auth.resolve_client_config(str(client_json(tmp_path, "cli")))
    assert src == "command-line" and client_id(cfg).startswith("id-cli")


def test_install_client_rejects_web_clients_and_sets_private_mode(tmp_path):
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "x", "client_secret": "y"}}))
    with pytest.raises(AuthError, match="桌面应用"):
        auth.install_client(str(web))

    dest = auth.install_client(str(client_json(tmp_path, "ok")))
    if os.name != "nt":  # Windows has no POSIX mode bits; the profile ACL keeps it private
        assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_token_store_file_fallback_is_private(tmp_path):
    store = auth.TokenStore()
    where = store.save('{"refresh_token": "1//x"}')
    path = tmp_path / "home" / "token.json"
    assert where == str(path)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load() == '{"refresh_token": "1//x"}'
    store.clear()
    assert store.load() is None


def test_login_uses_resolved_client_without_arguments(tmp_path, monkeypatch):
    auth.install_client(str(client_json(tmp_path, "local")))
    seen = {}

    class FakeCreds:
        granted_scopes = auth.BASE_SCOPES
        scopes = auth.BASE_SCOPES

        def to_json(self):
            return '{"refresh_token": "r"}'

    class FakeFlow:
        @classmethod
        def from_client_config(cls, config, scopes, autogenerate_code_verifier):
            seen.update(client=client_id(config), scopes=scopes, pkce=autogenerate_code_verifier)
            return cls()

        def run_local_server(self, **kwargs):
            seen.update(bind=kwargs["bind_addr"], port=kwargs["port"])
            return FakeCreds()

    import google_auth_oauthlib.flow as flow_mod

    monkeypatch.setattr(flow_mod, "InstalledAppFlow", FakeFlow)
    _creds, where, source = auth.login_oauth()
    assert source == "local" and seen["client"].startswith("id-local")
    assert seen["pkce"] is True and seen["bind"] == "127.0.0.1" and seen["port"] == 0
    assert auth.COLAB_SCOPE in seen["scopes"]
    assert auth.AuthSettings.load().client_source == "local"
    assert auth.TokenStore().load() == '{"refresh_token": "r"}'


def test_login_fails_when_colab_scope_unchecked(tmp_path, monkeypatch):
    auth.install_client(str(client_json(tmp_path, "local")))

    class NoColab:
        granted_scopes = ["openid"]
        scopes = ["openid"]

    class FakeFlow:
        @classmethod
        def from_client_config(cls, *a, **k):
            return cls()

        def run_local_server(self, **kwargs):
            return NoColab()

    import google_auth_oauthlib.flow as flow_mod

    monkeypatch.setattr(flow_mod, "InstalledAppFlow", FakeFlow)
    with pytest.raises(AuthError, match="Colab"):
        auth.login_oauth()


def test_granted_scopes_does_not_fall_back_when_user_unticked_everything():
    class Creds:
        granted_scopes = []
        scopes = auth.BASE_SCOPES

    assert auth.COLAB_SCOPE not in auth.granted_scopes(Creds())

    class StrCreds:
        granted_scopes = "openid https://www.googleapis.com/auth/colaboratory"

    assert auth.COLAB_SCOPE in auth.granted_scopes(StrCreds())


def test_scope_changed_warning_becomes_friendly_error(tmp_path, monkeypatch):
    auth.install_client(str(client_json(tmp_path, "local")))

    class FakeFlow:
        @classmethod
        def from_client_config(cls, *a, **k):
            return cls()

        def run_local_server(self, **kwargs):
            raise Warning('Scope has changed from "openid colaboratory" to "openid".')

    import google_auth_oauthlib.flow as flow_mod

    monkeypatch.setattr(flow_mod, "InstalledAppFlow", FakeFlow)
    with pytest.raises(AuthError, match="全选"):
        auth.login_oauth()
