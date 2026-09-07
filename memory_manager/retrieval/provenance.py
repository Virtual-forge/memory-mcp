"""Hydrate the exact L0 turns supporting an atom or L2 row."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from memory_manager.db.connection import Database
from memory_manager.synthesis.routing import L2_TABLES


def fetch_source_turns(
    database: Database,
    record_id: UUID,
    source_table: str,
    scope: str | None = None,
    project_id: UUID | None = None,
) -> list[dict[str, object]]:
    """Return provenance turns through the atom or category junction table."""
    validate_source_table(source_table)
    if source_table == "project_sections" and project_id is None:
        raise ValueError("project_id is required for project section provenance")
    if source_table == "atoms":
        rows = fetch_atom_turns(database, record_id, scope, project_id)
    else:
        rows = fetch_l2_turns(database, record_id, source_table, scope, project_id)
    return [serialize_turn(row) for row in rows]


def validate_source_table(source_table: str) -> None:
    if source_table != "atoms" and source_table not in L2_TABLES:
        raise ValueError(f"unsupported provenance source table: {source_table}")


def fetch_atom_turns(
    database: Database,
    atom_id: UUID,
    scope: str | None,
    project_id: UUID | None = None,
) -> list[dict[str, object]]:
    scope_clause, params = scope_filter("a.scope", atom_id, scope)
    project_clause = "" if project_id is None else " and a.project_id = %s"
    if project_id is not None:
        params.append(project_id)
    return database.fetch_all(
        f"""
        select distinct t.id, t.session_id, t.scope, t.source, t.content,
               t.tool_name, t.tool_call_id, t.source_event_id, t.created_at,
               t.source_path, t.source_heading, t.source_start_line,
               t.source_end_line, t.source_hash, a.project_id
        from atom_sources as source
        join atoms as a on a.id = source.atom_id
        join turns as t on t.id = source.turn_id
        where a.id = %s{scope_clause}{project_clause}
          and t.scope = a.scope
        order by t.created_at, t.id
        """,
        params,
    )


def fetch_l2_turns(
    database: Database,
    row_id: UUID,
    source_table: str,
    scope: str | None,
    project_id: UUID | None = None,
) -> list[dict[str, object]]:
    definition = L2_TABLES[source_table]
    if definition.project_scoped != (project_id is not None):
        return []
    scope_clause, params = scope_filter("l2_row.scope", row_id, scope)
    project_clause = "" if project_id is None else " and l2_row.project_id = %s"
    if project_id is not None:
        params.append(project_id)
    project_field = (
        "l2_row.project_id" if definition.project_scoped else "null::uuid"
    )
    return database.fetch_all(
        f"""
        select distinct t.id, t.session_id, t.scope, t.source, t.content,
               t.tool_name, t.tool_call_id, t.source_event_id, t.created_at,
               t.source_path, t.source_heading, t.source_start_line,
               t.source_end_line, t.source_hash, {project_field} as project_id
        from {definition.table_name} as l2_row
        join {definition.junction_table} as link
          on link.{definition.foreign_key} = l2_row.id
        join atom_sources as source on source.atom_id = link.atom_id
        join turns as t on t.id = source.turn_id
        where l2_row.id = %s{scope_clause}{project_clause}
          and t.scope = l2_row.scope
        order by t.created_at, t.id
        """,
        params,
    )


def scope_filter(column: str, record_id: UUID, scope: str | None) -> tuple[str, list[object]]:
    if scope is None:
        return "", [record_id]
    return f" and {column} = %s", [record_id, scope]


def serialize_turn(row: dict[str, Any]) -> dict[str, object]:
    serialized = {
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "scope": row["scope"],
        "source": row["source"],
        "content": row["content"],
        "tool_name": row.get("tool_name"),
        "tool_call_id": row.get("tool_call_id"),
        "source_event_id": row.get("source_event_id"),
        "created_at": serialize_datetime(row.get("created_at")),
    }
    if row.get("project_id") is not None:
        serialized["project_id"] = str(row["project_id"])
    for field in (
        "source_path",
        "source_heading",
        "source_start_line",
        "source_end_line",
        "source_hash",
    ):
        if row.get(field) is not None:
            serialized[field] = row[field]
    return serialized


def serialize_datetime(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value