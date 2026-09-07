"""Post-retrieval scoring, calibrated thresholding, and reranker adapters."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from memory_manager.extraction.judge import JsonCompletionClient
from memory_manager.retrieval.candidates import RawCandidate


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: RawCandidate
    score: float


class Reranker(Protocol):
    def rerank(self, query: str, candidates: list[RawCandidate]) -> list[ScoredCandidate]:
        ...


class KeywordReranker:
    """Offline fallback with a bounded [0, 1] score for tests and local setup."""

    def rerank(self, query: str, candidates: list[RawCandidate]) -> list[ScoredCandidate]:
        query_terms = tokenize(query)
        scored: list[ScoredCandidate] = []
        for candidate in candidates:
            content = str(normalize_candidate(candidate)["content"])
            candidate_terms = tokenize(content)
            overlap = len(query_terms & candidate_terms) / max(len(query_terms), 1)
            retrieval_signal = min(max(candidate.retrieval_score, 0.0), 1.0)
            score = min(max((0.8 * overlap) + (0.2 * retrieval_signal), 0.0), 1.0)
            scored.append(ScoredCandidate(candidate=candidate, score=score))
        return sorted(scored, key=lambda item: item.score, reverse=True)


class LLMReranker:
    """Use a small JSON-scoring model after retrieval, never before it."""

    def __init__(
        self,
        client: JsonCompletionClient,
        prompt_path: str | Path,
    ) -> None:
        self.client = client
        self.prompt_path = Path(prompt_path)

    def rerank(self, query: str, candidates: list[RawCandidate]) -> list[ScoredCandidate]:
        if not candidates:
            return []
        prompt = self.prompt_path.read_text(encoding="utf-8")
        payload = {
            "query": query,
            "candidates": [normalize_candidate(candidate) for candidate in candidates],
        }
        raw_scores = self.client.complete_json(system_prompt=prompt, payload=payload)
        score_rows = parse_scores(raw_scores)
        by_id = {candidate.id: candidate for candidate in candidates}
        scored = [
            ScoredCandidate(candidate=by_id[row["id"]], score=row["score"])
            for row in score_rows
            if row["id"] in by_id
        ]
        seen = {item.candidate.id for item in scored}
        fallback = KeywordReranker().rerank(
            query,
            [candidate for candidate in candidates if candidate.id not in seen],
        )
        return sorted(scored + fallback, key=lambda item: item.score, reverse=True)


def normalize_candidate(candidate: RawCandidate) -> dict[str, object]:
    """Return the exact compact shape accepted by the reranker prompt."""
    raw_title = None if candidate.source_table == "atoms" else candidate.payload.get("title")
    title = None if raw_title is None else str(raw_title)
    content = candidate.payload.get("content", "")
    return {
        "id": str(candidate.id),
        "title": title,
        "content": "" if content is None else str(content),
    }


def parse_scores(raw_scores: object) -> list[dict[str, object]]:
    if not isinstance(raw_scores, list):
        raise ValueError("reranker must return a JSON array")
    parsed: list[dict[str, object]] = []
    for row in raw_scores:
        if not isinstance(row, dict) or "id" not in row or "score" not in row:
            raise ValueError("reranker rows require id and score")
        score = float(row["score"])
        if not 0 <= score <= 1:
            raise ValueError("reranker scores must be between 0 and 1")
        parsed.append({"id": UUID(str(row["id"])), "score": score})
    return parsed


def apply_threshold(
    scored_candidates: Iterable[ScoredCandidate],
    min_score: float | None,
) -> list[ScoredCandidate]:
    if min_score is not None and not 0 <= min_score <= 1:
        raise ValueError("min_score must be between 0 and 1")
    scored = list(scored_candidates)
    if min_score is None:
        return scored
    return [candidate for candidate in scored if candidate.score >= min_score]


def tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]{2,}", text.lower()))
