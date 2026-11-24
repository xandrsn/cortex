"""Router local: resuelve sin Claude las órdenes simples. Devuelve la respuesta o None."""

import re
from datetime import datetime

DAYS = {"es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
        "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}
MONTHS = {"es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
                 "octubre", "noviembre", "diciembre"],
          "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                 "October", "November", "December"]}


def _norm(text: str) -> str:
    text = text.lower()
    for a, b in zip("áéíóúü", "aeiouu"):
        text = text.replace(a, b)
    return re.sub(r"[^\w\s]", "", text).strip()


def route(text: str, lang: str) -> str | None:
    t = _norm(text)
    now = datetime.now()
    if re.fullmatch(r"(cortex )?(que hora es|me dices la hora|dime la hora|la hora)( cortex)?", t):
        return f"Son las {now:%H:%M}."
    if re.fullmatch(r"(hey )?(cortex )?what time is it( cortex)?", t):
        return f"It's {now:%H:%M}."
    if re.fullmatch(r"(cortex )?(que dia es hoy|a que estamos hoy|que fecha es hoy|que dia es)( cortex)?", t):
        return f"Hoy es {DAYS['es'][now.weekday()]} {now.day} de {MONTHS['es'][now.month - 1]}."
    if re.fullmatch(r"(hey )?(cortex )?(what day is it( today)?|whats the date( today)?)( cortex)?", t):
        return f"It's {DAYS['en'][now.weekday()]}, {MONTHS['en'][now.month - 1]} {now.day}."
    return None


ACK_WORDS = {
    "ah", "oh", "eh", "mm", "mmm", "hmm", "vale", "ok", "okay", "okey", "oki", "gracias", "muchas", "perfecto",
    "genial", "guay", "bien", "muy", "de", "acuerdo", "entendido", "ya", "claro", "venga", "nada", "si", "no",
    "pues", "bueno", "estupendo", "fenomenal", "crack", "maquina", "thanks", "thank", "you", "cool", "got", "it",
    "great", "alright", "nice", "sure", "right", "yeah", "yes", "fine", "cortex", "chao", "adios",
}


def is_ack(text: str) -> bool:
    """¿Es solo un «ah vale», «okey, gracias»… que no pide respuesta?"""
    words = _norm(text).split()
    return not words or (len(words) <= 5 and all(w in ACK_WORDS for w in words))
