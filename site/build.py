"""Copia el panel (web/) a la demo de la web: `python site/build.py`. El resultado va al repositorio porque Vercel
publica site/ tal cual (sin compilar); tests/test_site.py falla si se queda atrás."""

import json
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent
sys.path.insert(0, str(ROOT / "src"))

PRIVATE = {"data_dir": "", "user_name": "", "mic_device": None, "speaker_device": None}


def outputs() -> dict[Path, bytes]:
    """Archivo generado → contenido. Siempre con saltos \\n, para que no dependa de git en Windows."""
    from pepito.config import DEFAULTS
    text = lambda p: p.read_bytes().replace(b"\r\n", b"\n")  # noqa: E731
    out = {SITE / "static" / p.relative_to(ROOT / "web"): text(p)
           for p in (ROOT / "web").rglob("*") if p.is_file() and p.name != "index.html"}
    html = text(ROOT / "web" / "index.html").decode("utf-8")
    first = '<script src="/static/vendor/gridstack-all.js"></script>'
    assert html.count(first) == 1 and html.count("<title>Cortex</title>") == 1
    html = html.replace(first, '<script src="/demo/defaults.js"></script>\n<script src="/demo/mock.js"></script>\n' + first)
    html = html.replace("<title>Cortex</title>", "<title>Cortex · demo web</title>\n<meta name=\"robots\" content=\"noindex\">")
    out[SITE / "demo" / "index.html"] = html.encode("utf-8")
    defaults = json.dumps({**DEFAULTS, **PRIVATE}, ensure_ascii=False, indent=1)
    out[SITE / "demo" / "defaults.js"] = f"window.CORTEX_DEFAULTS = {defaults};\n".encode("utf-8")
    return out


if __name__ == "__main__":
    for path, data in outputs().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(path.relative_to(ROOT))
