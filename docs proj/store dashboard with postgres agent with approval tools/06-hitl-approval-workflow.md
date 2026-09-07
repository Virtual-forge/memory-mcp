# HITL Approval Workflow — End to End

This traces exactly what happens, function by function, when a user asks the chat
widget to do something that requires `DROP TABLE`/`ALTER TABLE`.

## Step 1 — user sends a message

`ChatWidget.jsx` → `api.sendChatMessage(text)` (`frontend/src/api.js`):

```js
POST http://localhost:7790/agents/postgres_agent/runs
Content-Type: application/x-www-form-urlencoded
body: message=<text>&session_id=<uuid, from localStorage>&user_id=demo-user&stream=false
```

`session_id` is a client-generated UUID, persisted in `localStorage` under
`agent_session_id`, so a browser keeps one running conversation across reloads.

## Step 2 — the agent tries `execute_sql` for the dangerous statement

The model, following its instructions, would normally call `execute_sql("DROP TABLE
products")` via the Postgres MCP tool. `block_unapproved_ddl_hook` intercepts this call
(it runs on **every** tool call), matches the `DROP TABLE|ALTER TABLE` regex, and
returns a blocking string instead of executing it — telling the model to call
`run_admin_sql` instead.

## Step 3 — the agent calls `run_admin_sql`, and Agno pauses the run

Because `run_admin_sql` is registered with `requires_confirmation=True`, Agno's run
engine pauses execution before actually running it. The response to the original
`POST .../runs` call comes back (SSE or JSON, depending on the `stream` flag) with a
top-level `status` of `PAUSED`, plus a `requirements` (or `active_requirements`) list
describing the pending tool call — `tool_call_id`, `tool_name`, `tool_args`, etc.

## Step 4 — `JiraHitlMiddleware` intercepts the response and creates a Jira ticket

`hitl.py`'s `JiraHitlMiddleware` is a Starlette `BaseHTTPMiddleware`. Its `dispatch`:

1. Calls `call_next(request)` to get the real response, and only acts further if the
   request was `POST` to a path ending in `/runs` (i.e. the initial run-creation call —
   **not** `/continue` or `/cancel`).
2. Buffers the full response body (works around the fact AgentOS may stream it).
3. If `content-type` is `text/event-stream`, walks each `data:` line, parses it as JSON,
   and looks for `status == "PAUSED"` (case-insensitive). If `application/json`, checks
   the same thing on the parsed body directly.
4. On a `PAUSED` status, calls `create_jira_issue_for_run(data)`.

`create_jira_issue_for_run`:
- Pulls `run_id`, `session_id` (tries several possible key shapes/nesting for
  resilience against AgentOS payload variations), `agent_id`, and `requested_by`
  (falls back to a hardcoded email if `user_id` is absent in the payload).
- Reads `requirements`/`active_requirements`, and for each entry whose
  `tool_execution.requires_confirmation` is true, builds a Jira issue:
  - `project`: **hardcoded to `{"key": "SCRUM"}`** — not read from `JIRA_PROJECT_KEY`
  - `issuetype`: **hardcoded to `{"name": "Task"}`** — not read from `JIRA_ISSUE_TYPE`
  - `summary`: `"Approval: {tool_name}"`
  - `description`: a JSON dump of the full `requirement` object under a `Requirements:`
    heading
  - `labels`: `run_id:<run_id>` and (if present) `session_id:<session_id>`
  - Custom fields: `FIELD_APPROVAL_ID` (set to the **requirement's own `id`**, not the
    literal string `"Pending"`), `FIELD_TOOL_NAME`, `FIELD_AGENT_ID`,
    `FIELD_APPROVAL_TYPE` (falls back to `"required"`), `FIELD_TOOL_CALL_ID`,
    `FIELD_TOOL_ARGS` (JSON string), `FIELD_RUN_ID`, `FIELD_REQUESTED_BY`, and
    `FIELD_SESSION_ID` if known
- Creates the issue via `jira.create_issue(...)`, then upserts a row into
  `run_approvals` (`run_id`, `jira_issue_key`, `requirement_json`) — see
  [03-data-model.md](./03-data-model.md) for why this insert will fail on a fresh DB.

The original `/runs` response (containing the `PAUSED` status and `run_id`) is passed
through to the frontend unchanged.

