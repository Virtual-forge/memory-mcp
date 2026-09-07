# Known Gaps & Inconsistencies

These are differences between what a fresh reader would expect (from the top-level
`README.md` or from the code's own doc-comments) and what the code in this repo
actually does, found by reading every file rather than assuming the README is accurate.
Useful as a punch-list if you pick this repo back up.

## Missing pieces that will break a fresh clone

- **`run_approvals` table is never created.** `db/init.sql` only creates `products`.
  `agent.py`/`hitl.py` both assume `run_approvals` already exists. First DDL-approval
  attempt on a fresh DB will throw `relation "run_approvals" does not exist`. See
  [03-data-model.md](./03-data-model.md#run_approvals-not-in-initsql) for a DDL you can
  add.
- **No `.env.example` files anywhere** (backend, agent-service, or frontend), though
  the top-level README's setup steps say `cp .env.example .env` twice. You have to
  construct these `.env` files from the variable names referenced in the code (listed
  in [02-setup.md](./02-setup.md)).
- **`agent-service/requirements.txt` is incomplete.** It lists `agno`, `fastapi`,
  `uvicorn`, `python-dotenv`, `openai`, `anthropic`, `mcp` — but `agent.py`/`hitl.py`
  import `jira`, `psycopg2`, `certifi`, `truststore`, and `httpx`, none of which are
  declared. `pip install -r requirements.txt` alone will not be enough to run
  `python agent.py`.


## Hardcoded values worth knowing about

- `DATABASE_URL` is a literal string duplicated in both `agent.py` and `hitl.py`
  (`postgresql://postgres:qaszdeszqa@localhost:5432/test_db`), independent of the
  Node backend's `DATABASE_URL` env var — changing one does not change the other.
- `create_jira_issue_for_run` falls back to a hardcoded email address for
  `requested_by` when the run payload has no `user_id` — worth replacing with something
  environment-driven before this goes anywhere beyond a local demo.
- The frontend's `sendChatMessage` always sends `user_id: 'demo-user'` — every chat
  message looks like it came from the same user regardless of who's actually at the
  keyboard.

## Design notes (not bugs, just worth flagging)

- `products.status` has no DB-level constraint — it's a free-text column disciplined
  only by convention and the frontend's label map.
- Two independent write paths exist into `products` (Node API vs. agent's MCP
  connection) but the current frontend only exercises the Node API's `GET` routes — the
  `POST`/`PUT`/`DELETE` handlers are unused dead code from the UI's perspective, unless
  something outside this repo calls them.
- `postgres-mcp` runs with `--access-mode=unrestricted`, so all SQL-level safety is
  enforced entirely in this repo's application code (`block_unapproved_ddl_hook` +
  `run_admin_sql`'s confirmation gate) — there is no database-level or MCP-level
  restriction backing it up.
