"""Shared dense/sparse embedding and boundary-aware chunking."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SparseVectorData:
    indices: list[int]
    values: list[float]


@dataclass(frozen=True)
class EmbeddingPair:
    dense: list[float]
    sparse: SparseVectorData


class DenseEncoder(Protocol):
    def encode(self, text: str) -> Sequence[float]:
        ...


class SparseEncoder(Protocol):
    def encode(self, text: str) -> SparseVectorData:
        ...


class EmbeddingService:
    """Share exactly one dense/sparse encoder pair across indexing and recall."""

    def __init__(
        self,
        dense_encoder: DenseEncoder,
        sparse_encoder: SparseEncoder,
        dimensions: int | None = None,
        chunk_chars: int = 4_000,
        max_tokens: int = 512,
    ) -> None:
        self.dense_encoder = dense_encoder
        self.sparse_encoder = sparse_encoder
        self.configured_dimensions = dimensions
        self.chunk_chars = chunk_chars
        self.max_tokens = max_tokens
        self._validated_dimensions: int | None = None

    def validate_dimensions(self) -> int:
        """Probe the configured encoder at startup instead of first query failure."""
        actual = len(self.dense_encoder.encode("memory dimension validation"))
        if actual == 0:
            raise ValueError("dense embedding model returned an empty vector")
        if self.configured_dimensions is not None and actual != self.configured_dimensions:
            raise ValueError(
                "configured dense embedding dimensions do not match model output: "
                f"configured={self.configured_dimensions}, actual={actual}"
            )
        self._validated_dimensions = actual
        return actual

    @property
    def dimensions(self) -> int:
        if self._validated_dimensions is None:
            return self.validate_dimensions()
        return self._validated_dimensions

    def embed_query(self, text: str) -> EmbeddingPair:
        return self.embed_document(text, breadcrumb="query")

    def embed_document(self, text: str, breadcrumb: str = "") -> EmbeddingPair:
        chunks = chunk_text(
            text,
            min(self.chunk_chars, self.max_tokens * 4),
            breadcrumb,
        )
        dense_vectors = [list(self.dense_encoder.encode(chunk)) for chunk in chunks]
        sparse_vectors = [self.sparse_encoder.encode(chunk) for chunk in chunks]
        return EmbeddingPair(
            dense=average_dense_vectors(dense_vectors),
            sparse=merge_sparse_vectors(sparse_vectors),
        )


def chunk_text(text: str, max_chars: int, breadcrumb: str = "") -> list[str]:
    """Pack headings/paragraphs without splitting fenced code blocks."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    normalized = text.strip()
    if not normalized:
        return [breadcrumb.strip()] if breadcrumb.strip() else [""]
    breadcrumb_overhead = breadcrumb_overhead_chars(breadcrumb)
    if breadcrumb_overhead >= max_chars:
        raise ValueError("breadcrumb leaves no room for embedded content")
    content_limit = max_chars - breadcrumb_overhead
    if len(normalized) <= content_limit:
        return [with_breadcrumb(normalized, breadcrumb)]

    blocks = []
    for block in split_into_blocks(normalized):
        if len(block) > content_limit and not is_code_block(block):
            blocks.extend(split_long_text(block, content_limit))
        else:
            blocks.append(block)
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for block in blocks:
        block_length = len(block) + (2 if current else 0)
        if current and current_length + block_length > content_limit:
            chunks.append(with_breadcrumb("\n\n".join(current), breadcrumb))
            current = []
            current_length = 0
        current.append(block)
        current_length += block_length
    if current:
        chunks.append(with_breadcrumb("\n\n".join(current), breadcrumb))
    return chunks


def split_into_blocks(text: str) -> list[str]:
    lines = text.splitlines()
    blocks: list[str] = []
    current: list[str] = []
    in_code_block = False

    def flush() -> None:
        if current:
            blocks.append("\n".join(current).strip())
            current.clear()

    for line in lines:
        is_fence = line.lstrip().startswith("```")
        if is_fence:
            current.append(line)
            in_code_block = not in_code_block
            if not in_code_block:
                flush()
            continue
        if in_code_block:
            current.append(line)
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            flush()
            if line.strip():
                blocks.append(line.strip())
            continue
        current.append(line)
    flush()
    return [block for block in blocks if block]


def is_code_block(block: str) -> bool:
    return block.lstrip().startswith("```")


