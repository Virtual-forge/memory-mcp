# Backend API

## Startup

`backendfiles for reading/main.py` creates a FastAPI application named
`VS Code Agent Governance Gateway`.

At import/startup it:

1. Calls `load_dotenv()`.
2. Reads Jira settings from environment variables.
3. Adds permissive CORS middleware.
4. Calls `init_db()` from `database.py`.
5. Creates an empty in-memory `active_connections` map.

Running the file directly starts Uvicorn on `0.0.0.0:8000`.

The database URL is read in `database.py`:

```text
DATABASE_URL or postgresql://postgres:qaszdeszqa@localhost:5432/governance_db
```

The default contains a development password and should not be used outside a
local prototype.

## Database Schema

The table is named `agent_states`.

```mermaid
erDiagram
    AGENT_STATES {
        string thread_id PK
        string jira_key
        string status
        json checkpoint_data
        datetime created_at
        datetime updated_at
    }
```

`checkpoint_data` is intentionally flexible JSON. The extension currently
stores fields such as:

```json
{
  "agent_name": "chat-gov-agent",
  "workspace": "C:/work/project",
  "pending_step": "EXECUTE_deploy_to_production",
  "tool_name": "deploy_to_production",
  "tool_input": {
    "command": "kubectl apply -f prod.yaml",
    "targetEnv": "production"
  }
}
```

The hook stores a different shape containing `session_id`, `tool_use_id`, and
`cwd`. The JSON column allows both producers to use the same table.

## Environment Variables

| Variable | Required for | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL connection | SQLAlchemy connection string. |
| `JIRA_BASE_URL` | Jira operations | Base URL, for example `https://company.atlassian.net`. |
| `JIRA_EMAIL` | Jira operations | Jira account email used for basic authentication. |
| `JIRA_API_TOKEN` | Jira operations | Jira API token. |
| `JIRA_PROJECT_KEY` | Jira issue creation | Project key for approval Tasks. |

If Jira settings are incomplete, the gateway can still start and serve rows,
but it cannot create issues or synchronize a paused record from Jira.

## Endpoint Reference

### `POST /api/v1/pause`

Request body:

```json
{
  "thread_id": "approval_abc123",
  "action_type": "deploy_to_production",
  "checkpoint_data": {
    "tool_name": "deploy_to_production",
    "tool_input": {
      "targetEnv": "production",
      "command": "kubectl apply -f prod.yaml"
    }
  }
}
```

Processing order:

1. Query `agent_states` by `thread_id`.
2. If found, return the existing status and Jira key without creating a new
   Jira issue.
3. Otherwise create a Jira Task with a summary of
   `Approval Required: Agent Action [<action_type>]`.
4. Put the thread ID and pretty-printed checkpoint JSON in the Jira
   description.
5. Insert a `PAUSED` row and commit it.
6. Return the Jira key and thread ID.

New records return a response similar to:

```json
{
  "status": "paused",
  "jira_issue": "GOV-123",
  "thread_id": "approval_abc123"
}
```

Existing records return the stored status lowercased. The extension uppercases
statuses before comparing them.

### `GET /api/v1/status/{thread_id}`

Looks up one row. If its status is `PAUSED`, `sync_jira_status()` makes a Jira
REST request for the issue's current status before returning:

```json
{
  "thread_id": "approval_abc123",
  "jira_key": "GOV-123",
  "status": "APPROVED",
  "checkpoint_data": {}
}
```

Returns HTTP 404 when the thread does not exist.

### `GET /api/v1/pending-approvals`

Returns rows in `PAUSED`, `APPROVED`, or `REJECTED`. It synchronizes paused
rows from Jira before serializing them. `COMPLETED` rows are intentionally
excluded because they are no longer recoverable.

This endpoint is used by the extension's background reconciler and its
`continue` recovery flow.

### `GET /api/jira/approvals`

Returns approval records for a dashboard or administrative client. By default
it returns all statuses, including `COMPLETED`.

Use `?status=PAUSED`, `?status=APPROVED`, or another status to filter. The
special value `all` disables filtering.

### `POST /api/v1/mark-completed/{thread_id}`

Sets the matching row to `COMPLETED` and commits it. Returns HTTP 404 if the
thread is not found.

The gateway does not itself invoke tools. The caller is responsible for
invoking the approved action first and marking completion afterward.

### `GET /api/v1/ws/{thread_id}`

Accepts a WebSocket and stores it in `active_connections` under the thread ID.
The route waits for client input until the connection closes. The client does
not need to send application messages; the open connection is used as a route
for webhook events.

Only one active WebSocket is retained per thread ID. A newer connection
replaces the previous map entry.

### `POST /jira-webhook`

Expected Jira webhook data includes:

- `issue.key`; and
- a `changelog.items` entry whose `field` is `status` and whose `toString`
  contains the new status name.

The gateway finds the row by Jira key, maps the status, commits it, and sends
this event to the active WebSocket when one is connected:

```json
{
  "status": "APPROVED",
  "jira_key": "GOV-123",
  "checkpoint_data": {}
}
```

Unknown issues return `thread_not_found`; webhook payloads without a status
change return `ignored`.

## Jira Integration

`create_jira_ticket()` calls:

```text
POST {JIRA_BASE_URL}/rest/api/3/issue
```

It authenticates with `(JIRA_EMAIL, JIRA_API_TOKEN)` and creates a Jira
`Task`. The checkpoint is embedded in the Jira description so the human
reviewer can see the requested action.

`sync_jira_status()` calls:

```text
GET {JIRA_BASE_URL}/rest/api/3/issue/{jira_key}?fields=status
```

It only runs for rows still marked `PAUSED`. A successful status mapping is
committed to PostgreSQL. HTTP failures and request exceptions are logged and
leave the row unchanged.

## Request and Response Ownership

```mermaid
flowchart TD
    pause[Client POST /pause] --> jira_create[Create Jira issue]
    jira_create --> save[Save PAUSED state]
    save --> wait[Client waits]
    jira_webhook[Jira webhook] --> map[Map Jira status]
    poll[Client GET /status] --> sync[Optional Jira status sync]
    map --> save_decision[Save APPROVED or REJECTED]
    sync --> save_decision
    save_decision --> notify[Optional WebSocket event]
    notify --> invoke[Client invokes tool]
    invoke --> complete[Client POST /mark-completed]
```

The database is the coordination point. Jira is the human-facing decision
system, but clients read the gateway rather than querying Jira directly.

## Operational Notes

- `allow_origins=["*"]` and all methods/headers are enabled for CORS.
- The gateway has no authentication or authorization layer.
- Jira issue creation does not currently pass an explicit request timeout.
- `active_connections` is process-local; multiple gateway processes would not
  share WebSocket state.
- Table creation is automatic, but schema evolution is not managed by a
  migration tool.
- The source tree does not include `requirements.txt` or backend tests.