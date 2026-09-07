"""Runtime-loaded L1 extraction judge and its database write path."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from memory_manager.db.connection import Database
from memory_manager.extraction.entity_resolution import load_known_entities, resolve_entity
from memory_manager.extraction.idempotency import (
    load_uncovered_project_turns,
    load_uncovered_turns,
    mark_turns_covered,
    record_extraction_attempt,
)


class JsonCompletionClient(Protocol):
    """Minimal LLM boundary used by extraction and easy to fake in tests."""

    def complete_json(self, *, system_prompt: str, payload: dict[str, object]) -> object:
        ...


class JudgeAtom(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    category: str
    entity: str | None = None
    predicate: str | None = None
    confidence: float = Field(ge=0, le=1)
    source_turn_ids: list[UUID] = Field(min_length=1)
    supersedes: UUID | None = None

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str) -> str:
        if value not in {"fact", "preference", "decision", "event"}:
            raise ValueError("unknown atom category")
        return value

    @field_validator("statement")
    @classmethod
    def validate_statement(cls, value: str) -> str:
        return value.strip()


class JudgeScene(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_name: str = Field(min_length=1, max_length=120)
    atoms: list[JudgeAtom]

    @field_validator("scene_name")
    @classmethod
    def validate_scene_name(cls, value: str) -> str:
        return value.strip()


@dataclass(frozen=True)
class ExtractionContext:
    new_turns: list[dict[str, object]]
    recent_scene: str | None
    recent_atoms: list[dict[str, object]]
    known_entities: list[dict[str, object]]
    project_id: UUID | None = None
    document_mode: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "new_turns": self.new_turns,
            "recent_scene": self.recent_scene,
            "recent_atoms": self.recent_atoms,
            "known_entities": self.known_entities,
            "project_id": str(self.project_id) if self.project_id else None,
            "document_mode": self.document_mode,
        }


class ExtractionJudge:
    """Load the extraction prompt from disk and validate its JSON response."""

    def __init__(self, client: JsonCompletionClient, prompt_path: str | Path) -> None:
        self.client = client
        self.prompt_path = Path(prompt_path)

    def extract(
        self,
        context: ExtractionContext,
        allow_single_source: bool = False,
        drop_invalid_sources: bool = False,
    ) -> list[JudgeScene]:
        prompt = self.prompt_path.read_text(encoding="utf-8")
        raw_response = self.client.complete_json(
            system_prompt=prompt,
            payload=context.as_payload(),
        )
        return validate_judge_response(
            raw_response,
            {turn["id"] for turn in context.new_turns},
            require_corroboration=not allow_single_source,
            drop_invalid_sources=drop_invalid_sources,
        )


def validate_judge_response(
    raw_response: object,
    new_turn_ids: set[object],
    require_corroboration: bool = True,
    drop_invalid_sources: bool = False,
) -> list[JudgeScene]:
    """Validate schema, confidence, and provenance ownership before writes."""
    if isinstance(raw_response, str):
        raw_response = json.loads(raw_response)
    scenes = TypeAdapter(list[JudgeScene]).validate_python(raw_response)
    valid_ids = {UUID(str(turn_id)) for turn_id in new_turn_ids}
    for scene in scenes:
        valid_atoms: list[JudgeAtom] = []
        for atom in scene.atoms:
            if atom.confidence < 0.5:
                continue
            if not set(atom.source_turn_ids).issubset(valid_ids):
                if drop_invalid_sources:
                    continue
                raise ValueError("judge returned a source turn outside the extraction batch")
            if require_corroboration and len(set(atom.source_turn_ids)) < 2:
                continue
            valid_atoms.append(atom)
        scene.atoms = valid_atoms
    return scenes


def build_extraction_context(
    connection: Any,
    turns: list[dict[str, object]],
    scope: str,
    project_id: UUID | None = None,
) -> ExtractionContext:
    recent_atoms = load_recent_atoms(connection, scope, project_id=project_id)
    known_entities = load_known_entities(connection, scope)
    recent_scene = recent_atoms[0].get("scene_name") if recent_atoms else None
    return ExtractionContext(
        new_turns=[serialize_turn(turn) for turn in turns],
        recent_scene=str(recent_scene) if recent_scene else None,
        recent_atoms=recent_atoms,
        known_entities=known_entities,
        project_id=project_id,
        document_mode=project_id is not None,
    )


def load_recent_atoms(
    connection: Any,
    scope: str,
    limit: int = 100,
    project_id: UUID | None = None,
) -> list[dict[str, object]]:
    project_clause = " and a.project_id is null" if project_id is None else " and a.project_id = %s"
    params: list[object] = [scope]
    if project_id is not None:
        params.append(project_id)
    params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            select a.id, a.statement, a.category, a.scene_name, a.entity_id,
                   a.predicate, a.confidence, a.supersedes
            from atoms as a
            where a.scope = %s
              {project_clause}
            order by a.created_at desc
            limit %s
            """,
            params,
        )
        return list(cursor.fetchall())


