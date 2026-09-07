"""Move all records in one memory scope to another namespace."""

from __future__ import annotations

import argparse

from memory_manager.config import Settings
from memory_manager.db.connection import Database
from memory_manager.runtime import build_qdrant_client, configure_system_tls
from memory_manager.synthesis.routing import L2_TABLES

SCOPED_TABLES = (
    "entities",
    "turns",
    "extraction_attempts",
    "atoms",
    *sorted(L2_TABLES),
)
INDEXED_TABLES = ("atoms", *sorted(L2_TABLES))


def parse_args() -> argparse.Namespace:
    settings = Settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-scope", required=True)
    parser.add_argument("--to-scope", default=settings.resolved_default_scope)
    parser.add_argument(
        "--skip-qdrant",
        action="store_true",
        help="Update Postgres only; use this when the vector service is unavailable.",
    )
    return parser.parse_args()


def load_indexed_ids(database: Database, source_scope: str) -> list[str]:
    ids: list[str] = []
    for table in INDEXED_TABLES:
        rows = database.fetch_all(
            f"select id from {table} where scope = %s",
            (source_scope,),
        )
        ids.extend(str(row["id"]) for row in rows)
    return ids


def update_qdrant_scope(settings: Settings, ids: list[str], target_scope: str) -> None:
    if not ids:
        return
    from qdrant_client.models import PointIdsList

    configure_system_tls()
    client = build_qdrant_client(settings)
    client.set_payload(
        collection_name=settings.qdrant_collection,
        payload={"scope": target_scope},
        points=PointIdsList(points=ids),
    )


def migrate_database(database: Database, source_scope: str, target_scope: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    with database.transaction() as connection:
        with connection.cursor() as cursor:
            for table in SCOPED_TABLES:
                cursor.execute(
                    f"update {table} set scope = %s where scope = %s",
                    (target_scope, source_scope),
                )
                counts[table] = cursor.rowcount
    return counts


def main() -> None:
    args = parse_args()
    source_scope = args.from_scope.strip()
    target_scope = args.to_scope.strip()
    if not source_scope or not target_scope:
        raise SystemExit("source and target scopes must not be empty")
    if source_scope == target_scope:
        raise SystemExit("source and target scopes must differ")

    settings = Settings()
    database = Database(settings.database_url)
    indexed_ids = load_indexed_ids(database, source_scope)
    if not args.skip_qdrant:
        update_qdrant_scope(settings, indexed_ids, target_scope)
    counts = migrate_database(database, source_scope, target_scope)
    print(f"migrated {source_scope} -> {target_scope}")
    print({table: count for table, count in counts.items() if count})
    print(f"qdrant_points_retagged={0 if args.skip_qdrant else len(indexed_ids)}")


if __name__ == "__main__":
    main()