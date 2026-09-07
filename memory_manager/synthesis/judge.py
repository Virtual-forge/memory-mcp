"""Runtime-loaded L2 synthesis judge and response validation."""

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from memory_manager.extraction.judge import JsonCompletionClient
from memory_manager.synthesis.routing import validate_l2_category


class SynthesisDestinationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1)
    atom_ids: list[UUID] = Field(min_length=1)
    existing_row_id: UUID | None = None
    owner: str | None = None
    deadline: str | None = None
    status: str | None = None

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str) -> str:
        return validate_l2_category(value)

    @field_validator("title", "summary")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is not None and value not in {"todo", "doing", "done", "blocked", "cancelled"}:
            raise ValueError("unknown task status")
        return value


@dataclass(frozen=True)
class SynthesisContext:
    scope: str
    new_atoms: list[dict[str, object]]
    scene_names: list[str]
    existing_rows: dict[str, list[dict[str, object]]]

    def as_payload(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "new_atoms": self.new_atoms,
            "scene_names": self.scene_names,
            "existing_rows": self.existing_rows,
        }


class SynthesisJudge:
    """Ask the L2 prompt for category destinations, then validate every ID."""

    def __init__(self, client: JsonCompletionClient, prompt_path: str | Path) -> None:
        self.client = client
        self.prompt_path = Path(prompt_path)

    def synthesize(
        self,
        context: SynthesisContext,
    ) -> list[SynthesisDestinationModel]:
        prompt = self.prompt_path.read_text(encoding="utf-8")
        raw_response = self.client.complete_json(
            system_prompt=prompt,
            payload=context.as_payload(),
        )
        row_ids = {
            category: {row["id"] for row in rows}
            for category, rows in context.existing_rows.items()
        }
        return validate_synthesis_response(
            raw_response,
            {atom["id"] for atom in context.new_atoms},
            row_ids,
        )


def validate_synthesis_response(
    raw_response: object,
    atom_ids: set[object],
    existing_row_ids: dict[str, set[object]],
) -> list[SynthesisDestinationModel]:
    if isinstance(raw_response, str):
        raw_response = json.loads(raw_response)
    destinations = TypeAdapter(list[SynthesisDestinationModel]).validate_python(raw_response)
    valid_atom_ids = {UUID(str(atom_id)) for atom_id in atom_ids}
    normalized: list[SynthesisDestinationModel] = []
    for destination in destinations:
        if not set(destination.atom_ids).issubset(valid_atom_ids):
            raise ValueError("synthesis returned an atom outside the batch")
        if destination.existing_row_id is not None:
            valid_rows = {
                UUID(str(row_id)) for row_id in existing_row_ids.get(destination.category, set())
            }
            if destination.existing_row_id not in valid_rows:
                raise ValueError("synthesis selected an existing row in the wrong scope/category")
        normalized.append(destination)
    return normalized
