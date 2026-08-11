"""Utilidades de parseo determinístico de respuestas LLM.

Extrae el primer objeto JSON valido de una respuesta, tolerando bloques con vallas
de codigo (```json ... ```) y texto adicional alrededor.
"""
from __future__ import annotations

import json
from typing import Any


def extract_json(text: str) -> dict[str, Any]:
    """Devuelve el primer objeto JSON encontrado en `text`.

    Estrategia:
    1. Si hay un bloque ```json ... ``` se intenta primero su contenido.
    2. Si no, se intenta parsear el texto completo.
    3. Si falla, se busca el primer '{' equilibrado hasta su '}' de cierre.

    Lanza ValueError si no se encuentra ningun JSON valido.
    """
    candidates: list[str] = []

    fence = "```json"
    if fence in text:
        start = text.index(fence) + len(fence)
        end = text.find("```", start)
        if end != -1:
            candidates.append(text[start:end].strip())

    candidates.append(text.strip())

    balanced = _first_balanced_object(text)
    if balanced is not None:
        candidates.append(balanced)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    raise ValueError("No se encontro un objeto JSON valido en la respuesta del LLM.")


#: Claves habituales bajo las que un objeto JSON puede envolver la lista de resultados.
_LIST_KEYS = ("opportunities", "items", "results", "data")


def extract_json_array(text: str) -> list[dict[str, Any]]:
    """Devuelve una lista de objetos JSON de la respuesta del LLM.

    Tolera tres formatos frecuentes:
    1. Lista JSON de nivel superior: ``[ {...}, {...} ]``.
    2. Objeto envoltorio con la lista bajo una clave conocida
       (``opportunities`` / ``items`` / ``results`` / ``data``), util con el modo
       ``json_object`` de OpenAI que exige un objeto en la raiz.
    3. Un unico objeto que representa un elemento -> se devuelve como lista de uno.

    Devuelve ``[]`` si no encuentra nada parseable (no lanza), acorde al principio
    "no-operar por defecto": una respuesta ilegible no genera oportunidades.
    """
    candidates: list[str] = []

    fence = "```json"
    if fence in text:
        start = text.index(fence) + len(fence)
        end = text.find("```", start)
        if end != -1:
            candidates.append(text[start:end].strip())

    candidates.append(text.strip())

    array = _first_balanced(text, "[", "]")
    if array is not None:
        candidates.append(array)
    obj = _first_balanced(text, "{", "}")
    if obj is not None:
        candidates.append(obj)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        result = _as_dict_list(parsed)
        if result is not None:
            return result
    return []


def _as_dict_list(parsed: Any) -> list[dict[str, Any]] | None:
    """Normaliza un JSON ya parseado a una lista de dicts, o None si no aplica."""
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        for key in _LIST_KEYS:
            value = parsed.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        # Objeto suelto: se interpreta como un unico elemento (si no esta vacio).
        return [parsed] if parsed else []
    return None


def _first_balanced_object(text: str) -> str | None:
    return _first_balanced(text, "{", "}")


def _first_balanced(text: str, open_ch: str, close_ch: str) -> str | None:
    """Extrae la primera subcadena con `open_ch`/`close_ch` equilibrados (ignora strings)."""
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
