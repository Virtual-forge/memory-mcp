"""Sparse lexical retrieval using Qdrant's sparse vector index."""

from typing import Any
from uuid import UUID

from qdrant_client.models import SparseVector

from memory_manager.embedding import EmbeddingService
from memory_manager.retrieval.candidates import RawCandidate, build_scope_filter, query_points


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
    sparse = embeddings.embed_query(query).sparse
    return query_points(
        client,
        collection_name=collection_name,
        query=SparseVector(indices=sparse.indices, values=sparse.values),
        using="sparse",
        query_filter=build_scope_filter(scope, sources, project_id),
        limit=top_k,
        with_payload=True,
    )
