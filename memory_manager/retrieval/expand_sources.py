"""Resolve semantic category types into concrete Qdrant source tables."""

from collections.abc import Iterable
from uuid import UUID

from memory_manager.db.connection import Database
from memory_manager.synthesis.routing import (
    GENERAL_L2_CATEGORIES,
    L2_CATEGORIES,
    PROJECT_L2_CATEGORIES,
)

ALL_SOURCES = frozenset({"atoms", *L2_CATEGORIES})


def expand_sources(
    database: Database,
    types: Iterable[str] | None,
    sources: Iterable[str] | None,
    project_id: UUID | None = None,
) -> list[str]:
    """Union type-derived categories with explicit sources and validate all names."""
    explicit = set(sources or [])
    invalid = explicit - ALL_SOURCES
    if invalid:
        raise ValueError(f"unsupported recall source(s): {sorted(invalid)}")
    if project_id is None and explicit & PROJECT_L2_CATEGORIES:
        raise ValueError("project_id is required for project documentation sources")

    expanded: set[str] = set()
    requested_types = set(types or [])
    unknown_types = requested_types - {"agent", "workspace", "user"}
    if unknown_types:
        raise ValueError(f"unsupported recall type(s): {sorted(unknown_types)}")
    if requested_types:
        rows = database.fetch_all(
            """
            select category
            from category_types
            where type = any(%s)
            order by category
            """,
            (list(requested_types),),
        )
        expanded = {str(row["category"]) for row in rows}
        expanded.discard("atoms")
    if project_id is None:
        expanded -= PROJECT_L2_CATEGORIES
    if not requested_types and not explicit:
        return sorted(PROJECT_L2_CATEGORIES if project_id is not None else GENERAL_L2_CATEGORIES)
    return sorted(expanded | explicit)


def natural_default_mode(sources: list[str] | None) -> str:
    """Pick a single-category default; mixed categories deliberately use hybrid."""
    if not sources or len(sources) != 1:
        return "hybrid"
    source = sources[0]
    if source in {"profiles", "users", "atoms"}:
        return "vector"
    return "bm25"
