"""Application settings loaded from environment variables."""

import getpass
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for storage, models, and pipeline jobs."""

    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/memory_manager",
        description="Postgres connection string",
    )
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "memory"

    dense_embedding_backend: Literal["fastembed", "openai-compatible"] = "fastembed"
    dense_embedding_model: str = "BAAI/bge-small-en-v1.5"
    dense_embedding_dimensions: int | None = None
    sparse_embedding_model: str = "Qdrant/bm25"
    fastembed_cache_dir: str = Field(
        default_factory=lambda: str(Path.home() / ".cache" / "fastembed"),
        description="Persistent cache for local FastEmbed model assets",
    )
    embedding_max_tokens: int = 512
    content_chunk_chars: int = 4_000
    sparse_tokenizer_language: str = "english"

    llm_provider: str = "openai"
    llm_model: str = "gpt-5.6-luna"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_temperature: float | None = None
    reranker_model: str | None = None

    mcp_transport: Literal["stdio", "sse", "streamable-http"] = "streamable-http"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000

    memory_user_id: str | None = None
    default_scope: str | None = None
    memory_mcp_timeout_seconds: int = 60
    extraction_inactivity_minutes: int = 15
    extraction_volume_cap: int = 20
    synthesis_atom_volume: int = 50
    synthesis_inactivity_minutes: int = 15
    worker_poll_seconds: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator(
        "default_scope",
        "memory_user_id",
        "qdrant_collection",
        "dense_embedding_model",
        "sparse_embedding_model",
        "fastembed_cache_dir",
        "sparse_tokenizer_language",
        "mcp_host",
    )
    @classmethod
    def require_text(cls, value: str | None) -> str | None:
        """Reject empty identifiers that would make writes ambiguous."""
        if value is not None and not value.strip():
            raise ValueError("setting must not be empty")
        return value

    @model_validator(mode="after")
    def resolve_default_scope(self) -> "Settings":
        user_id = (self.memory_user_id or getpass.getuser()).strip()
        if not user_id:
            raise ValueError("memory user id must not be empty")
        self.memory_user_id = user_id
        if self.default_scope is None:
            self.default_scope = f"user:{user_id}"
        return self

    @property
    def resolved_default_scope(self) -> str:
        if self.default_scope is None:
            raise RuntimeError("default scope was not resolved")
        return self.default_scope

    @field_validator(
        "embedding_max_tokens",
        "content_chunk_chars",
        "extraction_inactivity_minutes",
        "extraction_volume_cap",
        "synthesis_atom_volume",
        "synthesis_inactivity_minutes",
        "worker_poll_seconds",
        "mcp_port",
        "memory_mcp_timeout_seconds",
    )
    @classmethod
    def require_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("setting must be positive")
        return value

    @field_validator("dense_embedding_dimensions")
    @classmethod
    def require_valid_dimensions(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("dense_embedding_dimensions must be positive")
        return value

    @field_validator("llm_temperature")
    @classmethod
    def require_valid_temperature(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 2:
            raise ValueError("llm_temperature must be between 0 and 2")
        return value
