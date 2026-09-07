"""Write atoms and L2 rows into one shared Qdrant collection."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from memory_manager.embedding import EmbeddingService


class QdrantWriter:
    """Maintain a rebuildable dense+sparse index derived from Postgres rows."""

    def __init__(
        self,
        client: Any,
        collection_name: str,
        embeddings: EmbeddingService,
    ) -> None:
        self.client = client
        self.collection_name = collection_name
        self.embeddings = embeddings

    def ensure_collection(self) -> None:
        from qdrant_client.models import (
            Distance,
            SparseVectorParams,
            VectorParams,
        )

        dimensions = self.embeddings.validate_dimensions()
        if self.client.collection_exists(self.collection_name):
            collection = self.client.get_collection(self.collection_name)
            vectors = collection.config.params.vectors
            dense_vector = vectors.get("dense") if isinstance(vectors, dict) else vectors
            existing_dimensions = getattr(dense_vector, "size", None)
            if existing_dimensions is not None and existing_dimensions != dimensions:
                raise ValueError(
                    "Qdrant collection dense dimensions do not match the embedding model: "
                    f"collection={existing_dimensions}, model={dimensions}"
                )
            self._ensure_filter_indexes()
            return
        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config={"dense": VectorParams(size=dimensions, distance=Distance.COSINE)},
            sparse_vectors_config={"sparse": SparseVectorParams()},
        )
        self._ensure_filter_indexes()

    def _ensure_filter_indexes(self) -> None:
        from qdrant_client.models import PayloadSchemaType

        fields = {
            "scope": PayloadSchemaType.KEYWORD,
            "source_table": PayloadSchemaType.KEYWORD,
            "source_deleted": PayloadSchemaType.BOOL,
            "superseded": PayloadSchemaType.BOOL,
            "project_id": PayloadSchemaType.KEYWORD,
        }
        for field_name, field_schema in fields.items():
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name=field_name,
                field_schema=field_schema,
            )

    def index_atom(self, atom: dict[str, object]) -> None:
        payload = {
            "scope": atom["scope"],
            "source_table": "atoms",
            "title": atom["statement"],
            "content": atom["statement"],
            "category": atom["category"],
            "origin": atom["origin"],
            "scene_name": atom["scene_name"],
            "predicate": atom.get("predicate"),
            "entity": atom.get("entity"),
            "source_deleted": bool(atom.get("source_deleted", False)),
            "superseded": bool(atom.get("superseded", False)),
            "project_id": atom.get("project_id"),
        }
        self._upsert(atom["id"], atom["statement"], payload)

    def index_l2_row(self, row: dict[str, object]) -> None:
        source_table = str(row["category"])
        content = str(row["summary"])
        payload = {
            "scope": row["scope"],
            "source_table": source_table,
            "title": row["title"],
            "content": content,
            "category": source_table,
            "owner": row.get("owner"),
            "deadline": row.get("deadline"),
            "status": row.get("status"),
            "project_id": row.get("project_id"),
        }
        self._upsert(row["id"], content, payload, breadcrumb=str(row["title"]))

    def index_records(
        self,
        atoms: list[dict[str, object]],
        l2_rows: list[dict[str, object]],
    ) -> None:
        for atom in atoms:
            self.index_atom(atom)
        for row in l2_rows:
            self.index_l2_row(row)

    def delete(self, ids: list[UUID | str]) -> None:
        from qdrant_client.models import PointIdsList

        self.client.delete(
            collection_name=self.collection_name,
            points_selector=PointIdsList(points=[str(point_id) for point_id in ids]),
        )

    def _upsert(
        self,
        point_id: UUID | str | object,
        content: str,
        payload: dict[str, object],
        breadcrumb: str = "",
    ) -> None:
        from qdrant_client.models import PointStruct, SparseVector

        vector = self.embeddings.embed_document(content, breadcrumb)
        self.client.upsert(
            collection_name=self.collection_name,
            points=[
                PointStruct(
                    id=str(point_id),
                    vector={
                        "dense": vector.dense,
                        "sparse": SparseVector(
                            indices=vector.sparse.indices,
                            values=vector.sparse.values,
                        ),
                    },
                    payload=to_json_payload(payload),
                )
            ],
        )


def to_json_payload(payload: dict[str, object]) -> dict[str, object]:
    return {key: json_value(value) for key, value in payload.items()}


def json_value(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value
