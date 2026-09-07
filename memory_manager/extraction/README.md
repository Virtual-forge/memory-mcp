# Extraction

Turn ingestion writes raw L0 rows synchronously. Extraction is a separate job:
`extract_pending_turns()` loads an uncovered batch, calls the runtime prompt,
validates provenance and confidence, resolves entities, writes L1 atoms, and
marks every turn covered. The coverage ledger handles empty scenes as well as
normal atom provenance, so overlapping inactivity and volume triggers remain
idempotent.
