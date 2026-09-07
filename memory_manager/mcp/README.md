# MCP

`server.py` assembles Postgres, Qdrant, the shared embedding service, and the
reranker, then registers thin FastMCP wrappers around `MemoryTools`. Run the
installed `memory-mcp` entry point after applying the SQL schema and configuring
`.env`.

## Remote HTTP server

The default transport is Streamable HTTP. It listens on `0.0.0.0:8000` and
serves the MCP endpoint at `/mcp`:

```powershell
$env:MCP_TRANSPORT = "streamable-http"
$env:MCP_HOST = "0.0.0.0"
$env:MCP_PORT = "8000"
memory-mcp
```

Forward port 8000 with ngrok. Configure the agent with the full MCP endpoint,
including the path:

```text
https://footbath-handshake-devouring.ngrok-free.dev/mcp
```

The local protocol smoke test can target the same endpoint once the tunnel is
up:

```powershell
python scripts/smoke_mcp.py --url https://footbath-handshake-devouring.ngrok-free.dev/mcp
```

For clients that launch MCP processes instead of connecting by URL, set
`MCP_TRANSPORT=stdio` in the child environment. The stdio mode is retained for
compatibility, but it is not the deployment mode used by the forwarded agent.

Every tool publishes a description through MCP `tools/list`. The descriptions
explain scope behavior, retrieval modes, valid categories, UUID requirements,
the L2-first recall default, provenance drill-down, and the distinction between
explicit `remember` writes and the scheduled L0 -> L1 -> L2 conversation pipeline.

The example AgentOS dispatcher wraps the raw recall tool with a per-run budget of
three lookups: L2 only on attempts one and two, then L1 plus L2 on attempt three.
Further recall calls are stopped. When a user asks for supporting detail about a
returned record, the agent can call `drill_down` with that record's exact `id` and
`source_table` to retrieve its related L0 turns.

Project documentation is a separate, confirmed scope. Call `list_projects`
first and use Agno's native structured `ask_user` feedback to confirm the
selected UUID. It reserves one option for `Other options`, which opens a second
`ask_user` feedback question with `Recall memory` and `Cancel`. `Recall memory`
uses generic recall with the original request; `Cancel` stops the lookup. At most three projects
can be shown at once; provide an exact scope when more are registered. Project-aware
`recall`, `search_candidates`, `fetch`, and
`drill_down` calls must pass that UUID; project sections are never included in
unscoped reads. `resolve_project` remains available for explicit name lookup
workflows. The worker's `import-project` command accepts recursive folders
of UTF-8 `.md` or `.markdown` files and preserves nested relative paths for
provenance.

Read tools accept an optional exact `scope` filter and an explicit
`all_scopes=true` fallback. Agents should pass a scope only when it came from
trusted user or runtime context; otherwise they should use `all_scopes=true`
rather than guessing a namespace. The flag removes the scope filter for ranked
search, but it does not enumerate all records. `remember`, `update`, and
`forget` remain scope-bound write operations. If `DEFAULT_SCOPE` is omitted,
the runtime derives `user:<MEMORY_USER_ID>` (or the local OS account when the
user ID is omitted) for default writes.
