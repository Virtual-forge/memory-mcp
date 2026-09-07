from uuid import uuid4

import pytest

from memory_manager.synthesis.judge import (
    SynthesisContext,
    SynthesisDestinationModel,
    validate_synthesis_response,
)
from memory_manager.synthesis.project import validate_project_synthesis_response
from memory_manager.synthesis.routing import L2_CATEGORIES, group_categories
from memory_manager.synthesis.upsert import gate_synthesis_summary


def test_category_routing_is_allowlisted_and_stable():
    assert "projects" in L2_CATEGORIES
    assert group_categories(["tools", "projects", "tools"]) == ["projects", "tools"]

    with pytest.raises(ValueError, match="unsupported L2 category"):
        group_categories(["unknown"])


def test_synthesis_rejects_atom_from_another_batch():
    atom_id = uuid4()
    with pytest.raises(ValueError, match="atom outside the batch"):
        validate_synthesis_response(
            [
                {
                    "category": "tools",
                    "title": "Qdrant",
                    "summary": "The project uses Qdrant.",
                    "atom_ids": [str(uuid4())],
                    "existing_row_id": None,
                    "owner": None,
                    "deadline": None,
                    "status": None,
                }
            ],
            {atom_id},
            {"tools": set()},
        )


def test_project_synthesis_drops_destination_with_invalid_atom_id():
    atom_id = uuid4()
    destinations = validate_project_synthesis_response(
        [
            {
                "title": "Architecture",
                "summary": "The project uses its documented architecture.",
                "atom_ids": [str(uuid4())],
                "existing_row_id": None,
            },
            {
                "title": "Setup",
                "summary": "The project has documented setup steps.",
                "atom_ids": [str(atom_id)],
                "existing_row_id": None,
            },
        ],
        {atom_id},
        set(),
        drop_invalid_atoms=True,
    )

    assert [destination.title for destination in destinations] == ["Setup"]


def test_synthesis_accepts_existing_row_only_in_matching_category():
    atom_id = uuid4()
    row_id = uuid4()
    destinations = validate_synthesis_response(
        [
            {
                "category": "tools",
                "title": "Qdrant hybrid search",
                "summary": "Dense and sparse retrieval are fused in Qdrant.",
                "atom_ids": [str(atom_id)],
                "existing_row_id": str(row_id),
                "owner": None,
                "deadline": None,
                "status": None,
            }
        ],
        {atom_id},
        {"tools": {row_id}},
    )
    assert destinations[0].existing_row_id == row_id

    with pytest.raises(ValueError, match="wrong scope/category"):
        validate_synthesis_response(
            [
                {
                    "category": "projects",
                    "title": "Qdrant hybrid search",
                    "summary": "The same title is not a project match.",
                    "atom_ids": [str(atom_id)],
                    "existing_row_id": str(row_id),
                    "owner": None,
                    "deadline": None,
                    "status": None,
                }
            ],
            {atom_id},
            {"projects": set()},
        )


def test_low_confidence_synthesis_preserves_settled_summary():
    atom_id = uuid4()
    row_id = uuid4()
    destination = SynthesisDestinationModel(
        category="tools",
        title="Qdrant",
        summary="The settled summary was replaced.",
        atom_ids=[atom_id],
        existing_row_id=row_id,
    )
    context = SynthesisContext(
        scope="demo",
        new_atoms=[
            {
                "id": str(atom_id),
                "statement": "Qdrant may be used for a future feature.",
                "confidence": 0.7,
            }
        ],
        scene_names=["the user is discussing Qdrant"],
        existing_rows={
            "tools": [
                {
                    "id": row_id,
                    "title": "Qdrant",
                    "summary": "Qdrant is the confirmed vector store.",
                }
            ]
        },
    )

    assert gate_synthesis_summary(destination, context) == (
        "Qdrant is the confirmed vector store.\n\n"
        "Proposed, not yet applied:\n"
        "- Qdrant may be used for a future feature."
    )
