# Memory Manager

A structured long-term memory pipeline built from the accompanying
`spec/memory-system-build-spec.md`.

The implementation keeps raw turns in Postgres, extracts precise L1 atoms with
provenance, synthesizes category-specific L2 rows, indexes atoms and L2 rows in
one Qdrant collection, and exposes the resulting operations through MCP.

## Local services

- Postgres: relational source of truth
- Qdrant: dense and sparse vector index
- An OpenAI-compatible endpoint: extraction, synthesis, optional reranking, and
	optionally dense embeddings

Copy `.env.example` to `.env`, install with `pip install -e ".[dev]"`, and apply
the schema before starting the services:

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
psql $env:DATABASE_URL -f memory_manager/db/schema.sql
memory-mcp
```

Start the asynchronous extraction/synthesis worker in a second terminal:

```powershell
memory-worker
```

The worker polls Postgres every `WORKER_POLL_SECONDS` (30 seconds by default).
L1 extraction runs after `EXTRACTION_INACTIVITY_MINUTES` or
`EXTRACTION_VOLUME_CAP` pending turns. L2 synthesis runs after
`SYNTHESIS_INACTIVITY_MINUTES` or `SYNTHESIS_ATOM_VOLUME` pending atoms.
Extraction attempts advance a cursor even when the judge returns no atom, but
only turns linked to an atom are marked covered. This keeps ordinary L0 turns
available to corroborate a later turn.

## Project documentation imports

Project documentation is opt-in and requires a confirmed project identity before
the worker performs extraction or synthesis. The AgentOS example first calls
`list_projects`, presents every registered project with Agno's native
`UserFeedbackTools.ask_user`, and uses the selected UUID as the authoritative
project identity. The selector shows up to three project titles and reserves a
fourth option for `Other options`. That option opens a second structured feedback
question with `Recall memory` and `Cancel`; the former performs generic recall using
the original request, while the latter stops the lookup. If more than three projects are registered, the selector asks for
an exact scope instead of silently dropping options. A missing identity must not
be guessed.

The importer accepts a folder containing one or more UTF-8 Markdown files with
any depth of nested subfolders. It imports only `.md` and `.markdown` files,
preserves each nested relative path (for example,
`architecture/system.markdown`), and ignores symlinks and common build, cache,
vendor, and virtual-environment directories. Unsupported formats are skipped;
they are not converted into Markdown. Files are split at headings and paragraph
boundaries with heading and line-range provenance, then stored as versioned
project document chunks.

After confirmation, import a folder with the worker CLI:

```powershell
memory-worker import-project C:\path\to\project-docs --project-id <confirmed-uuid>
```

Project chunks use a dedicated L1 extraction prompt that permits a direct claim
from one authoritative chunk. Project atoms are synthesized only into
project-scoped `project_sections` rows. Generic conversation extraction,
synthesis, and unscoped retrieval exclude project documentation; project reads
must carry the confirmed `project_id`. Changed or deleted files deactivate old
chunks, hide atoms that have no remaining active source, rebuild affected
sections, and remove obsolete Qdrant points.

The dispatcher keeps recall bounded to exactly three attempts per agent run:
L2, L2, then L1+L2. Project identity confirmation is separate from that recall
budget.

The MCP server defaults to Streamable HTTP on `0.0.0.0:8000`, with its MCP
endpoint at `/mcp`. To expose it through ngrok, forward port 8000 and configure
the agent with the forwarded URL plus `/mcp`, for example:

```text
https://footbath-handshake-devouring.ngrok-free.dev/mcp
```

The example agent reads `MEMORY_MCP_URL`; it defaults to
`http://localhost:8000/mcp` for the local MCP server. Set it to the current
ngrok URL, including `/mcp`, only when using a live tunnel. Set
`LLM_MODEL=gpt-5.6-luna` when using
the Luna-compatible endpoint; this is also the worker's default. MCP tool calls
wait up to 60 seconds by default; set `MEMORY_MCP_TIMEOUT_SECONDS` to increase
that limit when retrieval or embeddings are slower.

### Scope selection

