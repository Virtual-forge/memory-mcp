# Agent (`backend/chatbot/jira_agent.py`)

## What it is — and isn't

This is a **Jira Q&A/transition assistant**, not a database-operations agent with an
approval gate. It has no Postgres MCP tools, no `run_admin_sql`-style tool, and no
`requires_confirmation`/HITL middleware of any kind in this file. If you're expecting
something like the sibling `inventory-approval` repo's DDL-approval agent, this isn't
it — this agent's only capability is talking to Jira/Confluence via MCP.

```python
db = PostgresDb(
    db_url=DATABASE_URL,
    session_table="agno_sessions",
    approvals_table="agno_approvals",
)

jira_mcp = MCPTools(
    command="uvx mcp-atlassian",
    env={"JIRA_URL": ..., "JIRA_USERNAME": ..., "JIRA_API_TOKEN": ...},
)

shared_model = Nvidia(id="nvidia/nemotron-3-ultra-550b-a55b", api_key=NVIDIA_API_KEY)

jira_agent = Agent(
    name="approval-demo",
    model=shared_model,
    tools=[jira_mcp],
    db=db,
    debug_mode=True,
    instructions=[...],
    markdown=True,
)

agent_os = AgentOS(
    id="jira-approval-agent",
    agents=[jira_agent],
    cors_allowed_origins=["http://localhost:5174", "http://localhost:8000"],
    db=db,
)
app = agent_os.get_app()
```

Note `db=db` is passed to **both** `PostgresDb(...)` (constructing the connection +
table names) and reused as the `AgentOS`'s own `db=` — the same object handles session
storage and whatever approvals bookkeeping Agno itself does internally.

## Why `approvals_table="agno_approvals"` matters even though this agent has no gated tools

Because this `PostgresDb` is configured with `approvals_table="agno_approvals"`, if
Agno hasn't already created that table (e.g. via another agent process), instantiating
this `PostgresDb` is plausibly what creates `ai.agno_approvals` in the first place —
though nothing in this repo has a tool that would ever cause a row to actually be
inserted into it, since none of `jira_agent`'s tools declare
`requires_confirmation=True`. In other words: this file can plausibly be the reason the
table *exists*, without ever being the reason a row appears in it. Wherever the
approval rows the dashboard displays actually come from, it's not this agent.

## Model and MCP

- Model: `agno.models.nvidia.Nvidia`, id `nvidia/nemotron-3-ultra-550b-a55b`, same as
  the sibling repo, with `NVIDIA_API_KEY` read from env (no hardcoded fallback for the
  key itself, unlike the URL/email defaults below)
- Tools: exactly one — `mcp-atlassian`, invoked via `uvx mcp-atlassian` (requires `uv`
  and network access to fetch/run it — not a pip dependency)
- `JIRA_BASE_URL` and `JIRA_EMAIL` **do** have hardcoded fallback defaults in this file
  (`https://lakehaylihamza.atlassian.net/` and an associated Gmail address) if the
  corresponding env vars are unset — worth replacing with something environment-driven
  or at least project-owned before this goes anywhere beyond one person's local setup
- `JIRA_API_TOKEN` has no fallback — `MCPTools` will get `None` if it's unset, and the
  MCP server will presumably fail its own auth at that point

## Instructions given to the model

Paraphrased from the `instructions` list in the file:
- Act as a Jira assistant that can view tickets and change their status
- Always use actual tool/function calls for Jira operations — never narrate a tool call
  or print a JSON payload as if it were the action itself
- Always reference issues by their exact key (e.g. `APR-123`, `SCRUM-5`)
- Keep responses short and free of markdown tables/headers, since they render in a
  ~350px-wide chat panel

## Running it

```bash
cd backend/chatbot
python jira_agent.py   # agent_os.serve(app="jira_agent:app", port=7780)
```

This is a **separate long-running process** from the FastAPI backend (`main.py`) — the
top-level README's three-terminal setup (backend, agent, frontend) is accurate here,
even though other parts of the README have drifted from the code. There is no
`requirements.txt` scoped to just `backend/chatbot/` — its dependencies (`agno`,
`fastapi`, the Nvidia provider, `mcp`, `certifi`, `truststore`) aren't listed anywhere
in this repo's `backend/requirements.txt`, which only covers the plain FastAPI backend's
needs (`fastapi`, `uvicorn`, `asyncpg`, `pydantic[email]`, `PyJWT`, `bcrypt`,
`python-dotenv`, `httpx`). You'll need to `pip install` the agent's dependencies
separately.
