"""Runtime assembly for storage, MCP operations, and scheduled jobs."""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memory_manager.agno_capture import AgnoMemoryCapture, L0Pipeline
from memory_manager.config import Settings
from memory_manager.db.connection import Database
from memory_manager.embedding import (
    EmbeddingService,
    FastEmbedEncoderPair,
    FastEmbedSparseEncoder,
    OpenAIEmbeddingEncoder,
)
from memory_manager.extraction.judge import ExtractionJudge
from memory_manager.indexing.qdrant_writer import QdrantWriter
from memory_manager.ingestion.turns import TurnWriter
from memory_manager.llm import OpenAIJsonClient
from memory_manager.mcp.tools import MemoryTools
from memory_manager.pipeline import MemoryPipeline
from memory_manager.retrieval.recall import RecallService
from memory_manager.retrieval.rerank import KeywordReranker, LLMReranker, Reranker
from memory_manager.synthesis.judge import SynthesisJudge
from memory_manager.synthesis.project import ProjectSynthesisJudge


@dataclass(frozen=True)
class StorageRuntime:
    database: Database
    qdrant_client: Any
    embeddings: EmbeddingService
    qdrant_writer: QdrantWriter
    recall_service: RecallService


class _DenseAdapter:
    def __init__(self, pair: FastEmbedEncoderPair) -> None:
        self.pair = pair

    def encode(self, text: str) -> list[float]:
        return list(self.pair.dense_encode(text))


class _SparseAdapter:
    def __init__(self, pair: FastEmbedEncoderPair) -> None:
        self.pair = pair

    def encode(self, text: str):
        return self.pair.sparse_encode(text)


def build_storage_runtime(
    settings: Settings | None = None,
    reranker: Reranker | None = None,
) -> StorageRuntime:
    settings = settings or Settings()
    configure_system_tls()
    database = Database(settings.database_url)
    embeddings = build_embedding_service(settings)
    qdrant_client = build_qdrant_client(settings)
    qdrant_writer = QdrantWriter(qdrant_client, settings.qdrant_collection, embeddings)
    qdrant_writer.ensure_collection()
    selected_reranker = reranker or build_reranker(settings)
    recall_service = RecallService(
        database=database,
        qdrant_client=qdrant_client,
        collection_name=settings.qdrant_collection,
        embeddings=embeddings,
        reranker=selected_reranker,
    )
    return StorageRuntime(database, qdrant_client, embeddings, qdrant_writer, recall_service)


def configure_system_tls() -> None:
    """Use the Windows certificate store for HTTPS SDK clients."""
    if sys.platform != "win32":
        return
    try:
        import truststore
    except ImportError:
        return
    truststore.inject_into_ssl()


def build_embedding_service(settings: Settings) -> EmbeddingService:
    if settings.dense_embedding_backend == "openai-compatible":
        if not settings.llm_api_key:
            raise RuntimeError(
                "LLM_API_KEY is required for OpenAI-compatible dense embeddings"
            )
        dense_encoder = OpenAIEmbeddingEncoder(
            model=settings.dense_embedding_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            dimensions=settings.dense_embedding_dimensions,
        )
        sparse_encoder = FastEmbedSparseEncoder(
            settings.sparse_embedding_model,
            settings.sparse_tokenizer_language,
            settings.fastembed_cache_dir,
        )
    else:
        pair = FastEmbedEncoderPair(
            settings.dense_embedding_model,
            settings.sparse_embedding_model,
            settings.sparse_tokenizer_language,
            settings.fastembed_cache_dir,
        )
        dense_encoder = _DenseAdapter(pair)
        sparse_encoder = _SparseAdapter(pair)
    return EmbeddingService(
        dense_encoder=dense_encoder,
        sparse_encoder=sparse_encoder,
        dimensions=settings.dense_embedding_dimensions,
        chunk_chars=settings.content_chunk_chars,
        max_tokens=settings.embedding_max_tokens,
    )


def build_memory_tools(settings: Settings | None = None) -> MemoryTools:
    settings = settings or Settings()
    storage = build_storage_runtime(settings)
    return MemoryTools(
        database=storage.database,
        recall_service=storage.recall_service,
        qdrant_writer=storage.qdrant_writer,
        default_scope=settings.resolved_default_scope,
    )


def build_memory_pipeline(settings: Settings | None = None) -> MemoryPipeline:
    settings = settings or Settings()
    storage = build_storage_runtime(settings)
    llm_client = build_llm_client(settings)
    prompts_dir = Path(__file__).resolve().parents[1] / "prompts"
    return MemoryPipeline(
        database=storage.database,
        turn_writer=TurnWriter(storage.database),
        extraction_judge=ExtractionJudge(llm_client, prompts_dir / "extraction_judge.md"),
        project_extraction_judge=ExtractionJudge(
            llm_client,
            prompts_dir / "project_extraction_judge.md",
        ),
        synthesis_judge=SynthesisJudge(llm_client, prompts_dir / "l2_synthesis.md"),
        project_synthesis_judge=ProjectSynthesisJudge(
            llm_client,
            prompts_dir / "project_l2_synthesis.md",
        ),
        qdrant_writer=storage.qdrant_writer,
        inactivity_minutes=settings.extraction_inactivity_minutes,
        extraction_volume_cap=settings.extraction_volume_cap,
        synthesis_atom_volume=settings.synthesis_atom_volume,
    )


def build_memory_capture(settings: Settings | None = None) -> AgnoMemoryCapture:
    settings = settings or Settings()
    return AgnoMemoryCapture(
        pipeline=L0Pipeline(TurnWriter(Database(settings.database_url))),
        scope=settings.resolved_default_scope,
    )


def build_qdrant_client(settings: Settings) -> Any:
    from qdrant_client import QdrantClient

    return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)


def build_llm_client(settings: Settings) -> OpenAIJsonClient:
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY is required for extraction, synthesis, or LLM reranking")
    return OpenAIJsonClient(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=settings.llm_temperature,
    )


def build_reranker(settings: Settings) -> Reranker:
    if not settings.reranker_model:
        return KeywordReranker()
    return LLMReranker(
        build_llm_client(settings),
        Path(__file__).resolve().parents[1] / "prompts" / "reranker.md",
    )
