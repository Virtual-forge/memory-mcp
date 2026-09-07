"""Run an isolated production-shaped L0 -> L1 -> L2 demo pipeline."""

from argparse import ArgumentParser, Namespace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from memory_manager.config import Settings
from memory_manager.models.turn import TurnInput
from memory_manager.runtime import build_memory_pipeline

DEMO_TURNS = (
    (
        "user",
        (
            "I am building Memory Manager in this repository. It is a structured "
            "long-term memory project with Postgres as the source of truth, Qdrant "
            "for retrieval, and an MCP server for memory operations."
        ),
        None,
        None,
    ),
    (
        "user",
        (
            "The production architecture decision is to keep raw conversation turns "
            "in Postgres, extract precise L1 atoms with provenance, synthesize "
            "category-specific L2 rows, and use one shared Qdrant collection for "
            "dense, sparse, and native hybrid retrieval."
        ),
        None,
        None,
    ),
    (
        "user",
        (
            "The memory-mcp server is an important agent integration. It exposes "
            "remember, recall, search_candidates, fetch, update, and forget over "
            "stdio so an agent can use the memory system as a tool."
        ),
        None,
        None,
    ),
    (
        "assistant",
        (
            "Extraction and synthesis are separate scheduled jobs. Ingestion must "
            "write L0 synchronously without waiting for an LLM, while the extraction "
            "job produces L1 atoms and the synthesis job produces L2 rows."
        ),
        None,
        None,
    ),
    (
        "user",
        (
            "I prefer implementation work to stay structured, concise, and explicit. "
            "I do not want messy generated code or broad unrelated refactors."
        ),
        None,
        None,
    ),
    (
        "user",
        (
            "The maintainer is working on Windows and wants the memory system to be "
            "usable from VS Code with a repeatable local worker workflow."
        ),
        None,
        None,
    ),
)


def parse_args() -> Namespace:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope",
        default=f"demo-l0-l1-l2-{datetime.now(UTC):%Y%m%d-%H%M%S}",
        help="isolated memory scope to populate",
    )
    parser.add_argument(
        "--session-id",
        type=UUID,
        default=None,
        help="optional stable session UUID",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = Settings()
    session_id = args.session_id or uuid4()
    pipeline = build_memory_pipeline(settings)
    ingestion_time = datetime.now(UTC)

    turn_ids = []
    for index, (source, content, tool_name, tool_call_id) in enumerate(DEMO_TURNS):
        turn = TurnInput(
            session_id=session_id,
            scope=args.scope,
            source=source,
            content=content,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
        result = pipeline.ingest_turn(turn, now=ingestion_time + timedelta(seconds=index))
        turn_ids.append(result.turn_id)

    extraction = pipeline.run_extraction_if_due(
        session_id=session_id,
        scope=args.scope,
        now=ingestion_time + timedelta(minutes=settings.extraction_inactivity_minutes + 1),
    )
    if not extraction.ran:
        raise RuntimeError("the demo L0 batch did not trigger extraction")

    synthesis = pipeline.run_synthesis_job(args.scope)
    if not synthesis.ran:
        raise RuntimeError("the demo L1 atoms did not trigger synthesis")

    counts = load_counts(settings.database_url, args.scope)
    print("pipeline: L0 -> L1 -> L2 completed")
    print("scope:", args.scope)
    print("session_id:", session_id)
    print("L0 turns:", len(turn_ids))
    print("L1 atoms:", len(extraction.indexed_atom_ids))
    print("L2 atoms processed:", len(synthesis.atom_ids))
    print("L2 rows by category:")
    for category, count in counts.items():
        print(f"  {category}: {count}")


def load_counts(database_url: str, scope: str) -> dict[str, int]:
    import psycopg

    categories = {
        "projects": "projects",
        "tools": "tools",
        "skills": "skills",
        "mcp": "mcp",
        "workflows": "workflows",
        "profiles": "profiles",
        "users": "users",
        "docs": "docs",
        "tasks": "tasks",
    }
    counts: dict[str, int] = {}
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            for category, table in categories.items():
                cursor.execute(f"select count(*) from {table} where scope = %s", (scope,))
                counts[category] = int(cursor.fetchone()[0])
    return counts


if __name__ == "__main__":
    main()
