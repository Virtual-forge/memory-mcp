# Architecture

## Services

| Service | Location | Stack | Port | Role |
|---|---|---|---|---|
| Frontend | `frontend/` | React 18 + Vite 5 | `5173` | Product catalog UI + chat widget |
| Backend API | `backend/` | Node/Express 4 + `pg` | `4000` | REST CRUD over `products` (only `GET` routes are actually used by the frontend — see below) |
| Agent service | `agent-service/` | Python, Agno `AgentOS`, FastAPI under the hood | `7790` | Runs the chat agent; connects to Postgres directly via MCP, not through the backend API |

## Data flow

```
React (Vite, :5173)
  ├── GET /api/products, GET /api/products/:id ──► Node/Express (:4000) ──► Postgres
  └── POST /agents/postgres_agent/runs (chat) ───► AgentOS (:7790)
                                                       └── Agno Agent
                                                             ├── postgres-mcp (execute_sql) ──► Postgres "products" table
                                                             └── run_admin_sql (DDL, gated)  ──► Postgres (after Jira approval)
```

Two independent paths can mutate the same `products` table:

- The **Node API** (`backend/src/routes/products.js`) implements full CRUD (`GET`, `POST`,
  `PUT`, `DELETE`), but in the current frontend code (`frontend/src/api.js`) only
  `listProducts()` and `getProduct(id)` are ever called. There is no create/edit/delete
  form in the UI — `POST`/`PUT`/`DELETE` exist on the backend but are effectively dead
  endpoints from the frontend's perspective today.
- The **agent's MCP tool** (`postgres-mcp`, wired up in `agent-service/agent.py`) talks to
  Postgres directly with its own connection — it does not go through the Node API at all.
  This is the actual path used when you ask the chat widget to "restock the Cirrus
  headphones to 50" — it becomes a raw SQL `UPDATE` executed by the agent via MCP.

After the agent finishes a run, `ChatWidget.jsx`'s `onDataChanged` callback triggers
`App.jsx`'s `refreshAll()`, which re-fetches the product list and the selected product
detail from the **Node API** — so the dashboard reflects whatever the agent changed via
MCP, even though the two paths never talk to each other directly.

## Why the agent needs its own DDL gate

`postgres-mcp`'s `execute_sql` tool will run *any* SQL it's given, including
`DROP TABLE`/`ALTER TABLE` — MCP tools don't support Agno's per-tool
`requires_confirmation` flag. So the repo adds two layers around it (see
[06-hitl-approval-workflow.md](./06-hitl-approval-workflow.md) for the full flow):

1. A `tool_hooks` function (`block_unapproved_ddl_hook` in `hitl.py`) inspects every tool
   call before it runs and hard-blocks `execute_sql` calls whose SQL matches
   `DROP TABLE|ALTER TABLE`, telling the agent to use `run_admin_sql` instead.
2. `run_admin_sql` is a separate, custom tool decorated with
   `@tool(requires_confirmation=True)`, which *does* support Agno's pause/resume
   mechanism — so calling it pauses the run until a human approves it via Jira.

## Database URL is hardcoded, twice

Both `agent-service/agent.py` and `agent-service/hitl.py` independently define:

```python
DATABASE_URL = "postgresql://postgres:qaszdeszqa@localhost:5432/test_db"
```

This is not read from `.env` or any environment variable in either file — despite the
top-level README instructing you to set `DATABASE_URL` in `backend/.env`. That value only
configures the Node backend's connection; the Python agent service's database
connection is separate and must be edited directly in both `agent.py` and `hitl.py` if
you point it at a different database.
