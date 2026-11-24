"""What the installer and the exe startup take for granted (without opening anything)."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def test_installer_widgets_match_the_panel():
    app = (ROOT / "web" / "hub" / "app.js").read_text(encoding="utf-8")
    panel = set(re.findall(r'(\w+): "', app.split("const WIDGETS = {")[1].split("};")[0]))
    iss = (ROOT / "installer" / "cortex-hub.iss").read_text(encoding="utf-8-sig")
    assert set(re.findall(r"AddWidget\('(\w+)'", iss)) == panel


def test_micmute_child_does_not_start_another_cortex():
    """In the exe, audio.py launches `CortexHub.exe -m hub.micmute`: if that started the app, it would relaunch endlessly."""
    code = ("import sys, types; sys.argv = ['CortexHub.exe', '-m', 'hub.micmute'];"
            "import cortex_hub; m = types.ModuleType('hub.micmute'); m.main = lambda: print('micmute');"
            "sys.modules['hub.micmute'] = m; cortex_hub.main();"
            "assert 'hub.app' not in sys.modules")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "micmute", r.stderr


def test_claude_status_without_claude_code(monkeypatch):
    from hub import brain
    monkeypatch.setattr(brain, "claude_exe", lambda: None)
    assert brain.claude_status() == {"installed": False, "logged": False}


def test_claude_status_hides_the_account(monkeypatch):
    from hub import brain
    monkeypatch.setattr(brain, "claude_exe", lambda: "claude")
    monkeypatch.setattr(brain.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout='{"loggedIn": true, "email": "a@example.com"}', stderr=""))
    assert brain.claude_status() == {"installed": True, "logged": True}
