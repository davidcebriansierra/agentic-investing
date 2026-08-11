"""Capa de persistencia operativa (audit trail inmutable §7.3)."""
from src.persistence.repository import InMemoryRepository, Repository

__all__ = ["InMemoryRepository", "Repository"]
