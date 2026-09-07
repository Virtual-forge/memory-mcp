# Backend API Routes

Source: `backend/main.py`, `backend/jira/jira_dashboard.py`, `backend/jira/jira_webhook.py`.
All mounted on the single FastAPI app in `main.py` (`app.include_router(jira_router)`,
`app.include_router(jira_dashboard_router)`), served on port 8000. CORS is wide open
(`allow_origins=["*"]`, `allow_credentials=True`) — the code comment flags this as a
dev-only setting to tighten before production.

## Health

`GET /api/health` → `{"status": "ok"}`. No DB check (unlike the sibling
`inventory-approval` repo's health endpoint, which does ping the database).

## Auth

`POST /api/auth/login` — body `{email, password}`. Looks up `admins` by email, verifies
with `bcrypt`, returns `{token, email}` (`token` is a JWT signed with `JWT_SECRET`, valid
12 hours per `helpers/auth.py`'s hardcoded `TOKEN_EXPIRE_HOURS = 12` — not the 7 days
suggested elsewhere). `401` on bad credentials.

## Direct-DB approvals (`main.py`) — defined, but **not called by the current frontend**

| Route | Auth | What it does |
|---|---|---|
| `GET /api/approvals?status=pending\|approved\|rejected\|all` | JWT required | Queries `ai.agno_approvals` LEFT JOIN `ai.tool_descriptions`, `LIMIT 200`, newest first |
| `POST /api/approvals/{id}/resolve` | JWT required | Body `{decision: "approved"\|"rejected"}`. `UPDATE ... WHERE id = $1 AND status = 'pending'` — the `AND status = 'pending'` guard is what prevents two admins racing on the same request; returns `409` if the row was already resolved (or doesn't exist) |

See [Architecture Overview](../architecture/overview.md#two-approval-code-paths-exist-only-one-is-wired-into-the-ui)
for why these exist alongside the Jira-native routes below but sit unused by the shipped UI.

## Jira-native approvals (`jira_dashboard.py`) — what the frontend actually calls

| Route | Auth | What it does |
|---|---|---|
| `GET /api/jira/approvals?status=pending` | **none** | Runs a JQL query (`project = "{JIRA_PROJECT_KEY}" AND issuetype = Task`, optionally `AND status = "{JIRA_PENDING_STATUS_NAME}"`), returns `{approvals: [...]}` via `jira_client.simplify_issue` |
| `GET /api/jira/approvals/{issue_key}` | **none** | Single-issue equivalent |
| `POST /api/jira/approvals/{issue_key}/resolve` | **none** | Body `{decision: "approved"\|"rejected"}` → transitions the Jira issue to `"Approved"`/`"Rejected"` via `jira_client.transition_issue`. `409` if no such transition exists from the issue's current status |

None of these three routes have a `Depends(get_current_admin_email)` — they're callable
by anyone who can reach port 8000, regardless of login state. The dashboard's only real
gate is that the React app won't render the `Dashboard` component (and thus won't call
these) until `isLoggedIn()` is true — that's a client-side UI gate, not an API-level one.

`PENDING_STATUS_NAME` defaults to `"Pending"` but is overridable via
`JIRA_PENDING_STATUS_NAME` — useful if your Jira workflow's initial status has a
different name.

## Jira webhook (`jira_webhook.py`)

`POST /webhooks/jira-approval` — the endpoint a Jira Automation rule is meant to call
when an approval issue transitions. Requires header `X-Webhook-Secret` matching
`JIRA_WEBHOOK_SECRET`; `401` if it doesn't match. Body:

```json
{"issue_key": "...", "approval_id": "...", "status": "Approved", "resolved_by": "..."}
```

Maps `status` through a local `STATUS_MAP` (`"Approved" → "approved"`,
`"Rejected" → "rejected"`; anything else is silently ignored and returns
`{"ignored": true, ...}`), then does:

```sql
UPDATE ai.agno_approvals
SET status = $1, resolved_by = $2, resolved_at = $3, updated_at = $3
WHERE id = $4 AND status = 'pending'
```

Returns `{"updated": false}` (still `200`, not an error) if the row was already
resolved or `approval_id` didn't match anything — deliberately, so Jira doesn't retry a
delivery it thinks failed.

**This is the only place in this repo that writes a resolved status back to
`ai.agno_approvals` from the Jira side** — and it depends entirely on a Jira Automation
rule you configure yourself outside this codebase; nothing here creates that rule.

## Agent proxy (`main.py`)

`ANY /agents/{full_path}` and `ANY /sessions/{full_path}` — generic reverse proxies to
`http://127.0.0.1:7780/...` (the AgentOS process). No auth, no path validation beyond
what FastAPI's path converter does. As covered in
[Architecture Overview](../architecture/overview.md), the shipped `ChatWidget.jsx` does
**not** route through these — it calls port 7780 directly.

## Route that's documented elsewhere but doesn't exist as a live endpoint

`helpers/chatbot.py` defines `POST /api/chat`, forwarding to
`{AGNO_AGENT_URL}/api/agent/jira-approval-agent/run` (a different URL shape than what
`jira_agent.py`'s AgentOS actually exposes, which is `/agents/{agent_id}/runs`). This
router is **never imported or mounted in `main.py`** — grep the whole `backend/`
directory and the only `app.include_router(chatbot_router)` call is inside
`chatbot.py`'s own module-level docstring, describing how *you'd* wire it in, not
something `main.py` actually does. `POST /api/chat` is not a reachable endpoint on the
running app. See [Known Gaps](../known-gaps.md).
