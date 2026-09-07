"""Dense vector retrieval."""

from typing import Any
from uuid import UUID

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
    vector = embeddings.embed_query(query).dense
    return query_points(
        client,
        collection_name=collection_name,
        query=vector,
        using="dense",
        query_filter=build_scope_filter(scope, sources, project_id),
        limit=top_k,
        with_payload=True,
    )
