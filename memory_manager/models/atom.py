"""L1 atom and retrieval result models."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

AtomCategory = Literal["fact", "preference", "decision", "event"]
AtomOrigin = Literal["asserted", "inferred"]


@dataclass(frozen=True)
class Atom:
    id: UUID
    scope: str
    statement: str
    category: AtomCategory
    origin: AtomOrigin
    scene_name: str
    entity_id: UUID | None
    predicate: str | None
    confidence: float | None
    supersedes: UUID | None
    source_deleted: bool
    created_at: datetime


@dataclass(frozen=True)
class AtomResult:
    """Full content returned after retrieval and calibrated reranking."""

    id: UUID
    source_table: str
    title: str
    content: str
    scope: str
    score: float
    category: str | None = None
    entity: str | None = None
    predicate: str | None = None
    origin: str | None = None
    scene_name: str | None = None
    metadata: dict[str, object] | None = None
