# Frontend

React 18 + Vite 5, no router, no state library — plain `useState`/`useEffect` in
`App.jsx`.

## Component tree

```
App.jsx
├── ProductList.jsx     — search box + clickable product rows, client-side filtered
├── ProductDetail.jsx   — stats for the selected product (price, stock, sku, updated_at)
└── ChatWidget.jsx       — floating chat button/panel
    └── ApprovalButton.jsx  — rendered inline in a chat bubble when a run is paused
```

## `App.jsx`

- Loads the full product list once on mount (`loadProducts`), auto-selects the first
  product if none is selected.
- Loads the selected product's detail whenever `selectedId` changes.
- Exposes `refreshAll()` — re-fetches both the list and the current detail — passed
  into `ChatWidget` as `onDataChanged`, called after any non-paused chat response so
  the dashboard reflects whatever the agent changed via MCP.
- All data comes from the **Node backend** (`GET /api/products*`) — `App.jsx` never
  talks to the agent service directly; only `ChatWidget`/`api.js` do.

## `ProductList.jsx`

- Client-side substring filter over `name`, `category`, `sku` (case-insensitive) —
  there's no server-side search endpoint to call.
- Status dot colors keyed off the raw `status` string (`active` / `low_stock` /
  `discontinued`) via a hardcoded `STATUS_LABEL` map; any other string in the DB would
  render with no friendly label (falls back to the raw string).
- Exports `STATUS_LABEL` for reuse by `ProductDetail.jsx`.

## `ProductDetail.jsx`

Pure display component — price, stock, SKU, and a localized "last updated" date derived
from `updated_at`. No edit affordances (no fields are editable from this view; editing
only happens via the chat agent's direct DB writes).

## `ChatWidget.jsx`

- Maintains its own local `messages` array (not persisted — a page reload clears the
  visible transcript, though `agent_session_id` in `localStorage` means the *agent's*
  session/history survives even if the visible chat log doesn't).
- `handleSend` calls `api.sendChatMessage`; if the result is `{paused: true, runId}`, it
  appends a bubble with a nested `ApprovalButton`; otherwise it appends the assistant's
  reply text and calls `onDataChanged()`.
- On a fetch/network error it shows an inline error bubble suggesting the agent service
  might not be running.

## `ApprovalButton.jsx`

See [06-hitl-approval-workflow.md](./06-hitl-approval-workflow.md#step-5--frontend-shows-the-approval-button)
for the full state machine. Key point: this component is **manually click-driven**, not
a `setInterval` poller — status only advances when the user clicks the button, and
`localStorage[approval_status_${runId}]` persists that status across reloads so a
half-finished approval survives a page refresh.

## `api.js` — everything the frontend can call

| Function | Calls | Used by |
|---|---|---|
| `listProducts()` | `GET {API_URL}/products` | `App.jsx` |
| `getProduct(id)` | `GET {API_URL}/products/:id` | `App.jsx` |
| `sendChatMessage(message)` | `POST {AGENT_OS_URL}/agents/{AGENT_ID}/runs` (form-encoded, `stream=false`) | `ChatWidget.jsx` |
| `getApprovalStatus(runId)` | `GET {AGENT_OS_URL}/api/approvals/{runId}` | `ApprovalButton.jsx` |
| `continueRun(runId)` | `POST {AGENT_OS_URL}/api/approvals/{runId}/continue` | `ApprovalButton.jsx` |
| `rejectRun(runId)` | `POST {AGENT_OS_URL}/api/approvals/{runId}/reject` | `ApprovalButton.jsx` |

No `createProduct`/`updateProduct`/`deleteProduct` function exists in `api.js`, matching
the fact that nothing in the UI calls the backend's `POST`/`PUT`/`DELETE`
`/api/products` routes (see [04-backend-api.md](./04-backend-api.md)).

`SESSION_ID` is generated once via `crypto.randomUUID()` (or a timestamp fallback) and
stored in `localStorage['agent_session_id']`; `sendChatMessage` updates it if the
server ever returns a different `session_id`.
