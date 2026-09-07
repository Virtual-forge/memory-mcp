"""Shared shape for category-specific L2 rows."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class L2Row:
    id: UUID
    scope: str
    category: str
    title: str
    summary: str
    created_at: datetime
    updated_at: datetime
    owner: str | None = None
    deadline: datetime | None = None
    status: str | None = None


@dataclass(frozen=True)
class SynthesisDestination:
    category: str
    title: str
    summary: str
    atom_ids: tuple[UUID, ...]
    existing_row_id: UUID | None = None
    owner: str | None = None
    deadline: datetime | None = None
    status: str | None = None
