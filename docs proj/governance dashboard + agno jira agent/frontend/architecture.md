# Frontend

React 18 + Vite 5, plain JSX (no TypeScript), no router — `App.jsx` is a simple
logged-in/logged-out switch, not a multi-route app.

## Component tree

```
App.jsx                    — auth gate: renders Login or Dashboard
├── Login.jsx               — email/password form → api.login()
└── Dashboard.jsx           — sidebar nav (Dashboard/Approvals/Settings), KPI cards,
    │                          ProgressRing + AgentRequestChart, approvals list, chat toggle
    ├── RequestCard.jsx      — one expandable approval row, Approve/Block buttons
    ├── AgentRequestChart.jsx — horizontal bar breakdown of requests by agent_id
    └── ChatWidget.jsx        — chat panel, talks directly to AgentOS (see below)
```

There is no `TypeScript`, no `React Router`, and no separate "protected route" component
— the entire access-control model is `App.jsx`'s single `if (!loggedIn) return
<Login/>` check, backed by `isLoggedIn()` (`Boolean(localStorage.getItem("token"))`) in
`api.js`. This is a real simplification worth knowing if you were expecting route-level
guards.

## `api.js` — the authenticated fetch wrapper

```js
export async function request(path, options = {}) {
  const token = getToken();
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "ngrok-skip-browser-warning": "true",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });
  if (res.status === 401) {
    localStorage.removeItem("token");
    localStorage.removeItem("email");
    window.location.reload();
    throw new Error("Session expired");
  }
  ...
}
```

Every request unconditionally sends an `ngrok-skip-browser-warning` header — a sign
this was developed/demoed through an ngrok tunnel; harmless against a normal backend,
but worth knowing it's there if you're wondering why it's in every request.

Functions exported: `login`, `logout`, `getStoredEmail`, `isLoggedIn`,
`listApprovals(status)`, `resolveApproval(issueKey, decision)`. That's the complete
surface — there is no direct call anywhere in the frontend to `/api/approvals` (the
direct-DB path); `listApprovals`/`resolveApproval` both target `/api/jira/approvals...`
exclusively (see [Approval Workflow](../approvals/workflow.md)).

`listApprovals` also does client-side reshaping — it maps each Jira issue into the
shape `RequestCard`/`Dashboard` expect, and **always sets `resolved_by: null`**
regardless of what Jira says, since `jira_client.simplify_issue` on the backend doesn't
return a `resolved_by`/assignee-based field for this purpose. In practice, `RequestCard`'s
"Approved by {resolved_by}" / "Blocked by {resolved_by}" text will always render as
"— " (its fallback for a falsy value) — the resolver's identity never actually reaches
the UI today.

## `Dashboard.jsx`

- Fetches `listApprovals("all")` on mount and every 15 seconds
  (`setInterval(load, 15000)`) — this polling interval is real and matches what the
  README claims, unlike several other README claims in this project.
- `counts`/`filteredRequests` are derived client-side from the single `allRequests`
  array — there's no server-side filtering beyond the initial JQL fetch; the status
  dropdown and search box both filter what's already in memory.
- `ProgressRing` (defined inline in this file) and `AgentRequestChart` are both pure
  presentational components computed from `allRequests`/`filteredRequests` — no
  additional network calls.
- The chat panel is just `<ChatWidget />` rendered inside a fixed-position `div` when
  `showChat` is true — `ChatWidget` itself is unaware it's inside a floating panel.

## `ChatWidget.jsx`

Talks to `VITE_AGENT_OS_URL` (default `http://localhost:7780`) **directly** — not
through the FastAPI backend, despite the backend having a working proxy for exactly
this (`/agents/{full_path}`, `/sessions/{full_path}` in `main.py`). The file's own
top-of-file comment says this is intentional and flags it as unauthenticated at the
AgentOS level.

- `AGENT_ID` is hardcoded to `"approval-demo"` — matching `jira_agent.py`'s
  `Agent(name="approval-demo", ...)`. There is no `VITE_AGENT_ID` env var read anywhere
  in this file, despite one being listed in the top-level README's frontend `.env`
  example (`VITE_AGENT_ID=postgres_agent` — which is also the wrong agent id for this
  repo; `postgres_agent` is the sibling `inventory-approval` repo's agent id, not this
  one's).
- `user_id` sent with every chat request is hardcoded to a literal email address
  (`"hamzalakehayli@gmail.com"`) with the comment "static email for now" — every chat
  message looks like it came from the same user regardless of who's logged into the
  dashboard.
- Session id: a random UUID (or timestamp fallback) stored in `sessionStorage`
  (cleared when the tab closes) — not `localStorage`, so unlike the login token, a chat
  session doesn't survive a browser restart. Message history is also cached in
  `sessionStorage` under a separate key, restored on mount.
- On sending a message, if the AgentOS response's `status` is `"PAUSED"`, the widget
  starts polling `GET {AGENT_OS_URL}/sessions/{session_id}/runs/{run_id}` every 5
  seconds (`POLL_INTERVAL_MS`), up to 60 attempts (~5 minutes, `POLL_MAX_ATTEMPTS`),
  until the status is no longer `PAUSED`. Since `jira_agent.py`'s only tool
  (`mcp-atlassian`) has no `requires_confirmation` flag set anywhere in this repo, this
  pause/poll path is defensive code for a state this particular agent shouldn't
  actually be able to enter — but it's real, functioning logic if some other
  configuration of the agent ever did have a gated tool.

## `RequestCard.jsx`

Expandable row; `Approve`/`Block` buttons only render when `request.status ===
"pending"`. Calls straight into the `onResolve` prop passed down from `Dashboard.jsx`
(which is `handleResolve`, wrapping `resolveApproval`). Optimistically patches the
local `status` in React state on success rather than re-fetching — so a resolved card
flips to its new status instantly in the UI even before the next 15-second poll
confirms it against Jira.

## Styling

Plain CSS (`index.css`, ~36KB) with CSS custom properties for theming (`--cerulean-*`,
`--surface`, `--border`, etc.) — no CSS framework, no CSS-in-JS. A fair amount of the
newer dashboard UI (KPI cards, progress ring, agent chart) uses inline `style={}`
objects directly in `Dashboard.jsx` rather than the shared stylesheet, which is worth
knowing if you go looking for those rules in `index.css` and don't find them.
