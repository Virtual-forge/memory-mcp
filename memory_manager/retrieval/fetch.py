"""Hydrate Qdrant IDs from Postgres without widening scope."""

from uuid import UUID

from memory_manager.db.connection import Database
from memory_manager.models.atom import AtomResult
from memory_manager.synthesis.routing import L2_TABLES


def fetch_results(
    database: Database,
    ids: list[UUID],
    scores: dict[UUID, float] | None = None,
    scope: str | None = None,
    project_id: UUID | None = None,
) -> list[AtomResult]:
    if not ids:
        return []
    score_map = scores or {}
    records: list[AtomResult] = []
    records.extend(fetch_atoms(database, ids, score_map, scope, project_id))
    for category in sorted(L2_TABLES):
        records.extend(
            fetch_l2_category(database, category, ids, score_map, scope, project_id)
        )
    return sorted(records, key=lambda item: item.score, reverse=True)


def fetch_atoms(
    database: Database,
    ids: list[UUID],
    scores: dict[UUID, float],
    scope: str | None,
    project_id: UUID | None,
) -> list[AtomResult]:
    scope_clause = "" if scope is None else " and a.scope = %s"
    project_clause = " and a.project_id is null" if project_id is None else " and a.project_id = %s"
    params: list[object] = [ids]
    if scope is not None:
        params.append(scope)
    if project_id is not None:
        params.append(project_id)
    rows = database.fetch_all(
        f"""
         select a.id, a.scope, a.statement, a.category, a.origin, a.scene_name,
             a.predicate, a.confidence, a.project_id, e.name as entity
        from atoms as a
        left join entities as e on e.id = a.entity_id
        where a.id = any(%s)
          and not a.source_deleted
          and not exists (
              select 1 from atoms as newer where newer.supersedes = a.id
          )
          {scope_clause}{project_clause}
        """,
        params,
    )
    return [
        AtomResult(
            id=row["id"],
            source_table="atoms",
            title=row["statement"],
            content=row["statement"],
            scope=row["scope"],
            score=scores.get(row["id"], 0.0),
            category=row["category"],
            entity=row.get("entity"),
            predicate=row.get("predicate"),
            origin=row.get("origin"),
            scene_name=row.get("scene_name"),
            metadata={
                "confidence": row.get("confidence"),
                **(
                    {"project_id": str(row["project_id"])}
                    if row.get("project_id")
                    else {}
                ),
            },
        )
        for row in rows
    ]


def fetch_l2_category(
    database: Database,
    category: str,
    ids: list[UUID],
    scores: dict[UUID, float],
    scope: str | None,
    project_id: UUID | None,
) -> list[AtomResult]:
    definition = L2_TABLES[category]
    if definition.project_scoped != (project_id is not None):
        return []
    scope_clause = "" if scope is None else " and scope = %s"
    project_clause = "" if not definition.project_scoped else " and project_id = %s"
    params: list[object] = [ids]
    if scope is not None:
        params.append(scope)
    if definition.project_scoped:
        params.append(project_id)
    task_fields = ", owner, deadline, status" if definition.has_task_fields else ""
    project_field = ", project_id" if definition.project_scoped else ""
    rows = database.fetch_all(
        f"""
        select id, scope, title, summary{task_fields}{project_field}
        from {definition.table_name}
        where id = any(%s){scope_clause}{project_clause}
        """,
        params,
    )
    return [
        AtomResult(
            id=row["id"],
            source_table=category,
            title=row["title"],
            content=row["summary"],
            scope=row["scope"],
            score=scores.get(row["id"], 0.0),
            metadata={
                **{
                    key: row[key]
                    for key in ("owner", "deadline", "status")
                    if key in row and row[key] is not None
                },
                **(
                    {"project_id": str(row["project_id"])}
                    if row.get("project_id")
                    else {}
                ),
            },
        )
        for row in rows
    ]


def fetch_by_ids(
    database: Database,
    ids: list[UUID],
    scope: str | None = None,
    project_id: UUID | None = None,
) -> list[AtomResult]:
    """Fetch full records for caller-selected IDs without reranking."""
    return fetch_results(database, ids, scope=scope, project_id=project_id)
