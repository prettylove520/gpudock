import os
import urllib.request

from gpudock_core import proxy

KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")


def _clean(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)


def test_fills_proxy_from_windows_registry_when_an_unrelated_var_hides_it(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setattr(proxy.os, "name", "nt")
    # an unrelated *_PROXY variable is what makes urllib skip the registry
    monkeypatch.setenv("SOME_TOOL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setattr(urllib.request, "getproxies_registry", lambda: {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}, raising=False)
    assert proxy.use_system_proxy() == "http://127.0.0.1:7897"
    assert os.environ["HTTPS_PROXY"] == os.environ["HTTP_PROXY"] == "http://127.0.0.1:7897"
    assert "127.0.0.1" in os.environ["NO_PROXY"]


def test_explicit_proxy_wins_and_non_windows_is_untouched(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setattr(proxy.os, "name", "nt")
    monkeypatch.setattr(urllib.request, "getproxies_registry", lambda: {"https": "http://127.0.0.1:7897"}, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://corp:3128")
    assert proxy.use_system_proxy() is None
    assert os.environ["HTTPS_PROXY"] == "http://corp:3128"

    _clean(monkeypatch)
    monkeypatch.setattr(proxy.os, "name", "posix")
    assert proxy.use_system_proxy() is None
    assert "HTTPS_PROXY" not in os.environ


def test_no_system_proxy_changes_nothing(monkeypatch):
    _clean(monkeypatch)
    monkeypatch.setattr(proxy.os, "name", "nt")
    monkeypatch.setattr(urllib.request, "getproxies_registry", lambda: {}, raising=False)
    assert proxy.use_system_proxy() is None
    assert "HTTPS_PROXY" not in os.environ and "NO_PROXY" not in os.environ
