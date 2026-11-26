"""Embed the project's OAuth client into a release build (run by CI before `uv build`).

Reads the Desktop-app client JSON from the GPUDOCK_OAUTH_CLIENT_JSON environment
variable (a CI secret holding the file's contents) and writes it to
packages/core/src/gpudock_core/_oauth_client.json, which is gitignored and
listed under hatch `artifacts`, so it ends up in the wheel but never in git.

    GPUDOCK_OAUTH_CLIENT_JSON="$(cat client_secret.json)" python scripts/embed_oauth_client.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "packages/core/src/gpudock_core/_oauth_client.json"


def main() -> int:
    raw = os.environ.get("GPUDOCK_OAUTH_CLIENT_JSON")
    if not raw:
        print("GPUDOCK_OAUTH_CLIENT_JSON is not set; building without a bundled client.", file=sys.stderr)
        return 1
    data = json.loads(raw)
    installed = data.get("installed") or {}
    if not installed.get("client_id") or not installed.get("client_secret"):
        print("GPUDOCK_OAUTH_CLIENT_JSON must be a Desktop-app client JSON (has 'installed').", file=sys.stderr)
        return 1
    # Keep only what the installed-app flow needs.
    slim = {
        "installed": {
            "client_id": installed["client_id"],
            "client_secret": installed["client_secret"],
            "auth_uri": installed.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
            "token_uri": installed.get("token_uri", "https://oauth2.googleapis.com/token"),
            "redirect_uris": ["http://localhost"],
        }
    }
    TARGET.write_text(json.dumps(slim, indent=2))
    print(f"wrote {TARGET.relative_to(Path.cwd()) if TARGET.is_relative_to(Path.cwd()) else TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
