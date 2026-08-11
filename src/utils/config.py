"""Carga de ficheros de configuracion YAML del directorio config/."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# Raiz del proyecto = dos niveles por encima de este fichero (src/utils/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


def load_yaml(name: str) -> dict[str, Any]:
    """Carga un YAML del directorio config/ por nombre de fichero."""
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"No existe el fichero de configuracion: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=None)
def get_config() -> dict[str, Any]:
    return load_yaml("config.yaml")


@lru_cache(maxsize=None)
def get_risk_limits() -> dict[str, Any]:
    return load_yaml("risk_limits.yaml")


@lru_cache(maxsize=None)
def get_decisor_config() -> dict[str, Any]:
    return load_yaml("decisor_weights.yaml")
