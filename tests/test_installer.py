"""Lo que el instalador y el arranque del exe dan por hecho (sin abrir nada)."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def test_installer_widgets_match_the_panel():
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    panel = set(re.findall(r'(\w+): "', app.split("const WIDGETS = {")[1].split("};")[0]))
    iss = (ROOT / "installer" / "cortex.iss").read_text(encoding="utf-8-sig")
    assert set(re.findall(r"AddWidget\('(\w+)'", iss)) == panel


def test_micmute_child_does_not_start_another_cortex():
    """En el exe, audio.py lanza `Cortex.exe -m pepito.micmute`: si eso arrancara la app, se relanzaría sin fin."""
    code = ("import sys, types; sys.argv = ['Cortex.exe', '-m', 'pepito.micmute'];"
            "m = types.ModuleType('pepito.micmute'); m.main = lambda: print('micmute');"
            "sys.modules['pepito.micmute'] = m; import cortex; cortex.main();"
            "assert 'pepito.app' not in sys.modules")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "micmute", r.stderr


def test_claude_status_without_claude_code(monkeypatch):
    from pepito import brain
    monkeypatch.setattr(brain, "claude_exe", lambda: None)
    assert brain.claude_status() == {"installed": False, "logged": False}


def test_claude_status_hides_the_account(monkeypatch):
    from pepito import brain
    monkeypatch.setattr(brain, "claude_exe", lambda: "claude")
    monkeypatch.setattr(brain.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout='{"loggedIn": true, "email": "a@example.com"}', stderr=""))
    assert brain.claude_status() == {"installed": True, "logged": True}
