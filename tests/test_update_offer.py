"""The update card: shown at start when a newer Forge is out, answered from the message box."""
import os
import pathlib
import sys
import tempfile

os.environ["FORGE_HOME"] = tempfile.mkdtemp()
os.environ["FORGE_NOVA_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from forgecli import cli, update  # noqa: E402
from forgecli.ui import UI  # noqa: E402


class FakeAgent:
    inbox = []
    messages = []

    class session:
        messages = []
        id = "x"


def make():
    app = cli.App.__new__(cli.App)
    app.agent, app.ui = FakeAgent(), UI()
    app.ui.docked = True
    app.goodbye = lambda: 0
    return app


def main():
    installed = []
    cli.App._install_update = lambda self, latest, cmd: installed.append(latest) or False
    update.how_installed = lambda: ("pip", ["pip", "install", "-U", "forgecommandline"])

    # docked: the card never blocks, the question sits in the message box
    app = make()
    assert app.offer_update("9.9.9") is True
    assert app.ui.pending and app.ui.pending["update"][0] == "9.9.9" and "y = yes" in app.ui.pending["label"]

    # anything but a yes or no keeps asking, a no lets you carry on, a yes installs and closes
    def drive(answers):
        it = iter(answers)
        def prompt():
            try:
                return next(it)
            except StopIteration:
                raise EOFError
        return prompt
    app._docked_loop(drive(["hello there", "/model", "n"]))
    assert app.ui.pending is None and not installed

    app = make()
    app.offer_update("9.9.9")
    assert app._docked_loop(drive(["", "never reached"])) == 0 and installed == ["9.9.9"]

    app = make()
    app.offer_update("9.9.9")
    installed.clear()
    app._docked_loop(drive(["yes please"]))
    assert installed == ["9.9.9"]
    app = make()
    app.offer_update("9.9.9")
    app._docked_loop(drive(["no thanks"]))
    assert app.ui.pending is None
    print("ok  update card: unmissable, never freezes the chat, plain y / n")

    # auto-update: arranged once, off with FORGE_AUTO_UPDATE=0, asks after repeated failures,
    # and the first start on the new version says so exactly once
    import importlib, json
    upd = importlib.reload(update)
    upd.enabled = lambda: True
    upd.how_installed = lambda: ("pip", [sys.executable, "-c", "pass"])
    spawned = []
    real_popen = upd.subprocess.Popen
    upd.subprocess.Popen = lambda *a, **k: spawned.append(a)
    os.environ["FORGE_AUTO_UPDATE"] = "0"
    assert not upd.auto_update("99.0.0") and not spawned
    os.environ["FORGE_AUTO_UPDATE"] = "1"
    assert upd.auto_update("99.0.0") and upd.auto_update("99.0.0") and len(spawned) == 1
    upd._scheduled.clear()
    assert upd.auto_update("99.0.0") and len(spawned) == 1     # this process already has it lined up
    upd._save({"auto": {"failed": "99.0.1", "fails": 2}})
    assert upd.auto_failing("99.0.1") and not upd.auto_update("99.0.1")
    upd.how_installed = lambda: ("exe", None)
    assert not upd.auto_update("99.0.2")
    upd.subprocess.Popen = real_popen
    upd._save({"installed": cli.__version__})
    assert upd.just_updated() == cli.__version__ and upd.just_updated() is None
    print("ok  auto-update: quiet, once, opt-out, falls back to asking")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
