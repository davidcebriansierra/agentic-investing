"""Aplicacion de cambios de parametros propuestos por el asesor (confirmados via HITL).

El asesor LLM solo PROPONE; este modulo aplica el cambio a disco tras la confirmacion
humana. Los ficheros se reescriben con PyYAML, preservando la estructura (los
comentarios se pierden; se acepta porque los YAML de config ya son funcionales).

Solo se permiten rutas dentro de la whitelist `ALLOWED_PARAM_PREFIXES` del advisor:
cualquier otra ruta o fichero se rechaza antes de tocar el disco.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("agentic.governance.param_change")


class ParamChangeError(ValueError):
    """Error al aplicar un cambio de parametro (ruta invalida, tipo incompatible...)."""


def _parse_scalar(value: Any) -> Any:
    """Normaliza el valor propuesto: numeros/booleanos pasan tal cual; un string que
    parece numero se convierte para no degradar el tipo del YAML."""
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in ("true", "false"):
            return text.lower() == "true"
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            pass
    return value


def apply_param_change(
    config_dir: Path,
    file: str,
    path: str,
    value: Any,
    allowed_prefixes: dict[str, tuple[str, ...]],
) -> Any:
    """Escribe `value` en la ruta punteada `path` del YAML `file` bajo `config_dir`.

    Devuelve el valor anterior (o None si la clave no existia). Lanza
    `ParamChangeError` si el fichero/ruta no esta permitido, la ruta atraviesa un
    nodo no-dict, o el fichero no existe.
    """
    prefixes = allowed_prefixes.get(file)
    if prefixes is None:
        raise ParamChangeError(f"Fichero no permitido: {file}")
    if not path.startswith(prefixes):
        raise ParamChangeError(f"Ruta no permitida: {file}:{path}")

    target = Path(config_dir) / file
    if not target.is_file():
        raise ParamChangeError(f"Fichero inexistente: {target}")

    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ParamChangeError(f"YAML raiz no es un mapping: {file}")

    keys = path.split(".")
    node = data
    for key in keys[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            raise ParamChangeError(f"La ruta {path} atraviesa un nodo no-dict ({key})")
        node = nxt
    leaf = keys[-1]
    old_value = node.get(leaf)
    node[leaf] = _parse_scalar(value)

    target.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("Parametro aplicado: %s:%s = %s (antes: %s)", file, path, value, old_value)
    return old_value