def split_long_text(text: str, max_chars: int) -> list[str]:
    """Split one oversized prose block only at whitespace boundaries."""
    pieces: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_chars:
        split_at = remaining.rfind(" ", 0, max_chars + 1)
        if split_at <= 0:
            split_at = max_chars
        pieces.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def with_breadcrumb(text: str, breadcrumb: str) -> str:
    clean_breadcrumb = breadcrumb.strip()
    return f"{clean_breadcrumb}\n\n{text}" if clean_breadcrumb else text


def breadcrumb_overhead_chars(breadcrumb: str) -> int:
    clean_breadcrumb = breadcrumb.strip()
    return len(clean_breadcrumb) + 2 if clean_breadcrumb else 0


def average_dense_vectors(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        raise ValueError("cannot average zero dense vectors")
    dimensions = len(vectors[0])
    if dimensions == 0 or any(len(vector) != dimensions for vector in vectors):
        raise ValueError("dense vectors have inconsistent dimensions")
    averaged = [
        sum(vector[index] for vector in vectors) / len(vectors)
        for index in range(dimensions)
    ]
    magnitude = math.sqrt(sum(value * value for value in averaged))
    if magnitude == 0:
        return averaged
    return [value / magnitude for value in averaged]


def merge_sparse_vectors(vectors: list[SparseVectorData]) -> SparseVectorData:
    weights: dict[int, float] = {}
    for vector in vectors:
        if len(vector.indices) != len(vector.values):
            raise ValueError("sparse vector indices and values differ in length")
        for index, value in zip(vector.indices, vector.values):
            weights[index] = max(weights.get(index, 0.0), value)
    ordered = sorted(weights.items())
    return SparseVectorData(
        indices=[index for index, _ in ordered],
        values=[value for _, value in ordered],
    )


class OpenAIEmbeddingEncoder:
    """Use an OpenAI-compatible embeddings endpoint for dense vectors."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        dimensions: int | None = None,
        client: Any | None = None,
    ) -> None:
        if client is None:
            from openai import OpenAI

            kwargs: dict[str, object] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = OpenAI(**kwargs)
        self.client = client
        self.model = model
        self.dimensions = dimensions

    def encode(self, text: str) -> Sequence[float]:
        request: dict[str, object] = {"model": self.model, "input": [text]}
        if self.dimensions is not None:
            request["dimensions"] = self.dimensions
        response = self.client.embeddings.create(**request)
        data = getattr(response, "data", None)
        if not data or len(data) != 1:
            raise ValueError("embedding endpoint returned an unexpected number of vectors")
        embedding = getattr(data[0], "embedding", None)
        if embedding is None:
            raise ValueError("embedding endpoint returned no vector")
        return list(embedding)


class FastEmbedSparseEncoder:
    """Local FastEmbed adapter for the shared sparse BM25 vector."""

    def __init__(
        self,
        sparse_model: str,
        language: str = "english",
        cache_dir: str | None = None,
    ) -> None:
        from fastembed import SparseTextEmbedding

        sparse_options: dict[str, Any] = {}
        if sparse_model.lower() == "qdrant/bm25":
            sparse_options["language"] = language
        if cache_dir is not None:
            sparse_options["cache_dir"] = cache_dir
        self.sparse = SparseTextEmbedding(model_name=sparse_model, **sparse_options)

    def encode(self, text: str) -> SparseVectorData:
        value = next(iter(self.sparse.embed([text])))
        return SparseVectorData(indices=list(value.indices), values=list(value.values))


class FastEmbedEncoderPair:
    """Lazy adapter for FastEmbed's dense and BM25 encoders."""

    def __init__(
        self,
        dense_model: str,
        sparse_model: str,
        sparse_tokenizer_language: str = "english",
        cache_dir: str | None = None,
    ) -> None:
        from fastembed import TextEmbedding

        dense_options = {"cache_dir": cache_dir} if cache_dir is not None else {}
        self.dense = TextEmbedding(model_name=dense_model, **dense_options)
        self.sparse = FastEmbedSparseEncoder(
            sparse_model,
            sparse_tokenizer_language,
            cache_dir,
        )

    def dense_encode(self, text: str) -> Sequence[float]:
        return list(next(iter(self.dense.embed([text]))))

    def sparse_encode(self, text: str) -> SparseVectorData:
        return self.sparse.encode(text)
