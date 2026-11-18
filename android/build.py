"""Build the Forge Remote APK (the phone app in docs/app inside a small WebView shell).

No Gradle: aapt2 + javac + d8 + zipalign + apksigner from the Android SDK, so it builds in seconds
locally and in CI.

    python android/build.py                     # → android/build/forge-remote.apk

Signing: FORGE_KEYSTORE (path to a .jks), FORGE_KEYSTORE_PASS and FORGE_KEY_ALIAS (default "forge").
Without them a throwaway debug key is made, fine for trying it but phones won't take it as an update
of a release-signed install.
"""
from __future__ import annotations

import glob
import os
import pathlib
import re
import shutil
import subprocess
import sys
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "build"
WIN = os.name == "nt"


def sdk() -> pathlib.Path:
    for v in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(v) and pathlib.Path(os.environ[v]).is_dir():
            return pathlib.Path(os.environ[v])
    for p in (pathlib.Path.home() / "AppData/Local/Android/Sdk", pathlib.Path.home() / "Library/Android/sdk",
              pathlib.Path.home() / "Android/Sdk"):
        if p.is_dir():
            return p
    sys.exit("Android SDK not found: set ANDROID_HOME")


def newest(d: pathlib.Path) -> pathlib.Path:
    def key(p):
        return [int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", p.name.replace("android-", ""))]
    found = sorted((p for p in d.iterdir() if p.is_dir()), key=key)
    if not found:
        sys.exit(f"nothing in {d}")
    return found[-1]


def tool(bt: pathlib.Path, name: str) -> str:
    for ext in ((".bat", ".exe", "") if WIN else ("",)):
        p = bt / (name + ext)
        if p.exists():
            return str(p)
    sys.exit(f"{name} missing from {bt}")


def run(*cmd) -> None:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"failed: {' '.join(map(str, cmd))}\n{r.stdout}\n{r.stderr}")


def version() -> tuple[str, int]:
    v = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "forgecli/__init__.py").read_text()).group(1)
    nums = [int(x) for x in re.findall(r"\d+", v)[:3]] + [0, 0, 0]
    return v, nums[0] * 10000 + nums[1] * 100 + nums[2]


def main() -> None:
    base = sdk()
    bt = newest(base / "build-tools")
    jar = newest(base / "platforms") / "android.jar"
    name, code = version()
    shutil.rmtree(OUT, ignore_errors=True)
    for d in ("assets", "gen", "classes", "dex"):
        (OUT / d).mkdir(parents=True)

    # the web app, as the APK's assets (the service worker isn't needed inside the app)
    shutil.copytree(ROOT / "docs/app", OUT / "assets/app", ignore=shutil.ignore_patterns("sw.js"))

    run(tool(bt, "aapt2"), "compile", "--dir", HERE / "res", "-o", OUT / "res.zip")
    run(tool(bt, "aapt2"), "link", "-o", OUT / "base.apk", "-I", jar, "--manifest", HERE / "AndroidManifest.xml",
        "-A", OUT / "assets", "--java", OUT / "gen", "--version-code", code, "--version-name", name,
        "--min-sdk-version", "24", "--target-sdk-version", "34", "--auto-add-overlay", OUT / "res.zip")

    sources = glob.glob(str(HERE / "src/**/*.java"), recursive=True) + glob.glob(str(OUT / "gen/**/*.java"), recursive=True)
    run("javac", "-nowarn", "-Xlint:-options", "-source", "8", "-target", "8", "-encoding", "utf-8",
        "-bootclasspath", jar, "-classpath", jar, "-d", OUT / "classes", *sources)
    classes = glob.glob(str(OUT / "classes/**/*.class"), recursive=True)
    run(tool(bt, "d8"), "--release", "--min-api", "24", "--lib", jar, "--output", OUT / "dex", *classes)

    with zipfile.ZipFile(OUT / "base.apk", "a", zipfile.ZIP_DEFLATED) as z:
        z.write(OUT / "dex/classes.dex", "classes.dex")
    run(tool(bt, "zipalign"), "-f", "-p", "4", OUT / "base.apk", OUT / "aligned.apk")

    ks, pw = os.environ.get("FORGE_KEYSTORE"), os.environ.get("FORGE_KEYSTORE_PASS")
    alias = os.environ.get("FORGE_KEY_ALIAS", "forge")
    if not (ks and pw and os.path.exists(ks)):
        ks, pw, alias = str(OUT / "debug.jks"), "android", "debug"
        run("keytool", "-genkeypair", "-keystore", ks, "-storepass", pw, "-keypass", pw, "-alias", alias,
            "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000", "-dname", "CN=Forge Debug")
        print("note: signed with a throwaway debug key (set FORGE_KEYSTORE / FORGE_KEYSTORE_PASS to release-sign)")
    apk = OUT / "forge-remote.apk"
    run(tool(bt, "apksigner"), "sign", "--ks", ks, "--ks-pass", f"pass:{pw}", "--key-pass", f"pass:{pw}",
        "--ks-key-alias", alias, "--out", apk, OUT / "aligned.apk")
    run(tool(bt, "apksigner"), "verify", apk)
    print(f"{apk}  ({apk.stat().st_size // 1024} KB, v{name} / {code})")


if __name__ == "__main__":
    main()
