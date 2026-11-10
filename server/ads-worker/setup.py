"""One-command setup of the sponsor-line Worker on your Cloudflare account.

Run from the forge-cli folder after `npx wrangler login`:

    python server/ads-worker/setup.py

It creates the D1 database, loads the schema, makes a signing key pair, stores the private key and an
admin token as Worker secrets (never on disk in the repo), deploys, and writes the Worker URL and the
public key into forgecli/ads.py. The admin token is saved to ~/.forge/ads-admin-token.txt for you.
Safe to re-run: an existing database is reused.
"""
import base64
import pathlib
import re
import secrets
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
from forgecli.ads import public_from_seed  # noqa: E402

NPX = shutil.which("npx") or shutil.which("npx.cmd") or "npx"


def wrangler(*args, stdin=None, check=True):
    r = subprocess.run([NPX, "--yes", "wrangler", *args], cwd=HERE, input=stdin,
                       text=True, capture_output=True)
    out = (r.stdout or "") + (r.stderr or "")
    if check and r.returncode != 0:
        sys.exit(f"wrangler {' '.join(args)} failed:\n{out[-1500:]}")
    return out


def main():
    who = wrangler("whoami", check=False)
    if "not authenticated" in who.lower() or "You are not" in who:
        sys.exit("Not logged in to Cloudflare. Run:  npx wrangler login   then run this again.")

    toml = HERE / "wrangler.toml"
    text = toml.read_text(encoding="utf-8")
    if "PASTE-THE-ID" in text:
        out = wrangler("d1", "create", "forge-ads", check=False)
        m = re.search(r'database_id\s*[=:]\s*"?([0-9a-f-]{36})', out)
        if not m:  # already exists: look it up
            m = re.search(r"([0-9a-f-]{36})\s*[│|]\s*forge-ads\b", wrangler("d1", "list"))
        if not m:
            sys.exit("Could not get the D1 database id:\n" + out[-1500:])
        text = text.replace("PASTE-THE-ID-FROM-wrangler-d1-create", m.group(1))
        toml.write_text(text, encoding="utf-8")
        print("database ready:", m.group(1))

    wrangler("d1", "execute", "forge-ads", "--remote", "--yes", "--file=schema.sql")
    print("schema loaded")

    seed = secrets.token_bytes(32)
    pub = public_from_seed(seed).hex()
    pkcs8 = base64.b64encode(bytes.fromhex("302e020100300506032b657004220420") + seed).decode()
    admin = secrets.token_urlsafe(32)
    wrangler("deploy")  # the Worker must exist before secrets can be attached
    wrangler("secret", "put", "AD_SIGNING_KEY", stdin=pkcs8)
    wrangler("secret", "put", "ADMIN_TOKEN", stdin=admin)
    out = wrangler("deploy")
    m = re.search(r"https://[\w.-]+\.workers\.dev", out)
    if not m:
        sys.exit("Deployed, but could not read the Worker URL:\n" + out[-1500:])
    url = m.group(0)

    tok = pathlib.Path.home() / ".forge" / "ads-admin-token.txt"
    tok.parent.mkdir(parents=True, exist_ok=True)
    tok.write_text(admin + "\n", encoding="utf-8")

    ads = ROOT / "forgecli" / "ads.py"
    src = ads.read_text(encoding="utf-8")
    src = re.sub(r'^AD_SERVER = ".*?"', f'AD_SERVER = "{url}"', src, count=1, flags=re.M)
    src = re.sub(r'^AD_PUBLIC_KEY = ".*?"', f'AD_PUBLIC_KEY = "{pub}"', src, count=1, flags=re.M)
    ads.write_text(src, encoding="utf-8")

    print("Worker live at", url)
    print("forgecli/ads.py now points at it (public key", pub[:12] + "…)")
    print("admin token saved to", tok)
    print("Sponsor lines start showing to users after the next Forge release, once you add a sponsor.")


if __name__ == "__main__":
    main()
