"""Synchronous L0 writes; no extraction or model calls happen here."""

from typing import Any
from uuid import UUID

from memory_manager.db.connection import Database
from memory_manager.models.turn import TurnInput


class TurnWriter:
    """Persist raw turns immediately so the caller never waits on the judge."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def write_turn(self, turn: TurnInput) -> UUID:
        validate_turn(turn)
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into turns (
                        session_id, scope, source, content, tool_name, tool_call_id,
                        source_event_id, source_path, source_heading, source_start_line,
                        source_end_line, source_hash
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (session_id, scope, source_event_id)
                        where source_event_id is not null
                    do update set source_event_id = excluded.source_event_id
                    returning id
                    """,
                    (
                        turn.session_id,
                        turn.scope,
                        turn.source,
                        turn.content,
                        turn.tool_name,
                        turn.tool_call_id,
                        turn.source_event_id,
                        turn.source_path,
                        turn.source_heading,
                        turn.source_start_line,
                        turn.source_end_line,
                        turn.source_hash,
                    ),
                )
                row: dict[str, Any] = cursor.fetchone()
        return row["id"]


def validate_turn(turn: TurnInput) -> None:
    """Reject malformed L0 input before it reaches Postgres."""
    if not turn.scope.strip():
        raise ValueError("scope must not be empty")
    if not turn.content.strip():
        raise ValueError("content must not be empty")
    if turn.source == "tool" and not turn.tool_call_id:
        raise ValueError("tool turns require tool_call_id")


def write_turn(database: Database, turn: TurnInput) -> UUID:
    """Functional entry point matching the spec's ingestion contract."""
    return TurnWriter(database).write_turn(turn)
