# Backend API (Node/Express, port 4000)

Source: `backend/src/index.js`, `backend/src/routes/products.js`, `backend/src/db.js`.

Express app with `cors` (origin from `CORS_ORIGIN`, comma-split) and `express.json()`.
All routes below are mounted; a catch-all returns `404 {"error":"Not found"}` for
anything else.

## `GET /api/health`

Runs `SELECT 1` against the pool.
- `200 {"status":"ok","db":"connected"}`
- `500 {"status":"error","db":"unreachable"}` on failure

## `GET /api/products`

Returns all rows, `ORDER BY created_at DESC`. No pagination, filtering, or query
params are supported — the frontend's search box (`ProductList.jsx`) filters
client-side over the full result set.

- `200` → array of product rows
- `500 {"error":"Failed to fetch products"}`

**Used by the frontend** — `api.listProducts()` in `frontend/src/api.js`.

## `GET /api/products/:id`

- `200` → single product row
- `404 {"error":"Product not found"}`
- `500 {"error":"Failed to fetch product"}`

**Used by the frontend** — `api.getProduct(id)`.

## `POST /api/products`

Body: `{ sku, name, category, price, stock?, status?, description?, image_seed? }`

- `sku`, `name`, `category`, `price` are required — `400` with
  `{"error":"sku, name, category and price are required"}` if any are missing
  (note: `price` is checked with `=== undefined`, so `price: 0` is accepted)
- `stock` defaults to `0` if falsy; `status` defaults to `'active'` via
  `COALESCE($6, 'active')` (only kicks in on `null`/absent, not on falsy JS values)
- `201` → created row
- `409 {"error":"A product with that SKU already exists"}` on unique-constraint
  violation (`err.code === '23505'`)
- `500 {"error":"Failed to create product"}`

**Not called anywhere in the current frontend.**

## `PUT /api/products/:id`

Body: any subset of `{ name, category, price, stock, status, description, image_seed }`.
Uses `COALESCE($n, existing_column)` per field, so omitted/`null` fields are left
unchanged — this is a partial update ("PATCH" semantics despite the verb).

- `200` → updated row
- `404 {"error":"Product not found"}`
- `500 {"error":"Failed to update product"}`

**Not called anywhere in the current frontend.** (This is the endpoint the agent's
chat-driven "restock X to N" flow could theoretically use, but in practice the agent
updates rows directly via its own Postgres MCP connection — it never calls this HTTP
endpoint. See [01-architecture.md](./01-architecture.md).)

## `DELETE /api/products/:id`

- `204` no body on success
- `404 {"error":"Product not found"}`
- `500 {"error":"Failed to delete product"}`

**Not called anywhere in the current frontend.**
