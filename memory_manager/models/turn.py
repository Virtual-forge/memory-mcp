"""L0 conversation turn models."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

TurnSource = Literal["user", "assistant", "tool"]


@dataclass(frozen=True)
class TurnInput:
    """Input accepted by the synchronous turn writer."""

    session_id: UUID
    scope: str
    source: TurnSource
    content: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    source_event_id: str | None = None
    source_path: str | None = None
    source_heading: str | None = None
    source_start_line: int | None = None
    source_end_line: int | None = None
    source_hash: str | None = None


@dataclass(frozen=True)
class Turn:
    id: UUID
    session_id: UUID
    scope: str
    source: TurnSource
    content: str
    tool_name: str | None
    tool_call_id: str | None
    source_event_id: str | None
    source_path: str | None
    source_heading: str | None
    source_start_line: int | None
    source_end_line: int | None
    source_hash: str | None
    created_at: datetime
