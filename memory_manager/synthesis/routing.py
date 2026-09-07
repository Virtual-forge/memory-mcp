"""Fixed category routing metadata and validation."""

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class L2TableDefinition:
    category: str
    table_name: str
    junction_table: str
    foreign_key: str
    has_task_fields: bool = False
    project_scoped: bool = False


L2_TABLES: dict[str, L2TableDefinition] = {
    "projects": L2TableDefinition("projects", "projects", "project_atoms", "project_id"),
    "docs": L2TableDefinition("docs", "docs", "doc_atoms", "doc_id"),
    "tasks": L2TableDefinition("tasks", "tasks", "task_atoms", "task_id", True),
    "tools": L2TableDefinition("tools", "tools", "tool_atoms", "tool_id"),
    "skills": L2TableDefinition("skills", "skills", "skill_atoms", "skill_id"),
    "mcp": L2TableDefinition("mcp", "mcp", "mcp_atoms", "mcp_id"),
    "workflows": L2TableDefinition("workflows", "workflows", "workflow_atoms", "workflow_id"),
    "profiles": L2TableDefinition("profiles", "profiles", "profile_atoms", "profile_id"),
    "users": L2TableDefinition("users", "users", "user_atoms", "user_id"),
    "project_sections": L2TableDefinition(
        "project_sections",
        "project_sections",
        "project_section_atoms",
        "section_id",
        project_scoped=True,
    ),
}

L2_CATEGORIES = frozenset(L2_TABLES)
GENERAL_L2_TABLES = {
    name: definition for name, definition in L2_TABLES.items() if not definition.project_scoped
}
PROJECT_L2_TABLES = {
    name: definition for name, definition in L2_TABLES.items() if definition.project_scoped
}
GENERAL_L2_CATEGORIES = frozenset(GENERAL_L2_TABLES)
PROJECT_L2_CATEGORIES = frozenset(PROJECT_L2_TABLES)


def validate_l2_category(category: str) -> str:
    if category not in L2_CATEGORIES:
        raise ValueError(f"unsupported L2 category: {category}")
    return category


def group_categories(categories: Iterable[str] | None) -> list[str]:
    """Return stable, deduplicated categories with no caller-controlled identifiers."""
    requested = list(L2_CATEGORIES if categories is None else categories)
    for category in requested:
        validate_l2_category(category)
    return sorted(set(requested))
