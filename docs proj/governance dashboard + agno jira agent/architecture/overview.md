# Architecture Overview

## What this actually is

A dashboard for reviewing and resolving **Jira-tracked approval requests**, plus an
embedded chat widget backed by a separate, narrowly-scoped Jira Q&A agent. It is not
the same thing as a HITL agent that itself pauses on dangerous SQL (compare with the
sibling `inventory-approval` repo, which *does* do that) — this repo's chat agent
(`backend/chatbot/jira_agent.py`) only reads and transitions Jira issues; it has no
Postgres/DDL tools at all. Approval rows in `ai.agno_approvals` are assumed to be
created by *something else* (an Agno agent with `requires_confirmation` tools, not
present in this repo/branch) — see [Approval Workflow](../approvals/workflow.md) for
what that implies.

## Services and real ports

| Service | Location | Port | Notes |
|---|---|---|---|
| Frontend | `frontend/` (React 18 + Vite 5) | **5174** | Set explicitly in `vite.config.js` — several places in the old docs/README say 5173, which is Vite's default, not this app's configured port |
| Backend | `backend/main.py` (FastAPI) | 8000 | Run with `uvicorn main:app --port 8000` |
| Agent (AgentOS) | `backend/chatbot/jira_agent.py` | 7780 | Run separately: `python jira_agent.py` |
| Postgres | external | 5432 | Shared by the backend and the agent's `PostgresDb` |

```
React (Vite, :5174)
  ├── /api/auth/login, /api/jira/approvals* ──► FastAPI backend (:8000) ──► Postgres
  └── chat widget ─────────────────────────────► AgentOS (:7780) directly
                                                     (NOT through the backend's proxy)
```

## The backend's `/agents` and `/sessions` proxy exists but the frontend doesn't use it

`backend/main.py` defines a generic reverse proxy:

```python
@app.api_route("/agents/{full_path:path}", methods=[...])
async def proxy_agent(full_path: str, request: Request):
    agent_url = f"http://127.0.0.1:7780/agents/{full_path}"
    ...
```

and an equivalent one for `/sessions/{full_path:path}`. This is real, working code — but
`frontend/src/components/ChatWidget.jsx` talks to `VITE_AGENT_OS_URL` (default
`http://localhost:7780`) **directly**, not through the backend at `:8000`. The comment
at the top of `ChatWidget.jsx` says this explicitly: it's a separate origin from the
dashboard backend, and it's "intentionally unauthenticated at the AgentOS level right
now." So as shipped, the backend's `/agents`/`/sessions` proxy is unused dead code from
the frontend's perspective — unless you deliberately point `VITE_AGENT_OS_URL` at the
backend's own origin.

## Two "approval" code paths exist; only one is wired into the UI

The backend exposes two independent sets of approval endpoints:

1. **Direct-DB path** (`main.py`): `GET /api/approvals`, `POST
   /api/approvals/{id}/resolve` — reads/writes `ai.agno_approvals` straight over
   `asyncpg`, requires a JWT (`Depends(get_current_admin_email)`).
2. **Jira-native path** (`backend/jira/jira_dashboard.py`): `GET /api/jira/approvals`,
   `GET /api/jira/approvals/{issue_key}`, `POST
   /api/jira/approvals/{issue_key}/resolve` — reads Jira issues directly via JQL and
   resolves by transitioning the Jira issue. **No auth dependency on these routes at
   all.**

`frontend/src/api.js`'s `listApprovals()` and `resolveApproval()` — the only two
functions `Dashboard.jsx` actually calls — hit the **Jira-native path**
(`/api/jira/approvals...`), not `/api/approvals`. So in the current build, the
direct-DB endpoints in `main.py` are reachable but unused by the UI, and the
Jira-native endpoints (which have no auth check of their own) are what the whole
dashboard runs on, protected only by the fact that a logged-in session is needed to
reach the Dashboard screen in the first place — not by anything on the API itself. See
[Approval Workflow](../approvals/workflow.md) for the full trace.

## Database

Everything lives in one Postgres instance/database, split across:
- `public.admins` — dashboard login users (created by `migrations.sql` or
  `create_admin.py`, both of which run the same `CREATE TABLE IF NOT EXISTS`)
- `ai.agno_approvals` — **owned by Agno itself** (created by whatever agent service
  first instantiates a `PostgresDb(approvals_table="agno_approvals")` — not by anything
  in this repo). The dashboard only ever reads/writes rows here, never touches its
  schema.
- `ai.tool_descriptions` — owned by this repo, populated by
  `helpers/sync_tool_descriptions.py`, LEFT JOINed onto `agno_approvals` by `tool_name`
  purely to show a human-readable blurb
- `ai.approvals` — created by `migrations.sql`, but **not queried anywhere in the
  application code** (`main.py` and `jira_webhook.py` both query `ai.agno_approvals`,
  never `ai.approvals`) — see [Known Gaps](../known-gaps.md)
- `public.jira_sync` — created by `001_jira_listener_setup.sql`, meant to correlate
  approval rows with Jira issue keys, but nothing in this repo writes to it (see below)

Full detail in [Database Schema](../database/schema.md).

## Auth

JWT via `Authorization: Bearer <token>`, not cookies. `helpers/auth.py` hardcodes the
algorithm (`HS256`) and expiry (`TOKEN_EXPIRE_HOURS = 12`) — these are not
environment-configurable despite the top-level README's `.env` example listing
`JWT_ALGORITHM` and `JWT_EXPIRE_MINUTES` variables that the code never reads. The
frontend stores the token in `localStorage` (`api.js`), attaches it as a Bearer header
on every request, and force-reloads to the login screen on any `401`.
