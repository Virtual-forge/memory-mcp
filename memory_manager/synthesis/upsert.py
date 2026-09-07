"""Category-specific L2 row matching, upsert, and atom linking."""

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from memory_manager.db.connection import Database
from memory_manager.models.l2_row import SynthesisDestination
from memory_manager.synthesis.judge import (
    SynthesisContext,
    SynthesisDestinationModel,
    SynthesisJudge,
)
from memory_manager.synthesis.routing import (
    GENERAL_L2_TABLES,
    L2_TABLES,
    L2TableDefinition,
    group_categories,
)

CONFIDENCE_GATE = 0.8


def load_pending_atoms(
    connection: Any,
    scope: str,
    limit: int | None = None,
    project_id: UUID | None = None,
) -> list[dict[str, object]]:
    limit_clause = "" if limit is None else " limit %s"
    params: list[object] = [scope]
    project_clause = " and a.project_id is null"
    if project_id is not None:
        project_clause = " and a.project_id = %s"
        params.append(project_id)
    if limit is not None:
        params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            select a.id, a.statement, a.category, a.scene_name, a.entity_id,
                   a.predicate, a.confidence, e.name as entity
            from atoms as a
            left join entities as e on e.id = a.entity_id
            where a.scope = %s
                            {project_clause}
              and not a.source_deleted
              and not exists (
                  select 1 from synthesis_coverage as sc where sc.atom_id = a.id
              )
            order by a.created_at, a.id
            {limit_clause}
            """,
            params,
        )
        return list(cursor.fetchall())


def load_existing_rows(
    connection: Any,
    scope: str,
    categories: Iterable[str] | None = None,
    project_id: UUID | None = None,
) -> dict[str, list[dict[str, object]]]:
    rows_by_category: dict[str, list[dict[str, object]]] = {}
    for category in group_categories(categories):
        definition = L2_TABLES[category]
        task_fields = ", owner, deadline, status" if definition.has_task_fields else ""
        project_clause = ""
        params: list[object] = [scope]
        if definition.project_scoped:
            if project_id is None:
                continue
            project_clause = " and project_id = %s"
            params.append(project_id)
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                select id, scope, title, summary, created_at, updated_at{task_fields}
                from {definition.table_name}
                where scope = %s{project_clause}
                order by updated_at desc, id
                """,
                params,
            )
            rows_by_category[category] = list(cursor.fetchall())
    return rows_by_category


def build_synthesis_context(
    connection: Any,
    scope: str,
    atoms: list[dict[str, object]],
) -> SynthesisContext:
    categories = list(GENERAL_L2_TABLES)
    return SynthesisContext(
        scope=scope,
        new_atoms=[serialize_atom(atom) for atom in atoms],
        scene_names=sorted({str(atom["scene_name"]) for atom in atoms}),
        existing_rows=load_existing_rows(connection, scope, categories),
    )


def serialize_atom(atom: dict[str, object]) -> dict[str, object]:
    return {
        "id": str(atom["id"]),
        "statement": atom["statement"],
        "category": atom["category"],
        "scene_name": atom["scene_name"],
        "entity": atom.get("entity"),
        "predicate": atom.get("predicate"),
        "confidence": atom.get("confidence"),
    }


def run_synthesis(
    database: Database,
    scope: str,
    judge: SynthesisJudge,
    atom_volume: int | None = None,
) -> list[UUID]:
    """Synthesize a pending atom batch without holding a transaction over the LLM call."""
    with database.transaction() as connection:
        atoms = load_pending_atoms(connection, scope, atom_volume)
        if not atoms:
            return []
        context = build_synthesis_context(connection, scope, atoms)

    raw_destinations = judge.synthesize(context)
    destinations = [to_destination(destination, context) for destination in raw_destinations]
    atom_ids = [atom["id"] for atom in atoms]

    with database.transaction() as connection:
        write_destinations(connection, scope, destinations)
        mark_synthesis_covered(connection, atom_ids)
    return atom_ids


def to_destination(
    model: SynthesisDestinationModel,
    context: SynthesisContext | None = None,
) -> SynthesisDestination:
    deadline = None
    if model.deadline:
        deadline = datetime.fromisoformat(model.deadline.replace("Z", "+00:00"))
    return SynthesisDestination(
        category=model.category,
        title=model.title,
        summary=gate_synthesis_summary(model, context),
        atom_ids=tuple(model.atom_ids),
        existing_row_id=model.existing_row_id,
        owner=model.owner,
        deadline=deadline,
        status=model.status,
    )


