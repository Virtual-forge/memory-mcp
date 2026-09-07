"""Build and run the HTTP or stdio MCP server."""

from typing import Any, Literal

from memory_manager.config import Settings
from memory_manager.mcp.tools import MemoryTools
from memory_manager.runtime import build_memory_tools

MCP_INSTRUCTIONS = """
Memory Manager provides durable long-term memory backed by Postgres and Qdrant. Use
remember only when the latest user message explicitly asks to save a memory. A normal
preference, fact, plan, or project description is not permission to call remember;
the automatic L0 -> L1 -> L2 pipeline handles those. Use recall to search
relevant memories before answering questions about prior context.
Use search_candidates when you need lightweight IDs before fetching or changing an
exact atom. Use drill_down when you need the exact L0 turns supporting an atom or L2
row. Recall defaults to every L2 category; request sources=['atoms'] explicitly when
you need to escalate to raw L1 atoms. Scope is an optional read filter: use an exact
scope only when it is known, and use all_scopes=true when it is not. Do not invent
scope names. An omitted scope uses the configured default scope, except project identity
listing and lookup, which search all registered project scopes unless an explicit scope is
supplied. remember creates an
asserted L1 atom directly
and does not run the conversation
L0 -> L1 -> L2 extraction pipeline. update and forget operate on exact atom UUIDs
returned by this server; forget hides the atom from retrieval but retains its audit row.
When a request names or implies a specific project, use list_projects before recall or
any other project-specific documentation operation. The next action must be a
UserFeedbackTools.ask_user call with the listed projects as structured options; do not ask
for a project name in ordinary prose. Use the selected project's exact UUID and do not
invent one. If more than three projects are registered, narrow the list with an exact
scope; never silently drop projects or the Other options option. If the user selects
Other options, use a second structured ask_user question with Recall memory and Cancel
options. Recall memory uses the original request with generic recall; Cancel stops. If no
projects are registered, report that instead of guessing.
""".strip()


