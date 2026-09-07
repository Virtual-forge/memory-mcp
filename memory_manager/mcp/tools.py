"""MCP-facing memory operations with explicit scope and origin semantics."""

from dataclasses import asdict
from typing import Any
from uuid import UUID

from memory_manager.db.connection import Database
from memory_manager.extraction.entity_resolution import resolve_entity
from memory_manager.indexing.qdrant_writer import QdrantWriter
from memory_manager.models.atom import AtomResult
from memory_manager.projects import find_project_candidates, list_registered_projects
from memory_manager.retrieval.expand_sources import expand_sources
from memory_manager.retrieval.provenance import fetch_source_turns
from memory_manager.retrieval.recall import RecallService


class MemoryTools:
    """Application service behind the MCP functions."""

    def __init__(
        self,
        database: Database,
        recall_service: RecallService,
        qdrant_writer: QdrantWriter,
        default_scope: str,
    ) -> None:
        self.database = database
        self.recall_service = recall_service
        self.qdrant_writer = qdrant_writer
        self.default_scope = default_scope

    def remember(
        self,
        statement: str,
        scope: str | None = None,
        category: str = "fact",
        entity: str | None = None,
        predicate: str | None = None,
    ) -> dict[str, object]:
        """Write a human-asserted atom without sending it through the judge."""
        target_scope = scope or self.default_scope
        validate_atom_category(category)
        if not statement.strip():
            raise ValueError("statement must not be empty")
        with self.database.transaction() as connection:
            entity_id = resolve_entity(connection, target_scope, entity)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into atoms (
                        scope, statement, category, origin, scene_name, entity_id,
                        predicate, confidence
                    ) values (%s, %s, %s, 'asserted', %s, %s, %s, 1.0)
                    returning id
                    """,
                    (
                        target_scope,
                        statement.strip(),
                        category,
                        "the user is explicitly storing a memory",
                        entity_id,
                        predicate,
                    ),
                )
                atom_id = cursor.fetchone()["id"]
            row = load_atom_for_index(connection, atom_id, target_scope)
        self.qdrant_writer.index_atom(row)
        return serialize_atom_row(row)

    def recall(
        self,
        query: str,
        scope: str | None = None,
        mode: str | None = None,
        types: list[str] | None = None,
        sources: list[str] | None = None,
        top_k: int = 8,
        min_score: float | None = None,
        all_scopes: bool = False,
        project_id: str | None = None,
    ) -> list[dict[str, object]]:
        target_scope = resolve_read_scope(scope, all_scopes, self.default_scope)
        parsed_project_id = parse_optional_uuid(project_id)
        validate_project_scope(self.database, parsed_project_id, target_scope)
        resolved_sources = expand_sources(
            self.database,
            types,
            sources,
            project_id=parsed_project_id,
        )
        results = self.recall_service.recall(
            query=query,
            scope=target_scope,
            mode=mode,  # type: ignore[arg-type]
            sources=resolved_sources or None,
            top_k=top_k,
            min_score=min_score,
            project_id=parsed_project_id,
        )
        return [serialize_result(result) for result in results]

    def resolve_project(
        self,
        name: str,
        scope: str | None = None,
        all_scopes: bool = True,
    ) -> list[dict[str, object]]:
        target_scope = resolve_read_scope(scope, all_scopes, self.default_scope)
        return [
            serialize_project(project)
            for project in find_project_candidates(
                self.database, name, target_scope, limit=3
            )
        ]

    def list_projects(self, scope: str | None = None) -> list[dict[str, object]]:
        """List every registered project unless an exact scope is supplied."""
        target_scope = (
            None
            if scope is None
            else resolve_read_scope(scope, False, self.default_scope)
        )
        projects = list_registered_projects(self.database, target_scope)
        if len(projects) > 3:
            raise ValueError(
                f"{len(projects)} projects are registered, but structured project selection "
                "supports at most 3 projects plus Other options; provide an exact scope to narrow "
                "the list"
            )
        return [serialize_project(project) for project in projects]

    def search_candidates(
        self,
        query: str,
        scope: str | None = None,
        mode: str | None = None,
        types: list[str] | None = None,
        sources: list[str] | None = None,
        top_k: int = 20,
        all_scopes: bool = False,
        project_id: str | None = None,
    ) -> list[dict[str, object]]:
        target_scope = resolve_read_scope(scope, all_scopes, self.default_scope)
        parsed_project_id = parse_optional_uuid(project_id)
        validate_project_scope(self.database, parsed_project_id, target_scope)
        resolved_sources = expand_sources(
            self.database,
            types,
            sources,
            project_id=parsed_project_id,
        )
        candidates = self.recall_service.search_candidates(
            query=query,
            scope=target_scope,
            mode=mode,  # type: ignore[arg-type]
            sources=resolved_sources or None,
            top_k=top_k,
            project_id=parsed_project_id,
        )
        return [asdict(candidate) | {"id": str(candidate.id)} for candidate in candidates]

    def fetch(
        self,
        ids: list[str],
        scope: str | None = None,
        all_scopes: bool = False,
        project_id: str | None = None,
    ) -> list[dict[str, object]]:
        target_scope = resolve_read_scope(scope, all_scopes, self.default_scope)
        parsed_project_id = parse_optional_uuid(project_id)
        validate_project_scope(self.database, parsed_project_id, target_scope)
        return [
            serialize_result(result)
            for result in self.recall_service.fetch(
                ids,
                scope=target_scope,
                project_id=parsed_project_id,
            )
        ]

    def drill_down(
        self,
        record_id: str,
        source_table: str,
        scope: str | None = None,
        all_scopes: bool = False,
        project_id: str | None = None,
    ) -> list[dict[str, object]]:
        target_scope = resolve_read_scope(scope, all_scopes, self.default_scope)
        parsed_project_id = parse_optional_uuid(project_id)
        validate_project_scope(self.database, parsed_project_id, target_scope)
        return fetch_source_turns(
            self.database,
            parse_uuid(record_id),
            source_table,
            target_scope,
            parsed_project_id,
        )

    def update(
        self,
        atom_id: str,
        statement: str | None = None,
        category: str | None = None,
        entity: str | None = None,
        predicate: str | None = None,
        scope: str | None = None,
    ) -> dict[str, object]:
        """Explicitly correct one atom, retaining its original ID and provenance links."""
        target_scope = scope or self.default_scope
        parsed_id = parse_uuid(atom_id)
        if category is not None:
            validate_atom_category(category)
        with self.database.transaction() as connection:
            entity_id = (
                resolve_entity(connection, target_scope, entity)
                if entity is not None
                else None
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update atoms
                    set statement = coalesce(%s, statement),
                        category = coalesce(%s, category),
                        entity_id = coalesce(%s, entity_id),
                        predicate = coalesce(%s, predicate),
                        origin = 'asserted',
                        confidence = 1.0
                    where id = %s and scope = %s
                    returning id
                    """,
                    (statement, category, entity_id, predicate, parsed_id, target_scope),
                )
                row_id = cursor.fetchone()
            if row_id is None:
                raise KeyError(f"atom not found in scope: {atom_id}")
            row = load_atom_for_index(connection, row_id["id"], target_scope)
        self.qdrant_writer.index_atom(row)
        return serialize_atom_row(row)

    def forget(self, atom_id: str, scope: str | None = None) -> bool:
        """Hide an atom from retrieval while retaining its audit record."""
        target_scope = scope or self.default_scope
        parsed_id = parse_uuid(atom_id)
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update atoms
                    set source_deleted = true
                    where id = %s and scope = %s and not source_deleted
                    returning id
                    """,
                    (parsed_id, target_scope),
                )
                deleted = cursor.fetchone() is not None
        if deleted:
            self.qdrant_writer.delete([parsed_id])
        return deleted


def validate_atom_category(category: str) -> None:
    if category not in {"fact", "preference", "decision", "event"}:
        raise ValueError(f"unsupported atom category: {category}")


def resolve_read_scope(
    scope: str | None,
    all_scopes: bool,
    default_scope: str,
) -> str | None:
    if scope is not None and not scope.strip():
        raise ValueError("scope must not be empty")
    if all_scopes and scope is not None:
        raise ValueError("scope and all_scopes cannot be used together")
    if all_scopes:
        return None
    return scope or default_scope


def parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValueError("id must be a UUID") from exc


def parse_optional_uuid(value: str | None) -> UUID | None:
    return None if value is None else parse_uuid(value)


def validate_project_scope(
    database: Database,
    project_id: UUID | None,
    scope: str | None,
) -> None:
    if project_id is None:
        return
    scope_clause = "" if scope is None else " and scope = %s"
    params: list[object] = [project_id]
    if scope is not None:
        params.append(scope)
    project = database.fetch_one(
        f"select id from project_registry where id = %s{scope_clause}",
        params,
    )
    if project is None:
        raise KeyError(f"project not found in scope: {project_id}")


def load_atom_for_index(connection: Any, atom_id: UUID, scope: str) -> dict[str, object]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select a.id, a.scope, a.statement, a.category, a.origin, a.scene_name,
                   a.predicate, a.source_deleted, a.supersedes, e.name as entity,
                   exists (
                       select 1 from atoms as newer where newer.supersedes = a.id
                   ) as superseded
            from atoms as a
            left join entities as e on e.id = a.entity_id
            where a.id = %s and a.scope = %s
            """,
            (atom_id, scope),
        )
        row = cursor.fetchone()
    if row is None:
        raise KeyError(f"atom not found in scope: {atom_id}")
    return row


def serialize_atom_row(row: dict[str, object]) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "scope": row["scope"],
        "statement": row["statement"],
        "category": row["category"],
        "origin": row["origin"],
        "entity": row.get("entity"),
        "predicate": row.get("predicate"),
    }


def serialize_project(row: dict[str, object]) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "scope": row["scope"],
        "name": row["name"],
        "aliases": row.get("aliases", []),
        "root_path": row.get("root_path"),
    }


def serialize_result(result: AtomResult) -> dict[str, object]:
    return {
        "id": str(result.id),
        "source_table": result.source_table,
        "title": result.title,
        "content": result.content,
        "scope": result.scope,
        "score": result.score,
        "category": result.category,
        "entity": result.entity,
        "predicate": result.predicate,
        "origin": result.origin,
        "scene_name": result.scene_name,
        "metadata": result.metadata,
    }