def serialize_turn(turn: dict[str, object]) -> dict[str, object]:
    serialized: dict[str, object] = {
        "id": str(turn["id"]),
        "source": turn["source"],
        "content": turn["content"],
        "tool_name": turn.get("tool_name"),
        "tool_call_id": turn.get("tool_call_id"),
        "created_at": turn["created_at"].isoformat() if turn.get("created_at") else None,
    }
    for field in (
        "source_path",
        "source_heading",
        "source_start_line",
        "source_end_line",
        "source_hash",
    ):
        if turn.get(field) is not None:
            serialized[field] = turn[field]
    return serialized


def extract_pending_turns(
    database: Database,
    session_id: UUID,
    scope: str,
    judge: ExtractionJudge,
    volume_cap: int | None = None,
    project_id: UUID | None = None,
    allow_single_source: bool = False,
) -> list[UUID]:
    """Extract one uncovered batch, then atomically write provenance and coverage."""
    with database.transaction() as connection:
        turns = (
            load_uncovered_project_turns(connection, project_id, scope, volume_cap)
            if project_id is not None
            else load_uncovered_turns(connection, session_id, scope, volume_cap)
        )
        if not turns:
            return []
        context = build_extraction_context(connection, turns, scope, project_id=project_id)

    scenes = judge.extract(
        context,
        allow_single_source=allow_single_source,
        drop_invalid_sources=project_id is not None,
    )
    turn_ids = [turn["id"] for turn in turns]

    with database.transaction() as connection:
        still_uncovered = {
            turn["id"]
            for turn in (
                load_uncovered_project_turns(connection, project_id, scope, volume_cap)
                if project_id is not None
                else load_uncovered_turns(connection, session_id, scope, volume_cap)
            )
        }
        if not still_uncovered:
            record_extraction_attempt(connection, session_id, scope, turns[-1])
            return []
        write_judged_atoms(connection, scenes, scope, still_uncovered, project_id=project_id)
        covered_turn_ids = (
            turn_ids if project_id is not None else load_atom_source_turn_ids(connection, turn_ids)
        )
        mark_turns_covered(connection, covered_turn_ids)
        record_extraction_attempt(connection, session_id, scope, turns[-1])
    return turn_ids


def load_atom_source_turn_ids(connection: Any, turn_ids: list[UUID]) -> list[UUID]:
    if not turn_ids:
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select distinct turn_id
            from atom_sources
            where turn_id = any(%s)
            """,
            (turn_ids,),
        )
        return [row["turn_id"] for row in cursor.fetchall()]


def write_judged_atoms(
    connection: Any,
    scenes: list[JudgeScene],
    scope: str,
    allowed_turn_ids: set[object],
    project_id: UUID | None = None,
) -> list[UUID]:
    """Persist inferred atoms, entity links, and direct many-to-many provenance."""
    created_ids: list[UUID] = []
    for scene in scenes:
        for atom in scene.atoms:
            source_ids = [
                turn_id
                for turn_id in atom.source_turn_ids
                if turn_id in allowed_turn_ids
            ]
            if not source_ids:
                continue
            entity_id = resolve_entity(connection, scope, atom.entity)
            supersedes = find_same_scope_atom(connection, scope, atom.supersedes, project_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into atoms (
                        scope, statement, category, origin, scene_name, entity_id,
                        predicate, confidence, project_id, supersedes
                    ) values (%s, %s, %s, 'inferred', %s, %s, %s, %s, %s, %s)
                    returning id
                    """,
                    (
                        scope,
                        atom.statement,
                        atom.category,
                        scene.scene_name,
                        entity_id,
                        atom.predicate,
                        atom.confidence,
                        project_id,
                        supersedes,
                    ),
                )
                created_id = cursor.fetchone()["id"]
                cursor.executemany(
                    """
                    insert into atom_sources (atom_id, turn_id)
                    values (%s, %s)
                    on conflict do nothing
                    """,
                    [(created_id, turn_id) for turn_id in source_ids],
                )
            created_ids.append(created_id)
    return created_ids


def find_same_scope_atom(
    connection: Any,
    scope: str,
    atom_id: UUID | None,
    project_id: UUID | None = None,
) -> UUID | None:
    if atom_id is None:
        return None
    with connection.cursor() as cursor:
        cursor.execute(
                        """
                        select id
                        from atoms
                        where id = %s and scope = %s
                            and project_id is not distinct from %s
                        """,
                        (atom_id, scope, project_id),
        )
        row = cursor.fetchone()
    return row["id"] if row else None
