"""Native Qdrant RRF fusion for dense plus sparse retrieval."""

from typing import Any
from uuid import UUID

from qdrant_client.models import Fusion, FusionQuery, Prefetch, SparseVector

from memory_manager.embedding import EmbeddingService
from memory_manager.retrieval.candidates import (
    RawCandidate,
    build_scope_filter,
    deduplicate_candidates,
    query_points,
)


def search(
    client: Any,
    collection_name: str,
    embeddings: EmbeddingService,
    query: str,
    scope: str | None,
    sources: list[str] | None,
    top_k: int,
    project_id: UUID | None = None,
) -> list[RawCandidate]:
    pair = embeddings.embed_query(query)
    query_filter = build_scope_filter(scope, sources, project_id)
    candidates = query_points(
        client,
        collection_name=collection_name,
        prefetch=[
            Prefetch(query=pair.dense, using="dense", limit=top_k, filter=query_filter),
            Prefetch(
                query=SparseVector(indices=pair.sparse.indices, values=pair.sparse.values),
                using="sparse",
                limit=top_k,
                filter=query_filter,
            ),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        query_filter=query_filter,
        limit=top_k,
        with_payload=True,
    )
    return deduplicate_candidates(candidates)
