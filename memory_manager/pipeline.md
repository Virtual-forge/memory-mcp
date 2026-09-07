# Pipeline jobs

`MemoryPipeline` is the application boundary for scheduled work. Call
`ingest_turn()` on the request path; it writes L0 and returns the independent
inactivity/volume decision without calling an LLM. `memory_manager.worker`
discovers active sessions and scopes, calls `run_extraction_if_due()` for each
session, and calls `run_synthesis_job()` per scope after its L2 trigger fires.
Both jobs release their database transaction before the model call and index
only the rows they created or changed after the write succeeds.

Extraction stores an attempt cursor separately from successful coverage. A
turn with no atom therefore remains in the next extraction batch, while an
unchanged no-atom batch does not trigger the judge on every worker poll. The
library does not start a hidden daemon; run `memory-worker`, a task queue, or a
custom scheduler according to the deployment's runtime policy.

Project imports are a separate pipeline. `import_project_documents()` accepts a
confirmed project UUID and a recursive Markdown folder, persists versioned
document chunks, runs the project-only extraction judge with single-chunk
evidence allowed, synthesizes only `project_sections`, and indexes the changed
records. The importer retains nested relative paths and heading line ranges.
When a file changes or disappears, its old chunks are deactivated and atoms or
sections with no valid active source are removed from retrieval and Qdrant.
