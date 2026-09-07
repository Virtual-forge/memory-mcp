# Agent Service (Agno AgentOS, port 7790)

Source: `agent-service/agent.py`, `agent-service/hitl.py`.

## The agent

Defined in `agent.py`:

```python
shared_model = Nvidia(id="nvidia/nemotron-3-ultra-550b-a55b")
postgres_mcp_tools = MCPTools(
    command="postgres-mcp --access-mode=unrestricted",
    env={"DATABASE_URI": DATABASE_URL},
)

postgres_agent = Agent(
    id="postgres_agent",
    name="DBA Agent",
    model=shared_model,
    tools=[postgres_mcp_tools, run_admin_sql],
    tool_hooks=[block_unapproved_ddl_hook],
    instructions=[...],
    db=db,                 # Agno PostgresDb, same DATABASE_URL — used for session persistence
    debug_mode=True,
    cache_session=True,
)
```

- **Model**: Nvidia Nemotron 3 Ultra, via Agno's `agno.models.nvidia.Nvidia` provider —
  not OpenAI or Anthropic, despite both SDKs being in `requirements.txt`.
- **Tools**: the full `postgres-mcp` toolset (whatever `execute_sql` and friends the MCP
  server exposes) plus one custom tool, `run_admin_sql`.
- **Tool hook**: `block_unapproved_ddl_hook` runs before *every* tool call the agent
  makes (not just SQL ones — it inspects `function_name` on each call).

### System instructions given to the model

From `agent.py`, verbatim intent (not exact wording, since this is the actual prompt
text baked into the repo):
- It's told to act as a senior DBA with Postgres tools
- Reply in the same language the user wrote in
- SELECT/INSERT/UPDATE/DELETE need no approval — call `execute_sql` directly
- Only `DROP TABLE` / `ALTER TABLE` require approval, and must go through
  `run_admin_sql`, never `execute_sql`
- Don't narrate tool mechanics to the user, just execute and answer
- Never claim an action succeeded unless a tool call actually returned success
- When a tool result comes back after approval, report the outcome clearly

This is **instructional**, not enforced by any type system — the actual enforcement is
the `block_unapproved_ddl_hook` described below, which will reject a raw `execute_sql`
DDL attempt regardless of what the model intends.

## `block_unapproved_ddl_hook` (`hitl.py`)

```python
DANGEROUS_SQL_RE = re.compile(r"\b(DROP\s+TABLE|ALTER\s+TABLE)\b", re.IGNORECASE)

async def block_unapproved_ddl_hook(function_name, function_call, arguments):
    if function_name == "execute_sql" and DANGEROUS_SQL_RE.search(_extract_sql(arguments)):
        return "BLOCKED: schema-altering statements must go through the `run_admin_sql` tool..."
    if inspect.iscoroutinefunction(function_call):
        return await function_call(**arguments)
    return function_call(**arguments)
```

This only pattern-matches on the literal tool name `execute_sql` (the name
`postgres-mcp` exposes for running arbitrary SQL) and the regex
`DROP TABLE|ALTER TABLE`. It does **not** catch `TRUNCATE`, `CREATE TABLE`, `RENAME`, or
any other DDL statement, despite the top-level README describing the gate as covering
"DDL statements like `DROP TABLE`, `ALTER TABLE`, `TRUNCATE`, etc." — see
[08-known-gaps.md](./08-known-gaps.md).

If the SQL is blocked, the function returns a plain string telling the model to call
`run_admin_sql` instead — it does not raise an exception or otherwise stop the agent
loop.

## `run_admin_sql` (`hitl.py`)

```python
@jira_approval   # = @tool(requires_confirmation=True), tags _jira_approval = True
def run_admin_sql(sql: str, run_context: RunContext) -> str:
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    return f"Executed: {sql}"
```

Because this tool is registered with `requires_confirmation=True`, Agno's own run
engine pauses the run the moment the model tries to call it — this is what produces
the `PAUSED` status that the middleware (below) reacts to. It executes arbitrary SQL
(not just DROP/ALTER) via a direct `psycopg2` connection — the approval gate is on
*calling this tool at all*, not on validating what SQL is inside it.

## Endpoints AgentOS adds on top of Agno's built-ins

`agent_os.get_app()` returns AgentOS's standard FastAPI app (which already exposes
`POST /agents/{agent_id}/runs`, `.../continue`, `.../cancel`, etc.). `agent.py` then:

1. Adds `JiraHitlMiddleware` (see next page) via `app.add_middleware(...)`
2. Defines three custom routes:

| Route | Purpose |
|---|---|
| `GET /api/approvals/{run_id}` | Looks up the Jira ticket for a run, returns its raw Jira status name plus a simplified `pending`/`approved`/`blocked` |
| `POST /api/approvals/{run_id}/continue` | Only proceeds if simplified status is `approved`; resolves the paused run with `confirmed=True` |
| `POST /api/approvals/{run_id}/reject` | Only proceeds if simplified status is `blocked`; cancels the run |

These three are what the frontend actually talks to (`api.getApprovalStatus`,
`api.continueRun`, `api.rejectRun` in `frontend/src/api.js`) — the frontend never calls
AgentOS's native `/continue`/`/cancel` endpoints directly.

See [06-hitl-approval-workflow.md](./06-hitl-approval-workflow.md) for the full
end-to-end trace.
