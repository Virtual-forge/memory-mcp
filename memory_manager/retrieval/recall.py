"""Thin recall orchestrator over source expansion, mode search, rerank, and fetch."""

from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from typing import Any, Literal
from uuid import UUID

from memory_manager.db.connection import Database
from memory_manager.embedding import EmbeddingService
from memory_manager.models.atom import AtomResult
from memory_manager.retrieval.candidates import CandidateStub, RawCandidate, deduplicate_candidates
from memory_manager.retrieval.expand_sources import (
    ALL_SOURCES,
    GENERAL_L2_CATEGORIES,
    PROJECT_L2_CATEGORIES,
    natural_default_mode,
)
from memory_manager.retrieval.fetch import fetch_by_ids, fetch_results
from memory_manager.retrieval.rerank import Reranker, apply_threshold

RetrievalMode = Literal["vector", "bm25", "hybrid"]
MAX_RECALL_ATTEMPTS = 3


class RecallAttemptBudget:
    """Track the bounded, staged memory lookups for each agent run."""

    def __init__(self, max_attempts: int = MAX_RECALL_ATTEMPTS, max_runs: int = 1024) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if max_runs <= 0:
            raise ValueError("max_runs must be positive")
        self.max_attempts = max_attempts
        self.max_runs = max_runs
        self._attempts: OrderedDict[str, int] = OrderedDict()
        self._lock = Lock()

    def consume(self, run_id: str) -> int | None:
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        with self._lock:
            current = self._attempts.get(run_id, 0)
            if current >= self.max_attempts:
                self._attempts.move_to_end(run_id)
                return None
            attempt = current + 1
            self._attempts[run_id] = attempt
            self._attempts.move_to_end(run_id)
            while len(self._attempts) > self.max_runs:
                self._attempts.popitem(last=False)
            return attempt


def recall_sources_for_attempt(attempt: int, project_id: UUID | None = None) -> list[str]:
    """Return the source tables for the staged agent recall policy."""
    if attempt < 1 or attempt > MAX_RECALL_ATTEMPTS:
        raise ValueError(f"recall attempt must be between 1 and {MAX_RECALL_ATTEMPTS}")
    l2_sources = sorted(
        PROJECT_L2_CATEGORIES if project_id is not None else GENERAL_L2_CATEGORIES
    )
    return ["atoms", *l2_sources] if attempt == MAX_RECALL_ATTEMPTS else l2_sources


class RecallService:
    """Coordinate retrieval stages while keeping each stage in its own module."""

    def __init__(
        self,
        database: Database,
        qdrant_client: Any,
        collection_name: str,
        embeddings: EmbeddingService,
        reranker: Reranker,
    ) -> None:
        self.database = database
        self.qdrant_client = qdrant_client
        self.collection_name = collection_name
        self.embeddings = embeddings
        self.reranker = reranker

    def recall(
        self,
        query: str,
        scope: str | None,
        mode: RetrievalMode | None = None,
        sources: list[str] | None = None,
        top_k: int = 8,
        min_score: float | None = None,
        project_id: UUID | None = None,
    ) -> list[AtomResult]:
        validate_recall_inputs(query, scope, top_k)
        resolved_sources = resolve_sources(sources, project_id)
        selected_mode = resolve_mode(mode, resolved_sources)
        candidates = search_candidates_by_mode(
            selected_mode,
            self.qdrant_client,
            self.collection_name,
            self.embeddings,
            query,
            scope,
            resolved_sources,
            top_k,
            project_id,
        )
        scored = apply_threshold(self.reranker.rerank(query, candidates), min_score)
        score_map = {item.candidate.id: item.score for item in scored}
        return fetch_results(
            self.database,
            list(score_map),
            scores=score_map,
            scope=scope,
            project_id=project_id,
        )

    def search_candidates(
        self,
        query: str,
        scope: str | None,
        mode: RetrievalMode | None = None,
        sources: list[str] | None = None,
        top_k: int = 20,
        project_id: UUID | None = None,
    ) -> list[CandidateStub]:
        validate_recall_inputs(query, scope, top_k)
        resolved_sources = resolve_sources(sources, project_id)
        selected_mode = resolve_mode(mode, resolved_sources)
        candidates = search_candidates_by_mode(
            selected_mode,
            self.qdrant_client,
            self.collection_name,
            self.embeddings,
            query,
            scope,
            resolved_sources,
            top_k,
            project_id,
        )
        return [
            CandidateStub(
                id=candidate.id,
                title=str(candidate.payload.get("title", "")),
                source_table=candidate.source_table,
            )
            for candidate in deduplicate_candidates(candidates)[:top_k]
        ]

    def fetch(
        self,
        ids: list[str],
        scope: str | None = None,
        project_id: UUID | None = None,
    ) -> list[AtomResult]:
        return fetch_by_ids(self.database, parse_ids(ids), scope=scope, project_id=project_id)


def validate_recall_inputs(query: str, scope: str | None, top_k: int) -> None:
    if not query.strip():
        raise ValueError("query must not be empty")
    if scope is not None and not scope.strip():
        raise ValueError("scope must not be empty")
    if top_k <= 0:
        raise ValueError("top_k must be positive")


def resolve_sources(
    sources: list[str] | None,
    project_id: UUID | None = None,
) -> list[str]:
    default_sources = PROJECT_L2_CATEGORIES if project_id is not None else GENERAL_L2_CATEGORIES
    resolved = sorted(default_sources if not sources else set(sources))
    invalid = set(resolved) - ALL_SOURCES
    if invalid:
        raise ValueError(f"unsupported recall source(s): {sorted(invalid)}")
    if project_id is None and "project_sections" in resolved:
        raise ValueError("project_id is required for project_sections retrieval")
    return resolved


def resolve_mode(mode: RetrievalMode | None, sources: list[str] | None) -> RetrievalMode:
    if mode is not None:
        if mode not in {"vector", "bm25", "hybrid"}:
            raise ValueError(f"unsupported retrieval mode: {mode}")
        return mode
    return natural_default_mode(sources)


def search_candidates_by_mode(
    mode: RetrievalMode,
    client: Any,
    collection_name: str,
    embeddings: EmbeddingService,
    query: str,
    scope: str,
    sources: list[str],
    top_k: int,
    project_id: UUID | None = None,
) -> list[RawCandidate]:
    from memory_manager.retrieval.modes import bm25, hybrid, vector

    searcher: Callable[..., list[RawCandidate]]
    if mode == "vector":
        searcher = vector.search
    elif mode == "bm25":
        searcher = bm25.search
    else:
        searcher = hybrid.search
    return searcher(
        client,
        collection_name,
        embeddings,
        query,
        scope,
        sources,
        top_k,
        project_id,
    )


def parse_ids(ids: list[str]) -> list[Any]:
    from uuid import UUID

    try:
        return [UUID(value) for value in ids]
    except ValueError as exc:
        raise ValueError("all ids must be UUIDs") from exc
