"""Shared candidate shapes and Qdrant filter/response helpers."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class RawCandidate:
    id: UUID
    source_table: str
    retrieval_score: float
    payload: dict[str, object]


@dataclass(frozen=True)
class CandidateStub:
    id: UUID
    title: str
    source_table: str


def build_scope_filter(
    scope: str | None,
    sources: list[str] | None = None,
    project_id: UUID | None = None,
) -> Any:
    from qdrant_client.models import (
        FieldCondition,
        Filter,
        IsEmptyCondition,
        IsNullCondition,
        MatchAny,
        MatchValue,
        PayloadField,
    )

    must: list[Any] = []
    if scope is not None:
        must.append(FieldCondition(key="scope", match=MatchValue(value=scope)))
    if sources:
        must.append(FieldCondition(key="source_table", match=MatchAny(any=sources)))
    if project_id is not None:
        must.append(
            FieldCondition(key="project_id", match=MatchValue(value=str(project_id)))
        )
    else:
        must.append(
            Filter(
                should=[
                    IsNullCondition(is_null=PayloadField(key="project_id")),
                    IsEmptyCondition(is_empty=PayloadField(key="project_id")),
                ]
            )
        )
    return Filter(
        must=must or None,
        must_not=[
            FieldCondition(key="source_deleted", match=MatchValue(value=True)),
            FieldCondition(key="superseded", match=MatchValue(value=True)),
        ],
    )


def query_points(client: Any, **kwargs: object) -> list[RawCandidate]:
    response = client.query_points(**kwargs)
    points = getattr(response, "points", response)
    return [point_to_candidate(point) for point in points]


def point_to_candidate(point: Any) -> RawCandidate:
    payload = dict(getattr(point, "payload", None) or {})
    point_id = UUID(str(getattr(point, "id")))
    return RawCandidate(
        id=point_id,
        source_table=str(payload.get("source_table", "")),
        retrieval_score=float(getattr(point, "score", 0.0)),
        payload=payload,
    )


def deduplicate_candidates(candidates: list[RawCandidate]) -> list[RawCandidate]:
    best_by_id: dict[UUID, RawCandidate] = {}
    for candidate in candidates:
        previous = best_by_id.get(candidate.id)
        if previous is None or candidate.retrieval_score > previous.retrieval_score:
            best_by_id[candidate.id] = candidate
    return sorted(best_by_id.values(), key=lambda item: item.retrieval_score, reverse=True)
