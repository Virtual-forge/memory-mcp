"""Coverage checks that make overlapping extraction triggers safe."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID


def load_uncovered_turns(
    connection: Any,
    session_id: UUID,
    scope: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Load turns with no coverage marker or atom provenance yet."""
    limit_clause = "" if limit is None else " limit %s"
    params: list[Any] = [session_id, scope]
    if limit is not None:
        params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            select t.*
            from turns as t
            where t.session_id = %s
              and t.scope = %s
                            and not exists (
                                    select 1
                                    from project_document_chunks as project_chunk
                                    where project_chunk.turn_id = t.id
                            )
              and not exists (
                  select 1
                  from extraction_coverage as ec
                  where ec.turn_id = t.id
              )
              and not exists (
                  select 1
                  from atom_sources as source
                  where source.turn_id = t.id
              )
            order by t.created_at, t.id
            {limit_clause}
            """,
            params,
        )
        return list(cursor.fetchall())


def load_uncovered_project_turns(
    connection: Any,
    project_id: UUID,
    scope: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Load only active documentation chunks for one confirmed project."""
    limit_clause = "" if limit is None else " limit %s"
    params: list[Any] = [project_id, scope]
    if limit is not None:
        params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            select t.*
            from turns as t
            join project_document_chunks as chunk on chunk.turn_id = t.id
            join project_documents as document on document.id = chunk.document_id
            where document.project_id = %s
              and document.is_active
              and chunk.is_active
              and t.scope = %s
              and not exists (
                  select 1
                  from extraction_coverage as ec
                  where ec.turn_id = t.id
              )
              and not exists (
                  select 1
                  from atom_sources as source
                  where source.turn_id = t.id
              )
            order by t.created_at, t.id
            {limit_clause}
            """,
            params,
        )
        return list(cursor.fetchall())


def mark_turns_covered(connection: Any, turn_ids: Sequence[UUID]) -> None:
    """Mark only turns that gained atom provenance in the extraction transaction."""
    if not turn_ids:
        return
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            insert into extraction_coverage (turn_id)
            values (%s)
            on conflict (turn_id) do nothing
            """,
            [(turn_id,) for turn_id in turn_ids],
        )


def record_extraction_attempt(
    connection: Any,
    session_id: UUID,
    scope: str,
    attempted_through: dict[str, Any],
) -> None:
    """Advance the extraction cursor even when the judge returns no atom."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into extraction_attempts (
                session_id, scope, attempted_through_created_at, attempted_through_turn_id
            ) values (%s, %s, %s, %s)
            on conflict (session_id, scope) do update set
                attempted_through_created_at = excluded.attempted_through_created_at,
                attempted_through_turn_id = excluded.attempted_through_turn_id,
                attempted_at = now()
            """,
            (
                session_id,
                scope,
                attempted_through["created_at"],
                attempted_through["id"],
            ),
        )
