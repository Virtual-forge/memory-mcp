# Synthesis

`SynthesisJudge` loads `prompts/l2_synthesis.md` and validates category,
provenance, and existing-row IDs. `upsert.py` owns the SQL identifiers and
category-specific writes. A synthesis coverage ledger makes the atom batch
idempotent while allowing one atom to link to multiple L2 categories.
