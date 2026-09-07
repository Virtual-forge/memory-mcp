# Data Model

## `products` (defined in `db/init.sql`)

```sql
CREATE TABLE products (
    id          SERIAL PRIMARY KEY,
    sku         VARCHAR(32) UNIQUE NOT NULL,
    name        VARCHAR(200) NOT NULL,
    category    VARCHAR(100) NOT NULL,
    price       NUMERIC(10, 2) NOT NULL,
    stock       INTEGER NOT NULL DEFAULT 0,
    status      VARCHAR(20) NOT NULL DEFAULT 'active', -- active | low_stock | discontinued
    description TEXT,
    image_seed  VARCHAR(100),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Notes:
- `status` is a free-text `VARCHAR`, not a Postgres `ENUM` or `CHECK` constraint — the
  three-value convention (`active` / `low_stock` / `discontinued`) is enforced only by
  the seed data and by the frontend's `STATUS_LABEL` map in `ProductList.jsx`; nothing
  stops the agent (or a REST call) from writing any other string into `status`.
- `updated_at` is kept current by a trigger (`set_updated_at()`), fired `BEFORE UPDATE`.
- Seeded with 12 rows spanning `Peripherals`, `Displays`, `Audio`, `Computers`,
  `Furniture`, `Wearables`, `Accessories`, with a mix of all three statuses (including
  two `discontinued` items with `stock = 0`).

## `run_approvals` — used at runtime, **not** in `init.sql`

`agent-service/hitl.py` and `agent-service/agent.py` both read/write a `run_approvals`
table:

```python
# hitl.py — on creating a Jira ticket for a paused run
cur.execute(
    "INSERT INTO run_approvals (run_id, jira_issue_key, requirement_json) VALUES (%s, %s, %s) "
    "ON CONFLICT (run_id) DO UPDATE SET jira_issue_key = EXCLUDED.jira_issue_key, "
    "requirement_json = EXCLUDED.requirement_json",
    (run_id, new_issue.key, json.dumps(requirement)),
)

# agent.py — on checking approval status / resuming
cur.execute(
    "SELECT jira_issue_key, requirement_json FROM run_approvals WHERE run_id = %s", (run_id,)
)
```

This table is **never created anywhere in this repo** — not in `db/init.sql`, not via
migration, not via `CREATE TABLE IF NOT EXISTS` in the Python code. Running the approval
flow against a freshly-initialized database (via `init.sql` alone) will fail with
`relation "run_approvals" does not exist` the first time the agent tries to pause a run.

Based on how the code uses it, the minimal schema it needs is:

```sql
CREATE TABLE run_approvals (
    run_id           VARCHAR PRIMARY KEY,
    jira_issue_key   VARCHAR NOT NULL,
    requirement_json JSONB NOT NULL
);
```

(The `ON CONFLICT (run_id)` clause in the insert requires `run_id` to be a unique/primary
key — that's the one hard constraint implied by the code. Column types are inferred, not
verified against a real deployment.)

## Products table access patterns

- The Node backend reads/writes `products` through parameterized queries in
  `backend/src/routes/products.js` (standard `pg` pool).
- The agent reads/writes the same table through the `postgres-mcp` MCP server, which
  gets its own connection string via the `DATABASE_URI` env var passed into
  `MCPTools(command=..., env={"DATABASE_URI": DATABASE_URL})` — configured with
  `--access-mode=unrestricted`, i.e. no query restrictions are applied at the MCP layer
  itself (all gating happens in application code — see
  [06-hitl-approval-workflow.md](./06-hitl-approval-workflow.md)).
