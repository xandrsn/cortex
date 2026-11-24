"""La demo web: copia del panel al día y lógica (con node, sin abrir ningún navegador)."""

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_site_copy_is_up_to_date():
    spec = importlib.util.spec_from_file_location("site_build", ROOT / "site" / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    stale = [str(p.relative_to(ROOT)) for p, data in build.outputs().items()
             if not p.exists() or p.read_bytes().replace(b"\r\n", b"\n") != data]
    assert not stale, f"ejecuta python site/build.py: {stale}"


@pytest.mark.skipif(not shutil.which("node"), reason="sin node")
def test_demo_logic():
    r = subprocess.run(["node", str(ROOT / "tests" / "site.test.mjs")], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
