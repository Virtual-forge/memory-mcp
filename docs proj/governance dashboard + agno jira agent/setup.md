# Local Setup

Three processes, plus Postgres and a Jira Cloud instance. No `.env.example` files exist
in this repo (backend or frontend) — the variable names below come from what the code
actually reads via `os.environ`/`os.getenv`, not from any checked-in example.

## 0. Prerequisites

- Python 3.10+ (backend and agent)
- Node.js 18+
- Postgres reachable from wherever you run the backend/agent
- `uv`/`uvx` on `PATH` — the agent runs Jira/Confluence tools via `uvx mcp-atlassian`,
  not a pip package
- A Jira Cloud site + API token, with the same 9 custom fields as the sibling
  `inventory-approval` repo (this dashboard's default field IDs match that repo's
  defaults exactly — see [API Routes](./backend/api-routes.md) / `jira_client.py`)
- An Nvidia API key for the agent's Nemotron model

## 1. Database

```bash
psql "$DATABASE_URL" -f backend/database/migrations.sql
cd backend/database && python run_migration.py   # runs 001_jira_listener_setup.sql only
```

Note `run_migration.py` does **not** run `migrations.sql` — you have to do that
manually first, or the `admins`/`ai.tool_descriptions` tables won't exist. Also note
`ai.agno_approvals` isn't created by either of these — see
[Database Schema](./database/schema.md) for why, and don't expect the dashboard to show
anything until that table exists and has rows in it from elsewhere.

## 2. Backend (`backend/`)

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

`backend/.env`:

| Var | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | read directly via `os.environ["DATABASE_URL"]` — will crash on import if unset |
| `JWT_SECRET` | yes | `helpers/auth.py` raises `RuntimeError` at import time if empty |
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECT_KEY` | yes | read by `jira_client.py` via `os.environ[...]` (no `.get()`), so all four are hard requirements to even import the module |
| `JIRA_ISSUE_TYPE` | no, default `Task` | |
| `JIRA_PENDING_STATUS_NAME` | no, default `Pending` | must match your actual workflow's initial status name |
| `JIRA_WEBHOOK_SECRET` | yes (for `/webhooks/jira-approval` to work) | `jira_webhook.py` also reads this via `os.environ[...]` with no fallback |
| `JIRA_FIELD_APPROVAL_ID` ... `JIRA_FIELD_REQUESTED_BY` (9 fields) | no, have defaults | same default `customfield_100xx` IDs as the sibling `inventory-approval` repo — override if your Jira instance's fields differ |

`JWT_ALGORITHM` and `JWT_EXPIRE_MINUTES`, which the top-level README's `.env` example
lists, are **not read anywhere in the code** — algorithm and expiry are hardcoded in
`helpers/auth.py` (`HS256`, 12 hours). Setting them does nothing.

## 3. Create an admin login

```bash
cd backend
python helpers/create_admin.py you@example.com   # prompts for a password
```

(Not `python -m helpers.create_admin` as one part of the README suggests — the script's
own docstring says direct invocation; either may or may not work depending on your
working directory and how Python resolves the `helpers`/`database` package imports
inside it, since the script does `from helpers.auth import hash_password` — run it from
inside `backend/` either way.)

## 4. Agent (`backend/chatbot/`)

```bash
cd backend/chatbot
python jira_agent.py   # http://localhost:7780
```

This has its own dependency set not covered by `backend/requirements.txt` — you'll need
`agno`, the Nvidia model provider, `mcp`, `certifi`, and `truststore` installed in
whatever environment runs this file. There's no dedicated `requirements.txt` for it in
this repo.

Env vars read directly in `jira_agent.py` (all via `os.getenv`, so none are hard
requirements — but the agent won't do anything useful without a valid `NVIDIA_API_KEY`
and `JIRA_API_TOKEN`):

| Var | Default if unset |
|---|---|
| `DATABASE_URL` | `postgresql://postgres:qaszdeszqa@localhost:5432/test_db` (same literal fallback as the sibling repo) |
| `NVIDIA_API_KEY` | none |
| `JIRA_BASE_URL` | `https://lakehaylihamza.atlassian.net/` |
| `JIRA_EMAIL` | a literal Gmail address baked into the file |
| `JIRA_API_TOKEN` | none |

## 5. Frontend (`frontend/`)

```bash
cd frontend
npm install
npm run dev   # http://localhost:5174 — set explicitly in vite.config.js
```

`frontend/.env`:

| Var | Default |
|---|---|
| `VITE_API_BASE` | `http://localhost:8000` |
| `VITE_AGENT_OS_URL` | `http://localhost:7780` |

`VITE_AGENT_ID`, which the top-level README's frontend `.env` example lists, is **not
read anywhere in `ChatWidget.jsx`** — the agent id is hardcoded to `"approval-demo"`.
Setting `VITE_AGENT_ID` does nothing.

## Verifying it's wired up

1. `curl http://localhost:8000/api/health` → `{"status": "ok"}`
2. Log into `http://localhost:5174` with the admin you created
3. The Dashboard/Approvals views will be empty unless `ai.agno_approvals` already has
   rows *and* matching Jira issues already exist with the right custom fields — this
   repo doesn't create either of those for you from a clean slate (see
   [Approval Workflow](./approvals/workflow.md))
4. Open the chat widget and ask something like "show pending approvals" — this talks to
   the agent on `:7780` directly and exercises only the Jira MCP path, independent of
   whether any approvals exist in Postgres
