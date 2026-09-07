from types import SimpleNamespace
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient

from memory_manager.embedding import (
    EmbeddingService,
    OpenAIEmbeddingEncoder,
    SparseVectorData,
    average_dense_vectors,
    chunk_text,
    merge_sparse_vectors,
)
from memory_manager.indexing.qdrant_writer import QdrantWriter
from memory_manager.retrieval.candidates import RawCandidate
from memory_manager.retrieval.expand_sources import (
    GENERAL_L2_CATEGORIES,
    PROJECT_L2_CATEGORIES,
    expand_sources,
)
from memory_manager.retrieval.fetch import fetch_atoms
from memory_manager.retrieval.modes import bm25, hybrid, vector
from memory_manager.retrieval.recall import resolve_mode, resolve_sources
from memory_manager.retrieval.rerank import (
    KeywordReranker,
    apply_threshold,
    normalize_candidate,
    parse_scores,
)


def test_openai_embedding_encoder_uses_configured_model_and_dimensions():
    class Embeddings:
        def __init__(self):
            self.request = None

        def create(self, **request):
            self.request = request
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])])

    class Client:
        def __init__(self):
            self.embeddings = Embeddings()

    client = Client()
    encoder = OpenAIEmbeddingEncoder(
        model="text-embedding-3-small",
        api_key="test-key",
        base_url="http://embedding.test/v1",
        dimensions=3,
        client=client,
    )

    assert encoder.encode("hello") == [0.1, 0.2, 0.3]
    assert client.embeddings.request == {
        "model": "text-embedding-3-small",
        "input": ["hello"],
        "dimensions": 3,
    }


def test_chunking_splits_long_prose_and_preserves_code_block():
    prose_chunks = chunk_text("word " * 30, 40)
    assert len(prose_chunks) > 1
    assert all(len(chunk) <= 40 for chunk in prose_chunks)

    code = "```python\n" + ("print('x')\n" * 20) + "```"
    code_chunks = chunk_text(code, 20)
    assert len(code_chunks) == 1
    assert code_chunks[0].startswith("```python")


def test_embedding_aggregation_validates_shapes():
    assert average_dense_vectors([[1.0, 0.0], [0.0, 1.0]]) == pytest.approx(
        [2**-0.5, 2**-0.5]
    )
    merged = merge_sparse_vectors(
        [SparseVectorData([1, 2], [0.2, 0.8]), SparseVectorData([2, 3], [0.9, 0.1])]
    )
    assert merged.indices == [1, 2, 3]
    assert merged.values == pytest.approx([0.2, 0.9, 0.1])


def test_recall_mode_and_source_validation():
    assert resolve_sources(None) == sorted(GENERAL_L2_CATEGORIES)
    assert "atoms" not in resolve_sources(None)
    assert resolve_sources(None, uuid4()) == sorted(PROJECT_L2_CATEGORIES)
    assert resolve_mode(None, ["atoms"]) == "vector"
    assert resolve_mode(None, ["tools"]) == "bm25"
    assert resolve_mode(None, ["atoms", "tools"]) == "hybrid"
    assert resolve_sources(["tools", "atoms"]) == ["atoms", "tools"]
    with pytest.raises(ValueError, match="unsupported recall source"):
        resolve_sources(["not-a-table"])
    with pytest.raises(ValueError, match="project_id is required"):
        resolve_sources(["project_sections"])


def test_type_expansion_never_implies_atoms():
    class CategoryDatabase:
        def fetch_all(self, query, params):
            return [
                {"category": "atoms"},
                {"category": "projects"},
            ]

    assert expand_sources(CategoryDatabase(), ["workspace"], None) == ["projects"]
    assert expand_sources(CategoryDatabase(), ["workspace"], ["atoms"]) == [
        "atoms",
        "projects",
    ]


