"""Cuentas de webs de vídeo (usuario y contraseña) para que Pepito inicie sesión solo.

Charlie las escribe en el panel (Ajustes → Vídeo). La contraseña va al Administrador de credenciales de Windows
(cifrada con su usuario de Windows); aquí solo queda qué web y qué usuario, en secrets/ (fuera de Drive y de git).
"""

import json
import logging
import threading

import keyring

from .config import ROOT

log = logging.getLogger("webaccounts")

INDEX = ROOT / "secrets" / "web_accounts.json"
SERVICE = "Pepito web: {}"
_lock = threading.Lock()


def _domain(site: str) -> str:
    s = site.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0].removeprefix("www.")
    return ".".join(s.split(".")[-2:])


def _load() -> dict[str, str]:
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def accounts() -> list[dict]:
    """[{site, user}] (sin contraseñas): lo que enseña el panel."""
    return [{"site": d, "user": u} for d, u in sorted(_load().items())]


def save(site: str, user: str, password: str) -> str:
    domain = _domain(site)
    if "." not in domain or not user or not password:
        raise ValueError("Falta la web, el usuario o la contraseña.")
    with _lock:
        keyring.set_password(SERVICE.format(domain), user, password)
        data = _load()
        data[domain] = user
        INDEX.parent.mkdir(exist_ok=True)
        INDEX.write_text(json.dumps(data, indent=1), encoding="utf-8")
    log.info("Cuenta guardada para %s", domain)
    return domain


def delete(site: str) -> None:
    domain = _domain(site)
    with _lock:
        data = _load()
        user = data.pop(domain, None)
        if user:
            try:
                keyring.delete_password(SERVICE.format(domain), user)
            except keyring.errors.PasswordDeleteError:
                pass
        INDEX.write_text(json.dumps(data, indent=1), encoding="utf-8")


def get(host: str) -> tuple[str, str] | None:
    """(usuario, contraseña) para esa web (o None)."""
    domain = _domain(host)
    user = _load().get(domain)
    if not user:
        return None
    password = keyring.get_password(SERVICE.format(domain), user)
    return (user, password) if password else None
