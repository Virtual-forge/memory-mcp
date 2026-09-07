"""Small Postgres connection wrapper used by pipeline services."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Database:
    """Open short-lived psycopg connections and explicit transactions."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def connect(self) -> Any:
        """Create a row-producing connection without importing psycopg at module load."""
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.dsn, row_factory=dict_row)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Yield a connection whose changes commit or roll back as one unit."""
        connection = self.connect()
        try:
            with connection.transaction():
                yield connection
        finally:
            connection.close()

    def fetch_all(self, query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return list(cursor.fetchall())

    def fetch_one(self, query: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchone()

    def execute(self, query: str, params: Sequence[Any] = ()) -> None:
        with self.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, params)

    def apply_schema(self, schema_path: str | Path) -> None:
        """Apply the source-of-truth schema in one transaction."""
        schema = Path(schema_path).read_text(encoding="utf-8")
        with self.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(schema)
