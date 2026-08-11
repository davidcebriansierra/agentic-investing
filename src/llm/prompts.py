"""Gestion de prompts versionados (spec v2.0: config/prompts/, prompts_history §7.3).

Cada prompt es un fichero markdown con frontmatter YAML:

    ---
    version: v1.0
    role: system | user
    model_tier: reasoning | tool
    ---
    Cuerpo del prompt con {placeholders} estilo str.format.

`PromptTemplate` calcula un hash del cuerpo para trazabilidad y permite renderizar con
variables. `PromptLibrary` descubre y cachea los prompts del directorio config/prompts.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from src.utils.config import CONFIG_DIR

PROMPTS_DIR = CONFIG_DIR / "prompts"


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str
    body: str
    metadata: dict

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:16]

    def render(self, **variables) -> str:
        """Sustituye {placeholders}. Lanza KeyError si falta alguna variable."""
        return self.body.format(**variables)


def _parse_prompt_file(path: Path, name: str) -> PromptTemplate:
    raw = path.read_text(encoding="utf-8")
    metadata: dict = {}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            metadata = yaml.safe_load(parts[1]) or {}
            body = parts[2].lstrip("\n")
    version = str(metadata.get("version", "v0"))
    return PromptTemplate(name=name, version=version, body=body.strip(), metadata=metadata)


class PromptLibrary:
    """Carga y cachea los prompts versionados del directorio config/prompts."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or PROMPTS_DIR
        self._cache: dict[str, PromptTemplate] = {}

    def get(self, name: str) -> PromptTemplate:
        """Obtiene un prompt por nombre logico, p. ej. 'evaluators/conservative'."""
        if name in self._cache:
            return self._cache[name]
        path = self.base_dir / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"Prompt no encontrado: {path}")
        template = _parse_prompt_file(path, name)
        self._cache[name] = template
        return template

    def list_prompts(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.base_dir).with_suffix(""))
            for p in self.base_dir.rglob("*.md")
        )
