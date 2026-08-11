"""Carga centralizada de variables de entorno desde `.env`.

`load_env()` lee el fichero `.env` de la raiz del proyecto (si existe y si python-dotenv
esta disponible) y es idempotente: sucesivas llamadas no repiten el trabajo. Debe llamarse
lo antes posible en los puntos de entrada (app y scripts) para que los `os.getenv` de los
factories (LLM, conectores, IBKR, Telegram) vean las claves configuradas.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("agentic.env")

# Raiz del proyecto = dos niveles por encima de este fichero (src/utils/env.py).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_loaded = False


def load_env(dotenv_path: str | Path | None = None, override: bool = False) -> None:
    """Carga `.env` en el entorno del proceso (idempotente).

    - `dotenv_path`: ruta al fichero; por defecto `<raiz>/.env`.
    - `override`: si True, las variables del `.env` sobreescriben las ya presentes.
    """
    global _loaded
    if _loaded and not override:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv es dependencia base, defensivo
        logger.debug("python-dotenv no instalado; se omite la carga de .env.")
        _loaded = True
        return
    path = Path(dotenv_path) if dotenv_path else _PROJECT_ROOT / ".env"
    if path.exists():
        load_dotenv(path, override=override)
        logger.info("Variables de entorno cargadas desde %s", path)
    else:
        logger.debug("No se encontro %s; se usan solo variables del sistema.", path)
    _loaded = True
