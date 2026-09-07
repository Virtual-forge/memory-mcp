# Retrieval

`RecallService.recall()` is intentionally a short orchestrator. It selects a
mode, searches the shared Qdrant collection, reranks the candidate pool, applies
`min_score` to calibrated scores, and hydrates surviving IDs from Postgres.
`search_candidates()` skips reranking by design and returns title-only stubs.

Without a `project_id`, both the Qdrant filter and Postgres hydration accept
only generic atoms and general L2 categories. Project sections require an
explicit confirmed project UUID, which is matched at every retrieval and
provenance boundary.