def test_reranker_candidates_use_compact_contract_and_hide_atom_title():
    candidate = RawCandidate(
        uuid4(),
        "atoms",
        0.5,
        {"title": "should not be sent", "content": "The atom statement."},
    )

    assert normalize_candidate(candidate) == {
        "id": str(candidate.id),
        "title": None,
        "content": "The atom statement.",
    }


def test_threshold_uses_reranked_score():
    candidate = RawCandidate(uuid4(), "atoms", 0.99, {"content": "Qdrant retrieval"})
    scored = KeywordReranker().rerank("Postgres", [candidate])
    assert apply_threshold(scored, 0.5) == []
    assert apply_threshold(KeywordReranker().rerank("Qdrant", [candidate]), 0.5)


def test_reranker_scores_are_bounded_and_typed():
    candidate_id = uuid4()
    parsed = parse_scores([{"id": str(candidate_id), "score": 0.75}])
    assert parsed == [{"id": candidate_id, "score": 0.75}]
    with pytest.raises(ValueError, match="between 0 and 1"):
        parse_scores([{"id": str(candidate_id), "score": 1.1}])


def test_qdrant_writer_and_all_modes_use_one_collection():
    class DenseEncoder:
        def encode(self, text):
            return [1.0, 0.0]

    class SparseEncoder:
        def encode(self, text):
            return SparseVectorData([1], [1.0])

    client = QdrantClient(":memory:")
    embeddings = EmbeddingService(DenseEncoder(), SparseEncoder(), dimensions=2)
    writer = QdrantWriter(client, "memory", embeddings)
    writer.ensure_collection()
    writer.ensure_collection()
    point_id = "11111111-1111-4111-8111-111111111111"
    writer.index_atom(
        {
            "id": point_id,
            "scope": "demo",
            "statement": "Qdrant retrieval",
            "category": "fact",
            "origin": "asserted",
            "scene_name": "the user is testing retrieval",
            "predicate": None,
            "entity": None,
        }
    )
    search_args = (client, "memory", embeddings, "Qdrant", "demo", ["atoms"], 5)

    assert len(vector.search(*search_args)) == 1
    assert len(bm25.search(*search_args)) == 1
    assert len(hybrid.search(*search_args)) == 1

    writer.delete([point_id])
    assert vector.search(*search_args) == []


def test_qdrant_generic_and_project_atom_filters_are_disjoint():
    class DenseEncoder:
        def encode(self, text):
            return [1.0, 0.0]

    class SparseEncoder:
        def encode(self, text):
            return SparseVectorData([1], [1.0])

    client = QdrantClient(":memory:")
    embeddings = EmbeddingService(DenseEncoder(), SparseEncoder(), dimensions=2)
    writer = QdrantWriter(client, "memory", embeddings)
    writer.ensure_collection()
    project_id = uuid4()
    writer.index_atom(
        {
            "id": uuid4(),
            "scope": "demo",
            "statement": "generic memory",
            "category": "fact",
            "origin": "asserted",
            "scene_name": "generic",
            "project_id": None,
        }
    )
    writer.index_atom(
        {
            "id": uuid4(),
            "scope": "demo",
            "statement": "project memory",
            "category": "fact",
            "origin": "asserted",
            "scene_name": "project",
            "project_id": project_id,
        }
    )

    generic = vector.search(client, "memory", embeddings, "memory", "demo", ["atoms"], 5)
    project = vector.search(
        client,
        "memory",
        embeddings,
        "memory",
        "demo",
        ["atoms"],
        5,
        project_id,
    )

    assert [candidate.payload["content"] for candidate in generic] == ["generic memory"]
    assert [candidate.payload["content"] for candidate in project] == ["project memory"]


def test_fetch_atoms_requires_null_project_id_for_generic_hydration():
    class QueryDatabase:
        def __init__(self):
            self.query = ""

        def fetch_all(self, query, params):
            self.query = query
            return []

    database = QueryDatabase()
    fetch_atoms(database, [uuid4()], {}, "demo", None)

    assert "a.project_id is null" in database.query
