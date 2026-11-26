"""Reach Google through the Windows system proxy, as the browser does.

Python reads the Windows proxy from the registry only when no ``*_proxy``
environment variable exists at all, so one unrelated variable (say
``SOME_TOOL_PROXY``) silently turns it off. The browser still goes through the
proxy and the Google sign-in page works, but exchanging the code for a token
then times out. Filling HTTPS_PROXY / HTTP_PROXY from the registry when they
are unset fixes that; an explicit setting always wins.
"""

from __future__ import annotations

import os
import urllib.request

LOCAL = "localhost,127.0.0.1,::1"


def use_system_proxy() -> str | None:
    """Copy the Windows system proxy into the environment if none is set. Returns it."""
    if os.name != "nt":
        return None
    if any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")):
        return None
    try:
        reg = urllib.request.getproxies_registry()
    except OSError:
        return None
    if not reg.get("https") and not reg.get("http"):
        return None
    for scheme in ("https", "http"):
        value = reg.get(scheme) or reg.get("http")
        if value and not (os.environ.get(f"{scheme.upper()}_PROXY") or os.environ.get(f"{scheme}_proxy")):
            os.environ[f"{scheme.upper()}_PROXY"] = value
    if not (os.environ.get("NO_PROXY") or os.environ.get("no_proxy")):
        os.environ["NO_PROXY"] = LOCAL
    return os.environ["HTTPS_PROXY"]
