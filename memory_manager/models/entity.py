"""Entity registry models."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class Entity:
    id: UUID
    scope: str
    name: str
    aliases: tuple[str, ...] = field(default_factory=tuple)
    created_at: datetime | None = None