## Step 5 — frontend shows the approval button

`ChatWidget.jsx` sees `res.paused === true` and renders an `<ApprovalButton runId=... />`
bubble with the text "Action en attente de validation…".

`ApprovalButton.jsx` is **click-driven, not automatically polling**:
- Its status starts as whatever's cached in `localStorage` under
  `approval_status_${runId}` (or `"unchecked"` if none).
- Every click while not yet `"approved"` calls `GET /api/approvals/{run_id}`, updates
  local state/localStorage with whatever the endpoint returns (`pending`, `approved`,
  or `blocked`), and re-renders the button label accordingly.
- If the check comes back `"blocked"`, it immediately calls
  `POST /api/approvals/{run_id}/reject` and reports the result.
- Once status is `"approved"`, the *next* click calls
  `POST /api/approvals/{run_id}/continue` and reports the final content.

So a human has to (a) go update the Jira ticket's status themselves, and then (b) come
back and click the button in the UI at least twice (once to notice it's approved, once
more to actually continue) — there is no background timer polling every few seconds in
this code.

## Step 6 — a human moves the Jira ticket

Nothing in this repo automates the Jira-side decision — a person opens the ticket in
Jira and transitions it to whatever your workflow calls "approved" or "rejected". The
mapping from Jira's actual status *name* to the app's three-value model lives in
`agent.py`:

```python
JIRA_STATUS_MAP = {
    "to do": "pending",
    "in progress": "pending",
    "approved": "approved",
    "done": "approved",
    "rejected": "blocked",
    "blocked": "blocked",
}
```

Any Jira status name not in this dict (case-insensitively) defaults to `"pending"` —
including typos in a customized workflow, which will silently never resolve to
approved/blocked. This mapping must match your actual Jira workflow's status names, and
isn't configurable via environment variables.

## Step 7 — `continue` resumes the paused agent run

`POST /api/approvals/{run_id}/continue` (in `agent.py`):
1. Looks up the Jira issue key for this `run_id` from `run_approvals`
2. Fetches the issue from Jira, maps its status; `409` if not `"approved"`
3. Reads `session_id` off the issue's `FIELD_SESSION_ID` custom field
4. Calls `_resolve_paused_run(run_id, confirmed=True, requirement_json, session_id)`,
   which reconstructs the tool payload (`tool_call_id`, `tool_name`, `tool_args`,
   `requires_confirmation: True`, `confirmed: True`, a confirmation note) and POSTs it
   to AgentOS's **own** native endpoint:
   `POST http://localhost:7790/agents/{agent_id}/runs/{run_id}/continue`
5. Parses the (always-SSE) response for the final `content`/`status`, and returns
   `{"status": "completed", "content": ...}` to the frontend

`POST /api/approvals/{run_id}/reject` follows the same shape but requires the mapped
status to already be `"blocked"`, and calls AgentOS's
`POST .../runs/{run_id}/cancel` instead — it does **not** send `confirmed=False`
through `/continue`; rejection is a hard cancel of the run.

## Diagram

```
User: "Drop the products table"
        │
        ▼
POST /agents/postgres_agent/runs  ──────────────────────────►  AgentOS
        │                                                         │
        │                                          agent tries execute_sql(DROP TABLE...)
        │                                                         │
        │                                    block_unapproved_ddl_hook blocks it
        │                                                         │
        │                                    agent calls run_admin_sql (requires_confirmation)
        │                                                         │
        │                                          Agno pauses run → status: PAUSED
        │◄────────────────────────────────────────────────────────┘
        │
JiraHitlMiddleware sees PAUSED → creates Jira ticket → upserts run_approvals row
        │
Frontend shows "Action en attente de validation…" + ApprovalButton
        │
        ├── click → GET /api/approvals/{run_id} → still pending, keep waiting
        │
   (human moves Jira ticket to "Approved" or "Rejected")
        │
        ├── click again → status now "approved" → button becomes "click to confirm"
        │       └── click → POST /api/approvals/{run_id}/continue
        │               → AgentOS /runs/{run_id}/continue (confirmed=True)
        │               → run_admin_sql actually executes the DROP TABLE
        │
        └── (if ticket was moved to "Rejected"/"Blocked")
                └── auto POST /api/approvals/{run_id}/reject
                        → AgentOS /runs/{run_id}/cancel
```
