"""Sign in to GitHub from inside Forge with GitHub's device flow, without the gh CLI: Forge shows a
short code, you enter it at github.com/login/device, and the token is saved in the key store
(Windows Credential Manager / macOS Keychain when available).

Needs a GitHub OAuth App's client id with device flow enabled: FORGE_GITHUB_CLIENT_ID, or the
CLIENT_ID constant below once the Forge project registers one. A client id is public; no secret is
used or needed."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = ""                      # set when the Forge OAuth App is registered
SCOPES = "repo read:org workflow"
CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"


def client_id() -> str:
    return os.environ.get("FORGE_GITHUB_CLIENT_ID", "").strip() or CLIENT_ID


def _post(url: str, fields: dict, timeout: float = 15) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Accept": "application/json", "User-Agent": "forge-cli"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def start(cid: str, post=_post) -> dict:
    """Ask GitHub for a device code. Returns {device_code, user_code, verification_uri, interval,
    expires_in} or raises RuntimeError with a plain message."""
    data = post(CODE_URL, {"client_id": cid, "scope": SCOPES})
    if "device_code" not in data:
        raise RuntimeError(data.get("error_description") or data.get("error") or "GitHub refused the request")
    return data


def poll(cid: str, device: dict, post=_post, sleep=time.sleep, cancelled=lambda: False) -> str:
    """Wait for the user to approve; returns the access token or raises RuntimeError."""
    interval = int(device.get("interval", 5) or 5)
    deadline = time.time() + int(device.get("expires_in", 900) or 900)
    while time.time() < deadline:
        if cancelled():
            raise RuntimeError("sign-in cancelled")
        sleep(interval)
        data = post(TOKEN_URL, {"client_id": cid, "device_code": device["device_code"],
                                "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if data.get("access_token"):
            return data["access_token"]
        err = data.get("error", "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval = int(data.get("interval", interval + 5))
            continue
        if err == "expired_token":
            raise RuntimeError("the code expired; run /github login again")
        if err == "access_denied":
            raise RuntimeError("you declined access on GitHub")
        raise RuntimeError(data.get("error_description") or err or "unexpected reply from GitHub")
    raise RuntimeError("the code expired; run /github login again")


def whoami(token: str, timeout: float = 10) -> str:
    req = urllib.request.Request("https://api.github.com/user", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "forge-cli"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("login", "")
    except (urllib.error.URLError, OSError, ValueError):
        return ""