Scopes are optional filters on reads, not values the agent should invent. A
named scope narrows recall, candidate search, fetch, or provenance drill-down
to that namespace. When the agent does not have a reliable scope from the
user or runtime context, it should pass `all_scopes=true` instead. This removes
the scope filter so ranked retrieval can find a memory regardless of which
conversation, user, or project namespace created it. `top_k` still limits the
ranked results; `all_scopes` does not enumerate every record.

Writes still need a destination. Set `MEMORY_USER_ID=alice` to make the default
user scope `user:alice`; when `MEMORY_USER_ID` is absent, the local OS account is
used. `remember` and automatic Agno capture use that user scope unless an
explicit `DEFAULT_SCOPE` or write scope is supplied. Use a project scope only
when the memory is intentionally shared, for example `project:inventory`.

The current server has no authentication layer, so `all_scopes=true` means every
scope visible to this server. Add authorization before exposing that mode to
untrusted callers.

Run Qdrant locally at the configured `QDRANT_URL`. The first startup downloads
the local FastEmbed sparse model and validates the dense vector dimensions before
creating the shared collection. Set `DENSE_EMBEDDING_BACKEND=fastembed` for a
local dense model, or set it to `openai-compatible` to use the configured
`LLM_BASE_URL` and `LLM_API_KEY` with a model such as `text-embedding-3-small`.
FastEmbed model assets are cached persistently under
`~/.cache/fastembed` by default; override this with `FASTEMBED_CACHE_DIR` when
the worker should use another cache location. The sparse BM25 model is local
even when dense embeddings use the remote OpenAI-compatible endpoint, so its
first-run download from Hugging Face is expected.
`LLM_API_KEY` is required by `build_memory_pipeline()` for extraction and
synthesis; MCP recall can use the offline keyword reranker when
`RERANKER_MODEL` is unset.

On Windows, runtime startup uses `truststore` so Qdrant Cloud and other HTTPS
clients use the operating system certificate store.

Changing the dense model or its dimensions requires a new or recreated Qdrant
collection followed by reindexing. Qdrant cannot change a collection's vector
dimension in place.

The pipeline is intentionally dependency-injected: tests use in-memory fakes for
LLM, embeddings, Qdrant, and database boundaries, so the core behavior can be
validated without running either service.

## Build order implemented

1. Relational schema and typed data shapes
2. Async-capable extraction integration with idempotency and entity resolution
3. L2 routing and upsert synthesis
4. Vector, BM25, and Qdrant-native hybrid retrieval
5. Calibrated reranking and thresholds
6. MCP tools for remember, recall, candidate search, fetch, drill-down, update, and forget

See the modules under `memory_manager/` for the corresponding spec sections.

## Worker integration

The example AgentOS process in `agents/test_agent.py` registers Agno
`pre_hooks` and `post_hooks`. Those hooks synchronously write user input,
assistant output, tool-call arguments, and tool results to L0. Event IDs make
repeated hook delivery idempotent. The hooks do not call an LLM, extract atoms,
or synthesize L2 rows, so the agent request path stays short.
AgentOS also uses Agno's `PostgresDb` with the same `DATABASE_URL` as the memory
pipeline; this persists paused HITL runs so an edited `save_memory` request can
be continued after AgentOS rebuilds the agent for the continuation request.

Run the example agent from the `agents` directory in another terminal:

```powershell
Set-Location agents
python test_agent.py
```

The separate `memory-worker` process constructs `build_memory_pipeline()` and
performs all LLM work outside AgentOS request handling. Applications that need
their own scheduler can call `ingest_turn()`, `run_extraction_if_due()`, and
`run_synthesis_job()` directly; those methods remain independently callable.

The hand-labeled judge evaluation set belongs in
`tests/eval/extraction_eval_set.jsonl`; its format and required case coverage
are documented beside the empty starter file.

## Production-shaped demo run

After Postgres, Qdrant Cloud, and the OpenAI-compatible endpoint configured by
`LLM_BASE_URL` are running, execute:

```powershell
python scripts/run_demo_pipeline.py
```

The runner creates six isolated L0 conversation turns, advances the clock past
the inactivity threshold, calls the real extraction judge for L1, then calls the
real synthesis judge for L2. It reports counts for project, agent, and user
categories from Postgres. It never inserts an L2 row directly. The default scope
is unique per run; pass `--scope demo-name` when you want to inspect a stable
scope or rerun a controlled fixture.
