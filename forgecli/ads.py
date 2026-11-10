"""One sponsor line, shown on the goodbye screen, under the rules in HANDOFF.md (NEXT 8).

- Off unless an ad server is configured (FORGE_ADS_URL, or AD_SERVER below once Harry runs one) and
  its public key is known (FORGE_ADS_KEY / AD_PUBLIC_KEY). Users turn it off with FORGE_ADS=0 or
  `/ads off`. Never in pipes, scripts, CI or `forge -p` runs, and not in the first week after install.
- The request carries only Forge's version and a random install id (`/ads reset` makes a new one):
  no prompts, code, paths, keys or project details.
- Fetched in the background with a 0.5 s timeout; slow or down server = no ad, never a slower reply.
- Every ad is signed with Harry's Ed25519 key and rejected otherwise, so whoever takes over the
  server's address can't show their own lines. The text is reduced to plain printable words and one
  https link, so an ad can't clear the screen, move the cursor, fake a Forge message or hide its link.
- An ad is display text only: it never reaches the model, a tool or the shell.

`python -m forgecli.ads keygen` makes the key pair for the server (see server/ads-worker/README.md).
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

AD_SERVER = ""          # e.g. "https://ads.forge-cli.dev" once deployed
AD_PUBLIC_KEY = ""      # hex Ed25519 public key printed by `python -m forgecli.ads keygen`
TIMEOUT = 0.5
QUIET_DAYS = 7
MAX_TEXT, MAX_URL = 90, 100


def _home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("FORGE_HOME", pathlib.Path.home() / ".forge"))


def _state_path() -> pathlib.Path:
    return _home() / "ads.json"


def load_state() -> dict:
    try:
        st = json.loads(_state_path().read_text(encoding="utf-8"))
        if isinstance(st, dict):
            return st
    except (OSError, ValueError):
        pass
    return {}


def save_state(st: dict) -> None:
    try:
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps(st, indent=1), encoding="utf-8")
    except OSError:
        pass


def install_id(st: dict) -> str:
    if not st.get("id"):
        st["id"] = secrets.token_hex(12)
        st.setdefault("since", time.time())
        save_state(st)
    return st["id"]


def server() -> str:
    return (os.environ.get("FORGE_ADS_URL") or AD_SERVER).strip().rstrip("/")


def public_key() -> str:
    return (os.environ.get("FORGE_ADS_KEY") or AD_PUBLIC_KEY).strip().lower()


def user_off(st: dict | None = None) -> bool:
    env = os.environ.get("FORGE_ADS", "").strip().lower()
    if env in ("0", "off", "false", "no"):
        return True
    return (st if st is not None else load_state()).get("off") is True


def allowed(st: dict | None = None, interactive: bool | None = None) -> tuple[bool, str]:
    """(may we show an ad, why not)."""
    st = st if st is not None else load_state()
    if user_off(st):
        return False, "turned off"
    if not server() or not public_key():
        return False, "no ad server configured"
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive or os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return False, "not an interactive terminal"
    since = st.get("since") or time.time()
    if time.time() - since < QUIET_DAYS * 86400:
        return False, f"first {QUIET_DAYS} days after install"
    return True, ""


# --- cleaning --------------------------------------------------------------------------------------

_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A)) | {0x200E, 0x200F, 0x061C}


def clean_text(text: str) -> str:
    """Printable words only: no escape codes, control or format characters, bidi tricks or newlines."""
    out = []
    raw = str(text or "")
    raw = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)?", " ", raw)        # OSC (titles, hyperlinks)
    raw = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", " ", raw)                  # CSI (cursor, colour, clear)
    raw = re.sub(r"\x1b[PX^_][^\x1b]*(\x1b\\)?", " ", raw)               # DCS/SOS/PM/APC (sixel etc.)
    raw = re.sub(r"\x1b.?", " ", raw)
    raw = re.sub(r"\x9b[0-?]*[ -/]*[@-~]", " ", raw)                    # 8-bit CSI
    for ch in unicodedata.normalize("NFKC", raw):
        cat = unicodedata.category(ch)
        if ord(ch) in _BIDI or cat in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            out.append(" ")
        else:
            out.append(ch)
    s = re.sub(r"\s+", " ", "".join(out)).strip()
    return s if len(s) <= MAX_TEXT else s[:MAX_TEXT - 1].rstrip() + "…"


def clean_url(url: str) -> str:
    """One plain https link a person can read; anything else is dropped."""
    u = str(url or "").strip()
    if len(u) > MAX_URL or not re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d+)?(/[A-Za-z0-9._~/%?=&+\-#]*)?", u):
        return ""
    host = urllib.parse.urlparse(u).hostname or ""
    if "xn--" in host or "." not in host:
        return ""
    return u


# --- signatures (pure-Python Ed25519, RFC 8032) -----------------------------------------------------

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _inv(x):
    return pow(x, _P - 2, _P)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P:
        x = x * _I % _P
    if (x * x - xx) % _P:
        raise ValueError("not on curve")
    return _P - x if x & 1 else x


_BY = 4 * _inv(5) % _P
_BX = _xrecover(_BY)
_B = (_BX, _BY, 1, _BX * _BY % _P)


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _enc(p):
    zi = _inv(p[2])
    x, y = p[0] * zi % _P, p[1] * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _dec(b):
    y = int.from_bytes(b, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    if y >= _P:
        raise ValueError("bad point")
    x = _xrecover(y)
    if (x & 1) != sign:
        if x == 0:
            raise ValueError("bad point")
        x = _P - x
    return (x, y, 1, x * y % _P)


def _h(m):
    return hashlib.sha512(m).digest()


def _secret(seed: bytes):
    h = _h(seed)
    a = int.from_bytes(h[:32], "little")
    a = (a & ((1 << 254) - 8)) | (1 << 254)
    return a, h[32:]


def public_from_seed(seed: bytes) -> bytes:
    return _enc(_mul(_secret(seed)[0], _B))


def sign(seed: bytes, msg: bytes) -> bytes:
    a, prefix = _secret(seed)
    pub = _enc(_mul(a, _B))
    r = int.from_bytes(_h(prefix + msg), "little") % _L
    rb = _enc(_mul(r, _B))
    k = int.from_bytes(_h(rb + pub + msg), "little") % _L
    return rb + ((r + k * a) % _L).to_bytes(32, "little")


def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pub) != 32 or len(sig) != 64:
        return False
    try:
        a, r = _dec(pub), _dec(sig[:32])
    except ValueError:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _L:
        return False
    k = int.from_bytes(_h(sig[:32] + pub + msg), "little") % _L
    return _enc(_mul(s, _B)) == _enc(_add(r, _mul(k, a)))


def signed_message(ad: dict) -> bytes:
    """Exactly what the server signs (server/ads-worker/worker.js builds the same string)."""
    return "\n".join(str(ad.get(k, "")) for k in ("id", "text", "url", "exp")).encode("utf-8")


def check(ad: dict, key_hex: str | None = None, now: float | None = None) -> dict | None:
    """A signed, unexpired, cleaned ad, or None."""
    try:
        key = bytes.fromhex(key_hex if key_hex is not None else public_key())
        sig = bytes.fromhex(str(ad.get("sig", "")))
        exp = int(ad.get("exp", 0))
    except (ValueError, TypeError, AttributeError):
        return None
    if exp < (now or time.time()) or not verify(key, signed_message(ad), sig):
        return None
    text, url = clean_text(ad.get("text", "")), clean_url(ad.get("url", ""))
    if not text:
        return None
    return {"id": str(ad.get("id", ""))[:40], "text": text, "url": url, "exp": exp}


# --- fetching and showing ---------------------------------------------------------------------------

class Ads:
    """Starts one background fetch per session; `line()` gives the goodbye line, if any."""

    def __init__(self, version: str, interactive: bool | None = None):
        self.version = version
        self.st = load_state()
        install_id(self.st)
        self.ok, self.why = allowed(self.st, interactive)
        self.ad: dict | None = None
        self._thread: threading.Thread | None = None
        if self.ok:
            self._thread = threading.Thread(target=self._fetch, daemon=True)
            self._thread.start()

    def _fetch(self) -> None:
        q = urllib.parse.urlencode({"v": self.version, "id": self.st["id"]})
        try:
            req = urllib.request.Request(f"{server()}/v1/ad?{q}", headers={"User-Agent": "forge-cli"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read(4096).decode("utf-8", "replace"))
            ad = check(data) if isinstance(data, dict) and data.get("id") else None
            if ad:
                self.st["cached"] = ad
                save_state(self.st)
            self.ad = ad
        except Exception:
            self.ad = None      # offline or slow: fall back to the cached ad below

    def line(self) -> str | None:
        if not self.ok:
            return None
        if self._thread is not None:
            self._thread.join(timeout=0.05)     # never hold up quitting
        ad = self.ad
        if ad is None:
            cached = self.st.get("cached")
            ad = check(cached) if isinstance(cached, dict) and cached.get("sig") else None
        if ad is None:
            return None
        return f"sponsor · {ad['text']}" + (f"  {ad['url']}" if ad["url"] else "")


def keygen() -> None:
    import base64
    seed = secrets.token_bytes(32)
    pub = public_from_seed(seed)
    pkcs8 = bytes.fromhex("302e020100300506032b657004220420") + seed
    print("PUBLIC key (put in forgecli/ads.py AD_PUBLIC_KEY, or FORGE_ADS_KEY):")
    print("  " + pub.hex())
    print("PRIVATE key for the Worker (wrangler secret put AD_SIGNING_KEY) - keep it secret, never commit:")
    print("  " + base64.b64encode(pkcs8).decode())


if __name__ == "__main__":
    if sys.argv[1:2] == ["keygen"]:
        keygen()
    else:
        print("usage: python -m forgecli.ads keygen")
