"""Pure scheduling rules and Postgres state for L2 synthesis."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from memory_manager.db.connection import Database


@dataclass(frozen=True)
class SynthesisTriggerDecision:
    inactivity_due: bool
    volume_due: bool

    @property
    def due(self) -> bool:
        return self.inactivity_due or self.volume_due


@dataclass(frozen=True)
class SynthesisTriggerState:
    last_pending_atom_at: datetime | None
    pending_count: int


def synthesis_inactivity_is_due(
    last_pending_atom_at: datetime | None,
    now: datetime,
    inactivity_minutes: int,
) -> bool:
    if last_pending_atom_at is None:
        return False
    if last_pending_atom_at.tzinfo is None:
        last_pending_atom_at = last_pending_atom_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now - last_pending_atom_at >= timedelta(minutes=inactivity_minutes)


def evaluate_synthesis_triggers(
    last_pending_atom_at: datetime | None,
    pending_atom_count: int,
    now: datetime,
    inactivity_minutes: int,
    volume_cap: int,
) -> SynthesisTriggerDecision:
    return SynthesisTriggerDecision(
        inactivity_due=synthesis_inactivity_is_due(
            last_pending_atom_at,
            now,
            inactivity_minutes,
        )
        and pending_atom_count > 0,
        volume_due=pending_atom_count >= volume_cap,
    )


def load_synthesis_trigger_state(database: Database, scope: str) -> SynthesisTriggerState:
    row = database.fetch_one(
        """
        select count(*) as pending_count, max(a.created_at) as last_pending_atom_at
        from atoms as a
        where a.scope = %s
          and not a.source_deleted
          and not exists (
              select 1 from synthesis_coverage as sc where sc.atom_id = a.id
          )
        """,
        (scope,),
    )
    return SynthesisTriggerState(
        last_pending_atom_at=row["last_pending_atom_at"] if row else None,
        pending_count=int(row["pending_count"]) if row else 0,
    )
