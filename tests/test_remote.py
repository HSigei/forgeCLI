"""Phone remote (encryption, pairing, mirroring) and the self-installing Windows build."""
import json
import os
import pathlib
import sys
import tempfile
import time

os.environ["FORGE_HOME"] = tempfile.mkdtemp()
os.environ["FORGE_NOVA_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from forgecli import remote, update, winsetup  # noqa: E402


def test_crypto():
    s, other = remote._b64(os.urandom(32)), remote._b64(os.urandom(32))
    key = remote.key_of(s)
    frame = remote.seal(key, {"t": "say", "text": "héllo"})
    assert set(json.loads(frame)) == {"n", "c"} and "héllo" not in frame
    assert remote.unseal(key, frame)["text"] == "héllo"
    assert remote.unseal(remote.key_of(other), frame) is None      # wrong key: dropped
    assert remote.room_of(s) != remote.room_of(other) and len(remote.room_of(s)) == 48
    assert remote.room_of(s) not in key.hex()                      # room id doesn't give away the key
    real = remote.time.time
    remote.time.time = lambda: real() - remote.MAX_AGE - 5
    old = remote.seal(key, {"t": "say", "text": "x"})
    remote.time.time = real
    assert remote.unseal(key, old) is None                         # replayed / stale: dropped
    assert remote.unseal(key, '{"relay": "peer", "phones": 1}')["t"] == "relay"


def test_pairing():
    a = remote.load_secret()
    assert remote.load_secret() == a                               # stable between runs
    p = remote._path()
    assert str(p).startswith(os.environ["FORGE_HOME"])               # never inside the code folder
    b = remote.load_secret(new=True)
    assert b != a and remote.load_secret() == b                     # /remote new unpairs old phones
    assert remote.pair_link(b).endswith("#pair=" + b)


def test_windows_install_rules():
    real = winsetup.frozen
    winsetup.frozen = lambda: False
    assert not winsetup.wanted([]) and not winsetup.wanted(["--install"])   # never from Python
    winsetup.frozen = lambda: True
    exe = sys.executable
    try:
        sys.executable = r"C:\Users\x\Downloads\ForgeSetup (1).exe"
        assert winsetup.wanted([])                                  # double-clicked download
        assert not winsetup.wanted(["fix the tests"])               # a prompt, not an install
        sys.executable = r"C:\tools\forge.exe"
        assert not winsetup.wanted([]) and winsetup.wanted(["--install", "--quiet"])
        assert winsetup.wanted(["--uninstall"])
    finally:
        sys.executable, winsetup.frozen = exe, real
    cmd = winsetup.setup_cmd(1234)
    assert cmd[0] == "powershell" and "Wait-Process -Id 1234" in cmd[-1] and "--quiet" in cmd[-1]
    assert winsetup.SETUP_URL.endswith("/ForgeSetup.exe") and winsetup.SETUP_URL in cmd[-1]
    os.environ["LOCALAPPDATA"] = r"C:\L"
    assert str(winsetup.install_dir()).replace("/", "\\") == r"C:\L\Programs\Forge"


def test_exe_updates_itself():
    frozen = getattr(sys, "frozen", None)
    real = winsetup.installed_here
    try:
        sys.frozen = True
        winsetup.installed_here = lambda: True
        kind, cmd = update.how_installed()
        assert kind == "exe" and cmd and "ForgeSetup.exe" in cmd[-1]
        winsetup.installed_here = lambda: False                     # a loose forge.exe: just say where
        assert update.how_installed() == ("exe", None)
    finally:
        winsetup.installed_here = real
        if frozen is None:
            del sys.frozen
        else:
            sys.frozen = frozen


def test_record():
    winsetup._record(update=False)
    st = json.loads(update._path().read_text(encoding="utf-8"))
    assert st["installed"] == st["announced"]                        # fresh install: no "Updated to" greeting
    st.pop("announced")
    update._path().write_text(json.dumps(st), encoding="utf-8")
    winsetup._record(update=True)
    assert update.just_updated() == st["installed"]                  # quiet update: greet once


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL PASS")