def gate_synthesis_summary(
    destination: SynthesisDestinationModel,
    context: SynthesisContext | None,
) -> str:
    if context is None:
        return destination.summary

    atoms_by_id = {UUID(str(atom["id"])): atom for atom in context.new_atoms}
    low_confidence_atoms = [
        atoms_by_id[atom_id]
        for atom_id in destination.atom_ids
        if float(atoms_by_id[atom_id].get("confidence") or 0.0) < CONFIDENCE_GATE
    ]
    if not low_confidence_atoms:
        return destination.summary

    existing_summary = find_context_summary(context, destination)
    has_high_confidence_atom = any(
        float(atoms_by_id[atom_id].get("confidence") or 0.0) >= CONFIDENCE_GATE
        for atom_id in destination.atom_ids
    )
    settled_summary = destination.summary if has_high_confidence_atom else existing_summary
    proposed_note = "\n".join(
        [
            "Proposed, not yet applied:",
            *[f"- {atom['statement']}" for atom in low_confidence_atoms],
        ]
    )
    if not settled_summary:
        return proposed_note
    return f"{settled_summary.rstrip()}\n\n{proposed_note}"


def find_context_summary(
    context: SynthesisContext,
    destination: SynthesisDestinationModel,
) -> str | None:
    for row in context.existing_rows.get(destination.category, []):
        if destination.existing_row_id is not None:
            if str(row["id"]) == str(destination.existing_row_id):
                return str(row["summary"])
            continue
        if str(row.get("title", "")).casefold() == destination.title.casefold():
            return str(row["summary"])
    return None


def write_destinations(
    connection: Any,
    scope: str,
    destinations: Iterable[SynthesisDestination],
) -> list[UUID]:
    row_ids: list[UUID] = []
    for destination in destinations:
        row_ids.append(upsert_destination(connection, scope, destination))
    return row_ids


def upsert_destination(connection: Any, scope: str, destination: SynthesisDestination) -> UUID:
    definition = L2_TABLES.get(destination.category)
    if definition is None:
        raise ValueError(f"unsupported L2 category: {destination.category}")
    existing_id = destination.existing_row_id or find_title_match(
        connection, definition, scope, destination.title
    )
    row_id = update_row(connection, definition, scope, destination, existing_id)
    link_atoms(connection, definition, row_id, destination.atom_ids)
    return row_id


def find_title_match(
    connection: Any,
    definition: L2TableDefinition,
    scope: str,
    title: str,
) -> UUID | None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            select id
            from {definition.table_name}
            where scope = %s and lower(title) = lower(%s)
            order by updated_at desc
            limit 1
            """,
            (scope, title),
        )
        row = cursor.fetchone()
    return row["id"] if row else None


def update_row(
    connection: Any,
    definition: L2TableDefinition,
    scope: str,
    destination: SynthesisDestination,
    existing_id: UUID | None,
) -> UUID:
    with connection.cursor() as cursor:
        if existing_id is not None:
            if definition.has_task_fields:
                cursor.execute(
                    f"""
                    update {definition.table_name}
                    set title = %s, summary = %s, owner = %s, deadline = %s,
                        status = coalesce(%s, status), updated_at = now()
                    where id = %s and scope = %s
                    returning id
                    """,
                    (
                        destination.title,
                        destination.summary,
                        destination.owner,
                        destination.deadline,
                        destination.status,
                        existing_id,
                        scope,
                    ),
                )
            else:
                cursor.execute(
                    f"""
                    update {definition.table_name}
                    set title = %s, summary = %s, updated_at = now()
                    where id = %s and scope = %s
                    returning id
                    """,
                    (destination.title, destination.summary, existing_id, scope),
                )
        elif definition.has_task_fields:
            cursor.execute(
                f"""
                insert into {definition.table_name}
                    (scope, title, summary, owner, deadline, status)
                values (%s, %s, %s, %s, %s, coalesce(%s, 'todo'))
                returning id
                """,
                (
                    scope,
                    destination.title,
                    destination.summary,
                    destination.owner,
                    destination.deadline,
                    destination.status,
                ),
            )
        else:
            cursor.execute(
                f"""
                insert into {definition.table_name} (scope, title, summary)
                values (%s, %s, %s)
                returning id
                """,
                (scope, destination.title, destination.summary),
            )
        row = cursor.fetchone()
    if row is None:
        raise ValueError("synthesis attempted to update a row outside the requested scope")
    return row["id"]


def link_atoms(
    connection: Any,
    definition: L2TableDefinition,
    row_id: UUID,
    atom_ids: Iterable[UUID],
) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(
            f"""
            insert into {definition.junction_table} ({definition.foreign_key}, atom_id)
            values (%s, %s)
            on conflict do nothing
            """,
            [(row_id, atom_id) for atom_id in atom_ids],
        )


def mark_synthesis_covered(connection: Any, atom_ids: Iterable[UUID]) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            insert into synthesis_coverage (atom_id)
            values (%s)
            on conflict (atom_id) do nothing
            """,
            [(atom_id,) for atom_id in atom_ids],
        )
