# Database

`schema.sql` is the relational source of truth. It is deliberately plain SQL so
it can be applied by `Database.apply_schema()` or a normal Postgres migration
runner without introducing an ORM dependency.

`turns.source_event_id` supports idempotent capture from agent hooks. The
`extraction_attempts` cursor records the newest turn included in an extraction
attempt independently from `extraction_coverage`, allowing turns that yielded
no atom to remain available for later corroboration.
