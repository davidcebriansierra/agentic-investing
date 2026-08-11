"""Capa de acceso a LLMs (cliente abstracto, proveedores y utilidades de parseo)."""
from src.llm.base import LLMClient, LLMResponse, LLMUsage
from src.llm.mock import MockLLMClient
from src.llm.parsing import extract_json
from src.llm.prompts import PromptLibrary, PromptTemplate

__all__ = [
    "LLMClient",
    "LLMResponse",
    "LLMUsage",
    "MockLLMClient",
    "PromptLibrary",
    "PromptTemplate",
    "extract_json",
]
