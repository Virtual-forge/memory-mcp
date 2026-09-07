# Extraction evaluation set

Populate `extraction_eval_set.jsonl` with hand-labeled real transcript batches
before treating the extraction judge as production-quality. Each JSONL record
should contain:

```json
{"name":"case-name","scope":"demo","new_turns":[{"id":"uuid","source":"user","content":"..."}],"expected_scenes":[{"scene_name":"...","atoms":[{"statement":"...","category":"fact","source_turn_ids":["uuid"]}]}]}
```

The repository starts with an empty file rather than shipping fabricated
few-shot examples as if they were measured evaluation data. Add real cases for
positive extraction, no-op conversation, attribution-sensitive suggestions,
entity aliasing, supersession, and substantive tool output.
