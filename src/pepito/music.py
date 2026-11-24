"""Órdenes de música resueltas en local (sin Claude): devuelve una intención o None."""

import re

from .spotify import norm

# «pon un recordatorio», «pon la tele»… no son música
NOT_MUSIC = re.compile(r"^(un|una|el|la|los|las)? ?(recordatorio|aviso|alarma|temporizador|tele|television|"
                       r"lavadora|luz|luces|nota|tarea|cita|evento|reminder|timer|alarm)\b")

PATTERNS: list[tuple[str, str]] = [
    # español
    (r"(pausa|pausala|para|paralo|parala|para la musica|pausa la musica|quita la musica|calla la musica|stop)", "pause"),
    (r"(sigue|continua|reanuda|dale al play|vuelve a poner la musica|pon la musica|play|resume|continue)", "resume"),
    (r"(siguiente|la siguiente|pasa|pasala|salta|saltala|siguiente cancion|pasa de cancion|cambia de cancion|"
     r"next|skip|next song)", "next"),
    (r"(anterior|la anterior|atras|cancion anterior|vuelve a la anterior|previous|go back|previous song)", "previous"),
    (r"(?:pon(?:lo|la)?|pasa(?:lo|la)?|cambia(?:lo|la)?|mueve(?:lo|la)?|que suene|suena|play it|move it|"
     r"switch it)(?: la musica| spotify)? (?:en|a|al|on|to) (?:el |la |mi |the |my )?(este dispositivo|este ordenador|"
     r"este portatil|aqui|el portatil|portatil|ordenador|this device|this computer|here|laptop|"
     r"echo(?: dot)?(?: .+)?|alexa|altavoz|cocina|kitchen|pc)", "device"),
    (r"(?:ponlo|ponla|pasalo|pasala|cambialo|cambiala|muevelo|muevela|move it|switch it)(?: la musica)? "
     r"(?:en|a|al|on|to) (?:el |la |mi |the |my )?(.+)", "device"),
    (r"(sube|subo|subele|sube el volumen|subo el volumen|subele el volumen|sube la musica|mas alto|mas volumen|volume up|louder|"
     r"turn it up)", "volume_up"),
    (r"(baja|bajo|bajale|baja el volumen|bajo el volumen|bajale el volumen|baja la musica|mas bajo|menos volumen|volume down|quieter|"
     r"turn it down)", "volume_down"),
    (r"(?:pon el |pon )?volumen (?:al |a )?(\d{1,3})(?: por ciento)?|(?:set )?volume (?:to )?(\d{1,3})(?: percent)?",
     "volume_set"),
    (r"(que (cancion )?(suena|es esto|esta sonando)|como se llama (esta|la) cancion|que estoy escuchando|"
     r"what(s| is) (playing|this song)|what song is this)", "now"),
    (r"(cuales son mis playlists|mis playlists|que playlists tengo|dime mis playlists|my playlists|"
     r"what playlists do i have)", "list"),
    (r"(?:pon|ponme|reproduce|play)(?: la| mi| my| the)? (?:playlist|lista)(?: de| del)? (.+)", "playlist"),
    (r"(?:pon|ponme|reproduce|play)(?: me)? (?:algo de|musica de|canciones de|some music by|music by|songs by|"
     r"some) (.+)", "artist"),
    (r"(?:pon|ponme|reproduce|play) (.+)", "query"),
]


def parse(text: str) -> dict | None:
    t = norm(text)
    t = re.sub(r"^(oye |hey |venga |porfa )?(cortex )?", "", t)
    t = re.sub(r" ?(por favor|porfa|please|cortex)$", "", t).strip()
    t = re.sub(r" (de|en|del|on|in) spotify$", "", t).strip()
    for pattern, intent in PATTERNS:
        m = re.fullmatch(pattern, t)
        if not m:
            continue
        if intent == "device":
            return {"intent": intent, "value": m.group(1)}
        if intent == "volume_set":
            return {"intent": intent, "value": int(m.group(1) or m.group(2))}
        if intent in ("playlist", "artist", "query"):
            arg = m.group(1).strip()
            if intent == "query" and (NOT_MUSIC.match(arg) or len(arg) < 2):
                return None
            # «pon la playlist X» mantiene el nombre tal cual lo dijo (con mayúsculas y tildes)
            return {"intent": intent, "value": _original_tail(text, arg)}
        return {"intent": intent}
    return None


def _original_tail(text: str, normalized_arg: str) -> str:
    """Recupera el trozo original (con tildes) que corresponde al argumento normalizado."""
    words = text.strip(" .,!?¿¡").split()
    n = len(normalized_arg.split())
    tail = " ".join(words[-n:]) if n <= len(words) else normalized_arg
    tail = re.sub(r"[,.!?¿¡]+$", "", tail).strip()
    return tail if norm(tail) == normalized_arg else normalized_arg
