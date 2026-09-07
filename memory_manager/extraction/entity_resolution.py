"""Match entity names and aliases before creating registry rows."""

from typing import Any
from uuid import UUID


def normalize_entity_name(name: str) -> str:
    return " ".join(name.strip().split())


def resolve_entity(connection: Any, scope: str, name: str | None) -> UUID | None:
    """Resolve an entity in scope, creating it only when no alias matches."""
    if name is None or not name.strip():
        return None

    normalized_name = normalize_entity_name(name)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select id
            from entities
            where scope = %s
              and (
                  lower(name) = lower(%s)
                  or exists (
                      select 1
                      from unnest(aliases) as alias
                      where lower(alias) = lower(%s)
                  )
              )
            order by created_at
            limit 1
            """,
            (scope, normalized_name, normalized_name),
        )
        existing = cursor.fetchone()
        if existing:
            return existing["id"]

        cursor.execute(
            """
            insert into entities (scope, name)
            values (%s, %s)
            on conflict (scope, name) do update set name = excluded.name
            returning id
            """,
            (scope, normalized_name),
        )
        created = cursor.fetchone()
    return created["id"]


def load_known_entities(connection: Any, scope: str) -> list[dict[str, object]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            select name, aliases
            from entities
            where scope = %s
            order by name
            """,
            (scope,),
        )
        return list(cursor.fetchall())
