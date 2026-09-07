"""Project-scoped L2 synthesis for imported documentation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from memory_manager.db.connection import Database
from memory_manager.extraction.judge import JsonCompletionClient
from memory_manager.synthesis.upsert import (
    load_existing_rows,
    load_pending_atoms,
    mark_synthesis_covered,
    serialize_atom,
)


class ProjectSectionDestinationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1)
    atom_ids: list[UUID] = Field(min_length=1)
    existing_row_id: UUID | None = None

    @field_validator("title", "summary")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()


@dataclass(frozen=True)
class ProjectSynthesisContext:
    project_id: UUID
    project_name: str
    scope: str
    new_atoms: list[dict[str, object]]
    existing_rows: list[dict[str, object]]

    def as_payload(self) -> dict[str, object]:
        return {
            "project_id": str(self.project_id),
            "project_name": self.project_name,
            "scope": self.scope,
            "new_atoms": self.new_atoms,
            "existing_rows": {"project_sections": self.existing_rows},
        }


class ProjectSynthesisJudge:
    """Ask the project L2 prompt for bounded, project-owned sections."""

    def __init__(self, client: JsonCompletionClient, prompt_path: str | Path) -> None:
        self.client = client
        self.prompt_path = Path(prompt_path)

    def synthesize(
        self,
        context: ProjectSynthesisContext,
        drop_invalid_atoms: bool = False,
    ) -> list[ProjectSectionDestinationModel]:
        raw_response = self.client.complete_json(
            system_prompt=self.prompt_path.read_text(encoding="utf-8"),
            payload=context.as_payload(),
        )
        existing_ids = {row["id"] for row in context.existing_rows}
        return validate_project_synthesis_response(
            raw_response,
            {atom["id"] for atom in context.new_atoms},
            existing_ids,
            drop_invalid_atoms=drop_invalid_atoms,
        )


def validate_project_synthesis_response(
    raw_response: object,
    atom_ids: set[object],
    existing_row_ids: set[object],
    drop_invalid_atoms: bool = False,
) -> list[ProjectSectionDestinationModel]:
    if isinstance(raw_response, str):
        raw_response = json.loads(raw_response)
    destinations = TypeAdapter(list[ProjectSectionDestinationModel]).validate_python(
        raw_response
    )
    valid_atom_ids = {UUID(str(atom_id)) for atom_id in atom_ids}
    valid_row_ids = {UUID(str(row_id)) for row_id in existing_row_ids}
    valid_destinations: list[ProjectSectionDestinationModel] = []
    for destination in destinations:
        if not set(destination.atom_ids).issubset(valid_atom_ids):
            if drop_invalid_atoms:
                continue
            raise ValueError("project synthesis returned an atom outside the batch")
        if (
            destination.existing_row_id is not None
            and destination.existing_row_id not in valid_row_ids
        ):
            raise ValueError("project synthesis selected a row outside the project")
        valid_destinations.append(destination)
    return valid_destinations


def build_project_synthesis_context(
    connection: Any,
    project_id: UUID,
    project_name: str,
    scope: str,
    atoms: list[dict[str, object]],
) -> ProjectSynthesisContext:
    existing = load_existing_rows(
        connection,
        scope,
        categories=["project_sections"],
        project_id=project_id,
    )
    return ProjectSynthesisContext(
        project_id=project_id,
        project_name=project_name,
        scope=scope,
        new_atoms=[serialize_atom(atom) for atom in atoms],
        existing_rows=existing.get("project_sections", []),
    )


def run_project_synthesis(
    database: Database,
    project_id: UUID,
    project_name: str,
    scope: str,
    judge: ProjectSynthesisJudge,
    atom_volume: int | None = None,
) -> list[UUID]:
    """Synthesize one project's pending atoms without mixing other projects."""
    with database.transaction() as connection:
        atoms = load_pending_atoms(connection, scope, atom_volume, project_id=project_id)
        if not atoms:
            return []
        context = build_project_synthesis_context(
            connection,
            project_id,
            project_name,
            scope,
            atoms,
        )

    destinations = [
        destination.model_copy(
            update={
                "summary": project_summary_for_low_confidence(destination, context),
            }
        )
        for destination in judge.synthesize(context, drop_invalid_atoms=True)
    ]
    atom_ids = [atom["id"] for atom in atoms]
    with database.transaction() as connection:
        write_project_destinations(connection, project_id, scope, destinations)
        mark_synthesis_covered(connection, atom_ids)
    return atom_ids


def write_project_destinations(
    connection: Any,
    project_id: UUID,
    scope: str,
    destinations: list[ProjectSectionDestinationModel],
) -> list[UUID]:
    row_ids: list[UUID] = []
    for destination in destinations:
        existing_id = destination.existing_row_id or find_project_section_match(
            connection,
            project_id,
            scope,
            destination.title,
        )
        with connection.cursor() as cursor:
            if existing_id is not None:
                cursor.execute(
                    """
                    update project_sections
                    set title = %s, summary = %s, updated_at = now()
                    where id = %s and project_id = %s and scope = %s
                    returning id
                    """,
                    (
                        destination.title,
                        destination.summary,
                        existing_id,
                        project_id,
                        scope,
                    ),
                )
            else:
                cursor.execute(
                    """
                    insert into project_sections (project_id, scope, title, summary)
                    values (%s, %s, %s, %s)
                    returning id
                    """,
                    (project_id, scope, destination.title, destination.summary),
                )
            row = cursor.fetchone()
        if row is None:
            raise ValueError("project synthesis attempted to update a row outside the project")
        row_id = row["id"]
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                insert into project_section_atoms (section_id, atom_id)
                values (%s, %s)
                on conflict do nothing
                """,
                [(row_id, atom_id) for atom_id in destination.atom_ids],
            )
        row_ids.append(row_id)
    return row_ids


def find_project_section_match(
    connection: Any,
    project_id: UUID,
    scope: str,
    title: str,
) -> UUID | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select id
            from project_sections
            where project_id = %s and scope = %s and lower(title) = lower(%s)
            order by updated_at desc
            limit 1
            """,
            (project_id, scope, title),
        )
        row = cursor.fetchone()
    return row["id"] if row else None


def project_summary_for_low_confidence(
    destination: ProjectSectionDestinationModel,
    context: ProjectSynthesisContext,
) -> str:
    """Keep the same proposed-note convention as ordinary L2 synthesis."""
    atoms_by_id = {UUID(str(atom["id"])): atom for atom in context.new_atoms}
    low_confidence = [
        atoms_by_id[atom_id]
        for atom_id in destination.atom_ids
        if float(atoms_by_id[atom_id].get("confidence") or 0.0) < 0.8
    ]
    if not low_confidence:
        return destination.summary
    existing = next(
        (
            str(row["summary"])
            for row in context.existing_rows
            if (
                destination.existing_row_id is not None
                and str(row["id"]) == str(destination.existing_row_id)
            )
            or (
                destination.existing_row_id is None
                and str(row.get("title", "")).casefold() == destination.title.casefold()
            )
        ),
        None,
    )
    proposed_note = "\n".join(
        [
            "Proposed, not yet applied:",
            *[f"- {atom['statement']}" for atom in low_confidence],
        ]
    )
    if not existing:
        return proposed_note
    return f"{existing.rstrip()}\n\n{proposed_note}"
