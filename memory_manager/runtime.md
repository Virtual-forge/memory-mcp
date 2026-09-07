# Runtime assembly

Use `build_memory_tools()` for the MCP server. Use
`build_memory_pipeline()` in a worker or scheduler that has an LLM key; it
shares the same Postgres, Qdrant, embedding, and indexing boundaries while
adding the extraction and synthesis judges.

Use `build_memory_capture()` in an AgentOS process. It creates only the
Postgres-backed L0 writer, so automatic Agno capture does not initialize
Qdrant or an LLM client. Run `memory-worker` separately for L1 extraction and
L2 synthesis.
