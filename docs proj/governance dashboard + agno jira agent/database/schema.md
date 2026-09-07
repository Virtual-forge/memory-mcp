# Database Schema

## Tables the application code actually touches

### `admins` (public schema)

```sql
CREATE TABLE admins (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Defined identically in both `database/migrations.sql` and `helpers/create_admin.py`
(the latter runs `CREATE TABLE IF NOT EXISTS` again before inserting, so it works even
if you never ran `migrations.sql`). Used by `POST /api/auth/login` to verify a
`bcrypt`-hashed password and issue a JWT.

### `ai.agno_approvals` — Agno's table, not this repo's

This is the table every real approval read/write in this codebase actually hits:
`main.py`'s `list_approvals`/`resolve_approval`, and `jira_webhook.py`'s webhook
handler. **Nothing in this repo creates it** — it's created by Agno's own
`PostgresDb(approvals_table="agno_approvals")` machinery, presumably by an agent
service outside this repo/branch that uses `requires_confirmation` tools (see
[Approval Workflow](../approvals/workflow.md)).

Its real column list, per a comment in `001_jira_listener_setup.sql` (attributed to a
one-off `inspect_table.py` script that isn't in this repo):

```
id, run_id, session_id, status, source_type, approval_type, pause_type, tool_name,
tool_args, expires_at, agent_id, team_id, workflow_id, user_id, schedule_id,
schedule_run_id, source_name, requirements, context, resolution_data, resolved_by,
resolved_at, created_at, updated_at, run_status
```

Key real types (confirmed by the same comment, and consistent with how the Python code
handles them):
- `id` is `character varying`, **not** `uuid` — `main.py`'s `row_to_approval` explicitly
  does `data["id"] = str(data["id"])`
- `resolved_at`, `created_at`, `updated_at`, `expires_at` are **epoch bigints**, not
  `timestamptz` — `main.py` wraps them in `to_timestamp(...)` at query time to get a
  real timestamp back
- `context`, `requirements`, `resolution_data` are `jsonb`

### `ai.tool_descriptions` — owned by this repo

```sql
CREATE TABLE ai.tool_descriptions (
    tool_name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

LEFT JOINed onto `ai.agno_approvals` by `tool_name` in both `main.py`'s approval
queries and `jira_dashboard.py`'s `fetch_tool_descriptions()`, purely to attach a
human-readable blurb. Populated by running `helpers/sync_tool_descriptions.py`, whose
`TOOLS`/`TOOLS_MANUAL` lists ship **empty** — you have to edit the script to point at
your actual tools (or hardcode name→description pairs) before it does anything; running
it unedited just prints a warning and exits.

## Tables defined in this repo's migrations but not used by the app

### `ai.approvals` — created, never queried

`migrations.sql` creates `ai.approvals` (`UUID` id, `TIMESTAMPTZ` timestamps, its own
`status`/`resolved_by`/`resolved_at` columns) with a comment explaining it's meant as a
patch/adapter in case Agno's own table doesn't have `context`/`requested_by` columns.
In practice, no file in `backend/` ever runs a query against `ai.approvals` — every real
read/write targets `ai.agno_approvals` instead. This table, if created, sits empty and
unused.

### `jira_sync` — created, trigger fires, nothing listens

`database/001_jira_listener_setup.sql` (the only migration `run_migration.py` actually
runs) creates:

```sql
CREATE TABLE jira_sync (
    approval_id TEXT PRIMARY KEY,
    jira_issue_key TEXT,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    synced_at TIMESTAMPTZ
);
```

plus a trigger on `ai.agno_approvals`:

```sql
CREATE TRIGGER trg_notify_new_approval
    AFTER INSERT ON ai.agno_approvals
    FOR EACH ROW
    WHEN (NEW.status = 'pending' OR NEW.approval_type = 'audit')
    EXECUTE FUNCTION notify_new_approval();  -- PERFORM pg_notify('new_approval', NEW.id)
```

The file's own header comment references a **`approval_listener.py`** that would
`LISTEN` on the `new_approval` channel and be responsible for actually creating the
Jira issue and populating `jira_sync`. That file does not exist anywhere in this repo.
So as shipped: new rows in `ai.agno_approvals` fire a Postgres `NOTIFY`, the `jira_sync`
table exists and is ready to be written to, but nothing in this codebase is listening
or writing to it — a new approval will not automatically get a Jira issue created for
it. See [Known Gaps](../known-gaps.md).

## Migration runners, and which one actually runs which file

- `helpers/create_admin.py` — self-contained, creates `admins` only, then inserts one row
- `database/run_migration.py` — hardcoded to read and execute **only**
  `001_jira_listener_setup.sql`. It does **not** run `migrations.sql`.
- `migrations.sql` itself has no Python runner in this repo — it's meant to be applied
  by hand (`psql "$DATABASE_URL" -f backend/database/migrations.sql`), per its own header
  comment.

So a from-scratch setup needs, in order: `migrations.sql` run manually via `psql`
(admins, `ai.approvals`, `ai.tool_descriptions` — even though `ai.approvals` ends up
unused), then `python run_migration.py` for the `jira_sync` table/trigger, then
`ai.agno_approvals` needs to already exist from wherever your Agno approval-generating
agent creates it — this repo alone never creates that table.
