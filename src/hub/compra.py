"""Shopping list: the "## Lista de esta semana" section of compra.md (in Drive), as Markdown checkboxes.

    - [ ] milk
    - [x] eggs

The rest of the file (stores, base list...) is left alone. Cortex (Claude) can edit the same section.
"""

import logging
import re
import threading

from . import datos

log = logging.getLogger("compra")

HEADING = "## Lista de esta semana"
ITEM = re.compile(r"^\s*[-*]\s*(\[( |x|X)\]\s*)?(.+?)\s*$")
_lock = threading.Lock()


def _path():
    return datos.folder() / "compra.md"


def _split(text: str) -> tuple[str, list[str], str]:
    """(everything before the section including its title, the section lines, whatever comes after)."""
    i = text.find(HEADING)
    if i < 0:
        return text.rstrip() + "\n\n" + HEADING + "\n", [], ""
    head_end = text.index("\n", i) + 1 if "\n" in text[i:] else len(text)
    nxt = re.search(r"^## ", text[head_end:], re.M)
    end = head_end + nxt.start() if nxt else len(text)
    return text[:head_end], text[head_end:end].splitlines(), text[end:]


def items() -> list[dict]:
    try:
        text = _path().read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in _split(text)[1]:
        m = ITEM.match(line)
        if m:
            out.append({"text": m.group(3), "done": (m.group(2) or " ").lower() == "x"})
    return out


def _write(new_items: list[dict]) -> None:
    path = _path()
    text = path.read_text(encoding="utf-8") if path.exists() else "# Compra\n"
    head, _, tail = _split(text)
    body = "\n" + "".join(f"- [{'x' if i['done'] else ' '}] {i['text']}\n" for i in new_items)
    if not new_items:
        body = "\n(empty)\n"
    path.write_text(head + body + ("\n" + tail if tail else ""), encoding="utf-8")


def add(text: str) -> list[dict]:
    with _lock:
        cur = items()
        names = [t.strip() for t in re.split(r",|\by\b|\band\b", text) if t.strip()]
        for name in names:
            if not any(i["text"].lower() == name.lower() for i in cur):
                cur.append({"text": name[:80], "done": False})
        _write(cur)
        return cur


def toggle(index: int) -> list[dict]:
    with _lock:
        cur = items()
        if 0 <= index < len(cur):
            cur[index]["done"] = not cur[index]["done"]
            _write(cur)
        return cur


def clear_done() -> list[dict]:
    """Remove what is already bought (checked)."""
    with _lock:
        cur = [i for i in items() if not i["done"]]
        _write(cur)
        return cur
