# Setup & Running Locally

This walks through what's actually required to get all three services running, based on
the code — not just the top-level README's claims (see
[08-known-gaps.md](./08-known-gaps.md) for where the two diverge).

## 0. Prerequisites

- Node.js 18+ (backend uses `node --watch`, needs Node 18.11+)
- Python 3.10+
- A reachable Postgres instance
- The `postgres-mcp` CLI on `PATH` (installed separately — it is **not** an npm/pip
  package pulled in by `requirements.txt`)
- An Nvidia API key (the agent uses `agno.models.nvidia.Nvidia`, model id
  `nvidia/nemotron-3-ultra-550b-a55b`)
- A Jira Cloud instance + API token, with 9 custom fields already created (see
  [06-hitl-approval-workflow.md](./06-hitl-approval-workflow.md))

There is no `.env.example` file anywhere in this repo (backend, agent-service, or
frontend), despite instructions elsewhere referencing one — you'll need to create these
`.env` files from scratch using the variable names below.

## 1. Database

```bash
psql "$DATABASE_URL" -f db/init.sql
```

This creates and seeds the `products` table only (12 demo rows). It does **not** create
the `run_approvals` table that `agent-service/agent.py` and `agent-service/hitl.py`
require at runtime — see [03-data-model.md](./03-data-model.md#run_approvals-not-in-initsql)
for the DDL you need to run manually before the approval flow will work.

## 2. Backend API (`backend/`)

```bash
cd backend
npm install
node src/index.js     # or: npm run dev (auto-restarts on changes)
```

Create `backend/.env` with:

| Var | Required | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | yes | — | Postgres connection string, read by `pg.Pool` in `src/db.js` |
| `PORT` | no | `4000` | |
| `CORS_ORIGIN` | no | `http://localhost:5173` | Comma-separated list allowed |

## 3. Agent service (`agent-service/`)

```bash
cd agent-service
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python agent.py        # http://localhost:7790
```

`requirements.txt` lists `agno`, `fastapi`, `uvicorn`, `python-dotenv`, `openai`,
`anthropic`, `mcp` — but `agent.py`/`hitl.py` also import `jira`, `psycopg2`, `certifi`,
`truststore`, and `httpx`, none of which are in `requirements.txt`. Install them
manually (`pip install jira psycopg2-binary certifi truststore httpx`) or the service
will fail on import.

Create `agent-service/.env` (all read via `os.environ[...]`, so these are **required**,
not optional, except where noted):

| Var | Required | Notes |
|---|---|---|
| `JIRA_BASE_URL` | yes | |
| `JIRA_EMAIL` | yes | |
| `JIRA_API_TOKEN` | yes | |
| `JIRA_PROJECT_KEY` | read but effectively unused | see gap notes — issue creation hardcodes `"SCRUM"` |
| `JIRA_ISSUE_TYPE` | no, default `"Task"` | also hardcoded to `"Task"` at issue-creation time regardless of this var — see gap notes |
| `JIRA_FIELD_APPROVAL_ID` | no, default `customfield_10044` | |
| `JIRA_FIELD_TOOL_NAME` | no, default `customfield_10046` | |
| `JIRA_FIELD_AGENT_ID` | no, default `customfield_10045` | |
| `JIRA_FIELD_APPROVAL_TYPE` | no, default `customfield_10112` | |
| `JIRA_FIELD_TOOL_CALL_ID` | no, default `customfield_10146` | |
| `JIRA_FIELD_TOOL_ARGS` | no, default `customfield_10148` | |
| `JIRA_FIELD_SESSION_ID` | no, default `customfield_10145` | |
| `JIRA_FIELD_RUN_ID` | no, default `customfield_10147` | |
| `JIRA_FIELD_REQUESTED_BY` | no, default `customfield_10181` | |

You must also either:
- create a Postgres database matching the hardcoded `DATABASE_URL`
  (`postgres:qaszdeszqa@localhost:5432/test_db`), or
- edit the `DATABASE_URL` literal in **both** `agent.py` and `hitl.py`.

## 4. Frontend (`frontend/`)

```bash
cd frontend
npm install
npm run dev             # http://localhost:5173
```

Create `frontend/.env` with (all optional — `frontend/src/api.js` has working
fallbacks for local dev):

| Var | Default |
|---|---|
| `VITE_API_URL` | `http://localhost:4000/api` |
| `VITE_AGENT_OS_URL` | `http://localhost:7790` |
| `VITE_AGENT_ID` | `postgres_agent` |

## Verifying it's wired up

1. `curl http://localhost:4000/api/health` → `{"status":"ok","db":"connected"}`
2. Open `http://localhost:5173` — you should see the 12 seeded products
3. Open the chat widget, ask "which products are low on stock?" — this runs entirely
   through the agent's own MCP/Postgres connection, independent of the Node API
4. Ask it to "drop the products table" to exercise the approval flow (see next page) —
   **note this will actually attempt a real DROP once approved**, so do this against a
   throwaway database
