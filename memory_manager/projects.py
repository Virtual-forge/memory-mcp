"""Project identity helpers used before project-specific memory operations."""

from collections.abc import Iterable
from typing import Any

from memory_manager.db.connection import Database


def normalize_project_name(value: str) -> str:
    """Normalize whitespace and case for stable project-name comparisons."""
    normalized = " ".join(value.split()).casefold()
    if not normalized:
        raise ValueError("project name must not be empty")
    return normalized


def _project_terms(value: str) -> list[str]:
    return normalize_project_name(value).replace("-", " ").replace("_", " ").split()


def _project_terms_match(query: str, candidate: str) -> bool:
    query_terms = _project_terms(query)
    candidate_terms = _project_terms(candidate)
    compact_candidate = "".join(candidate_terms)
    return all(
        term in candidate_terms or (len(term) >= 4 and term in compact_candidate)
        for term in query_terms
    )


def rank_project_candidates(
    name: str,
    projects: Iterable[dict[str, Any]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return exact, alias, and partial project matches in deterministic order."""
    if limit <= 0:
        raise ValueError("project candidate limit must be positive")
    query = normalize_project_name(name)
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for project in projects:
        project_name = str(project.get("name", ""))
        if not project_name.strip():
            continue
        normalized_name = normalize_project_name(project_name)
        aliases = [
            normalize_project_name(str(alias))
            for alias in project.get("aliases", [])
            if str(alias).strip()
        ]
        if query == normalized_name:
            rank = 0
        elif query in aliases:
            rank = 1
        elif query in normalized_name or any(query in alias for alias in aliases):
            rank = 2
        elif _project_terms_match(query, project_name) or any(
            _project_terms_match(query, alias) for alias in aliases
        ):
            rank = 3
        else:
            continue
        ranked.append((rank, normalized_name, project))
    ranked.sort(key=lambda item: (item[0], item[1], str(item[2].get("id", ""))))
    return [project for _, _, project in ranked[:limit]]


def find_project_candidates(
    database: Database,
    name: str,
    scope: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Find project identities without invoking embeddings or an LLM."""
    query = """
        select id, scope, name, aliases, root_path
        from project_registry
    """
    params: list[object] = []
    if scope is not None:
        query += " where scope = %s"
        params.append(scope)
    query += " order by normalized_name"
    return rank_project_candidates(name, database.fetch_all(query, params), limit=limit)


def list_registered_projects(
    database: Database,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    """List registered project identities in deterministic order."""
    query = """
        select id, scope, name, aliases, root_path
        from project_registry
    """
    params: list[object] = []
    if scope is not None:
        query += " where scope = %s"
        params.append(scope)
    query += " order by normalized_name, id"
    return database.fetch_all(query, params)


def register_project(
    database: Database,
    name: str,
    scope: str,
    root_path: str | None = None,
    aliases: Iterable[str] = (),
) -> dict[str, Any]:
    """Create or update a project identity and return its stable record."""
    normalized_name = normalize_project_name(name)
    normalized_aliases = sorted(
        {
            normalize_project_name(alias)
            for alias in aliases
            if alias.strip() and normalize_project_name(alias) != normalized_name
        }
    )
    with database.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                insert into project_registry (
                    scope, name, normalized_name, aliases, root_path
                ) values (%s, %s, %s, %s, %s)
                on conflict (scope, normalized_name) do update set
                    name = excluded.name,
                    aliases = excluded.aliases,
                    root_path = coalesce(excluded.root_path, project_registry.root_path),
                    updated_at = now()
                returning id, scope, name, aliases, root_path
                """,
                (scope, name.strip(), normalized_name, normalized_aliases, root_path),
            )
            return cursor.fetchone()