from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from memory_manager.extraction.judge import (
    ExtractionContext,
    ExtractionJudge,
    validate_judge_response,
)
from memory_manager.extraction.triggers import evaluate_triggers, inactivity_is_due


class StaticJsonClient:
    def __init__(self, response):
        self.response = response
        self.system_prompt = None

    def complete_json(self, *, system_prompt, payload):
        self.system_prompt = system_prompt
        return self.response


def test_judge_filters_low_confidence_and_keeps_provenance():
    turn_id = uuid4()
    corroborating_turn_id = uuid4()
    response = [
        {
            "scene_name": "the user is choosing a retrieval backend",
            "atoms": [
                {
                    "statement": "The project uses Qdrant for vector search.",
                    "category": "fact",
                    "entity": "Qdrant",
                    "predicate": "uses",
                    "confidence": 0.95,
                    "source_turn_ids": [str(turn_id), str(corroborating_turn_id)],
                    "supersedes": None,
                },
                {
                    "statement": "A weak guess should not be stored.",
                    "category": "fact",
                    "entity": None,
                    "predicate": None,
                    "confidence": 0.2,
                    "source_turn_ids": [str(turn_id), str(corroborating_turn_id)],
                    "supersedes": None,
                },
            ],
        }
    ]
    scenes = validate_judge_response(response, {turn_id, corroborating_turn_id})

    assert len(scenes) == 1
    assert len(scenes[0].atoms) == 1
    assert scenes[0].atoms[0].source_turn_ids == [turn_id, corroborating_turn_id]


def test_judge_rejects_source_turn_outside_batch():
    with pytest.raises(ValueError, match="outside the extraction batch"):
        validate_judge_response(
            [
                {
                    "scene_name": "the user is changing the memory schema",
                    "atoms": [
                        {
                            "statement": "This claim has no valid evidence.",
                            "category": "fact",
                            "entity": None,
                            "predicate": None,
                            "confidence": 0.9,
                            "source_turn_ids": [str(uuid4())],
                            "supersedes": None,
                        }
                    ],
                }
            ],
            {uuid4()},
        )


def test_project_judge_drops_atoms_with_invalid_source_ids():
    turn_id = uuid4()
    invalid_turn_id = uuid4()
    scenes = validate_judge_response(
        [
            {
                "scene_name": "the project documents its architecture",
                "atoms": [
                    {
                        "statement": "The project uses Postgres.",
                        "category": "fact",
                        "entity": None,
                        "predicate": "uses",
                        "confidence": 0.95,
                        "source_turn_ids": [str(turn_id), str(invalid_turn_id)],
                        "supersedes": None,
                    },
                    {
                        "statement": "The project uses Qdrant.",
                        "category": "fact",
                        "entity": None,
                        "predicate": "uses",
                        "confidence": 0.95,
                        "source_turn_ids": [str(turn_id)],
                        "supersedes": None,
                    },
                ],
            }
        ],
        {turn_id},
        require_corroboration=False,
        drop_invalid_sources=True,
    )

    assert [atom.statement for atom in scenes[0].atoms] == [
        "The project uses Qdrant."
    ]


def test_automatic_extraction_drops_single_turn_atoms():
    turn_id = uuid4()
    scenes = validate_judge_response(
        [
            {
                "scene_name": "the user is discussing a one-off request",
                "atoms": [
                    {
                        "statement": "The user made a one-off request.",
                        "category": "event",
                        "entity": None,
                        "predicate": None,
                        "confidence": 0.95,
                        "source_turn_ids": [str(turn_id)],
                        "supersedes": None,
                    }
                ],
            }
        ],
        {turn_id},
    )

    assert scenes[0].atoms == []


def test_extraction_judge_loads_prompt_from_disk():
    turn_id = uuid4()
    corroborating_turn_id = uuid4()
    client = StaticJsonClient(
        [
            {
                "scene_name": "the user is recording a durable preference",
                "atoms": [
                    {
                        "statement": "The user prefers precise memory extraction.",
                        "category": "preference",
                        "entity": None,
                        "predicate": "prefers",
                        "confidence": 0.9,
                        "source_turn_ids": [str(turn_id), str(corroborating_turn_id)],
                        "supersedes": None,
                    }
                ],
            }
        ]
    )
    judge = ExtractionJudge(
        client,
        Path(__file__).parents[1] / "prompts" / "extraction_judge.md",
    )
    scenes = judge.extract(
        ExtractionContext(
            new_turns=[
                {"id": turn_id, "content": "I prefer precise extraction."},
                {"id": corroborating_turn_id, "content": "Please keep extraction precise."},
            ],
            recent_scene=None,
            recent_atoms=[],
            known_entities=[],
        )
    )

    assert scenes[0].atoms[0].category == "preference"
    assert "memory extraction judge" in client.system_prompt


def test_triggers_use_last_turn_and_independent_volume_cap():
    now = datetime.now(UTC)
    decision = evaluate_triggers(
        last_turn_at=now - timedelta(minutes=16),
        unprocessed_turn_count=2,
        now=now,
        inactivity_minutes=15,
        volume_cap=20,
    )
    assert decision.inactivity_due is True
    assert decision.volume_due is False
    assert inactivity_is_due(None, now, 15) is False

    volume_decision = evaluate_triggers(
        last_turn_at=now,
        unprocessed_turn_count=20,
        now=now,
        inactivity_minutes=15,
        volume_cap=20,
    )
    assert volume_decision.due is True
    assert volume_decision.volume_due is True

    unchanged_batch = evaluate_triggers(
        last_turn_at=now - timedelta(minutes=16),
        unprocessed_turn_count=20,
        now=now,
        inactivity_minutes=15,
        volume_cap=20,
        has_new_turns=False,
    )
    assert unchanged_batch.due is False