def register_tools(mcp: Any, tools: MemoryTools) -> None:
    """Register documented transport functions around the memory service."""

    @mcp.tool(
        description=(
            "Store an explicitly requested memory as an asserted L1 atom. Call this "
            "only when the latest user message directly says to remember, save, store, "
            "or note something for later. Do not call it for ordinary preferences, "
            "facts, plans, or project context; those belong to the automatic L0-to-L2 "
            "pipeline. It indexes immediately and does not run extraction or synthesis. "
            "Scope defaults to the configured user scope; category must be fact, "
            "preference, decision, or event."
        )
    )
    def remember(
        statement: str,
        scope: str | None = None,
        category: Literal["fact", "preference", "decision", "event"] = "fact",
        entity: str | None = None,
        predicate: str | None = None,
    ) -> dict[str, object]:
        return tools.remember(statement, scope, category, entity, predicate)

    @mcp.tool(
        description=(
            "Search durable memories for a natural-language query and return hydrated "
            "results from Postgres. Use mode vector, bm25, or hybrid; hybrid combines "
            "dense and sparse retrieval. Filter with exact source table names or with "
            "types such as agent, workspace, or user. Omitted filters search every L2 "
            "category; atoms require an explicit sources=['atoms'] escalation. Results "
            "stay inside the requested scope, which defaults to the configured default "
            "scope. Set all_scopes=true when no reliable scope is known; do not combine "
            "all_scopes=true with scope. If the query names a project, list and confirm "
            "the project first with UserFeedbackTools.ask_user; project documentation "
            "requires the confirmed project_id."
        )
    )
    def recall(
        query: str,
        scope: str | None = None,
        mode: Literal["vector", "bm25", "hybrid"] | None = None,
        types: list[str] | None = None,
        sources: list[str] | None = None,
        top_k: int = 8,
        min_score: float | None = None,
        all_scopes: bool = False,
        project_id: str | None = None,
    ) -> list[dict[str, object]]:
        return tools.recall(
            query,
            scope,
            mode,
            types,
            sources,
            top_k,
            min_score,
            all_scopes,
            project_id,
        )

    @mcp.tool(
        description=(
            "List all registered project identities in deterministic name order. Use this "
            "before answering a project-specific question so UserFeedbackTools can present "
            "the project names as structured choices. With no scope, this searches all "
            "project scopes; pass an exact scope only when the user supplied one. The "
            "returned UUID is authoritative for the later project_recall operation. The "
            "structured selector reserves one option for Other options, so at most three "
            "projects are shown; larger lists fail clearly so the caller can narrow with "
            "an exact scope. After Other options is selected, the caller can choose "
            "Recall memory or Cancel."
        )
    )
    def list_projects(scope: str | None = None) -> list[dict[str, object]]:
        return tools.list_projects(scope)

    @mcp.tool(
        description=(
            "Resolve a project name to deterministic candidate identities. Use this "
            "before project-documentation import or retrieval. It performs no LLM or "
            "embedding work and returns at most three candidates for explicit name lookup. "
            "For agent project selection, use list_projects instead so every registered "
            "project is available to UserFeedbackTools."
        )
    )
    def resolve_project(
        name: str,
        scope: str | None = None,
        all_scopes: bool = True,
    ) -> list[dict[str, object]]:
        return tools.resolve_project(name, scope, all_scopes)

    @mcp.tool(
        description=(
            "Search for lightweight memory candidates without hydrating full records. "
            "Use this before fetch, update, or forget when you need IDs and source "
            "tables. Supports vector, bm25, and hybrid modes plus the same scope, type, "
            "and source filters as recall. Set all_scopes=true when no reliable scope is "
            "known; do not invent a scope name. Project searches require the confirmed "
            "project_id returned by list_projects."
        )
    )
    def search_candidates(
        query: str,
        scope: str | None = None,
        mode: Literal["vector", "bm25", "hybrid"] | None = None,
        types: list[str] | None = None,
        sources: list[str] | None = None,
        top_k: int = 20,
        all_scopes: bool = False,
        project_id: str | None = None,
    ) -> list[dict[str, object]]:
        return tools.search_candidates(
            query,
            scope,
            mode,
            types,
            sources,
            top_k,
            all_scopes,
            project_id,
        )

    @mcp.tool(
        description=(
            "Fetch complete memory records for exact UUIDs returned by recall, "
            "search_candidates, or remember. Pass the scope when known to prevent "
            "cross-scope hydration, or set all_scopes=true when the scope is unknown."
        )
    )
    def fetch(
        ids: list[str],
        scope: str | None = None,
        all_scopes: bool = False,
        project_id: str | None = None,
    ) -> list[dict[str, object]]:
        return tools.fetch(ids, scope, all_scopes, project_id)

    @mcp.tool(
        description=(
            "Return the exact L0 conversation turns supporting an atom or an L2 row. "
            "Use source_table='atoms' for a direct atom lookup, or pass an L2 category "
            "such as projects, tools, skills, profiles, or users to follow its junction "
            "table to the source atoms and then to their turns. Project sections require "
            "the confirmed project_id returned by list_projects."
        )
    )
    def drill_down(
        id: str,
        source_table: str,
        scope: str | None = None,
        all_scopes: bool = False,
        project_id: str | None = None,
    ) -> list[dict[str, object]]:
        return tools.drill_down(id, source_table, scope, all_scopes, project_id)

    @mcp.tool(
        description=(
            "Correct one existing asserted L1 atom by UUID while preserving its ID and "
            "provenance links. Only supplied fields change. Use an exact atom_id and "
            "the correct scope; category must be fact, preference, decision, or event."
        )
    )
    def update(
        atom_id: str,
        statement: str | None = None,
        category: Literal["fact", "preference", "decision", "event"] | None = None,
        entity: str | None = None,
        predicate: str | None = None,
        scope: str | None = None,
    ) -> dict[str, object]:
        return tools.update(atom_id, statement, category, entity, predicate, scope)

    @mcp.tool(
        description=(
            "Hide an existing memory from retrieval and remove its Qdrant point while "
            "retaining the Postgres audit row. Use only with an exact atom UUID and "
            "the correct scope. This is a soft delete, not physical data erasure."
        )
    )
    def forget(atom_id: str, scope: str | None = None) -> bool:
        return tools.forget(atom_id, scope)


def main() -> None:
    from mcp.server.fastmcp import FastMCP

    settings = Settings()
    mcp = FastMCP(
        "memory-manager",
        instructions=MCP_INSTRUCTIONS,
        host=settings.mcp_host,
        port=settings.mcp_port,
        streamable_http_path="/mcp",
        stateless_http=True,
    )
    register_tools(mcp, build_memory_tools(settings))
    mcp.run(transport=settings.mcp_transport)


if __name__ == "__main__":
    main()
