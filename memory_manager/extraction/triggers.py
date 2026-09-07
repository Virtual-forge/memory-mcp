"""Pure trigger rules for inactivity and volume-cap extraction flushes."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from memory_manager.db.connection import Database


@dataclass(frozen=True)
class TriggerDecision:
    inactivity_due: bool
    volume_due: bool

    @property
    def due(self) -> bool:
        return self.inactivity_due or self.volume_due


@dataclass(frozen=True)
class SessionTriggerState:
    last_turn_at: datetime | None
    pending_count: int
    has_new_turns: bool


def inactivity_is_due(
    last_turn_at: datetime | None,
    now: datetime,
    inactivity_minutes: int,
) -> bool:
    """Return true only after inactivity since the most recent turn."""
    if last_turn_at is None:
        return False
    if last_turn_at.tzinfo is None:
        last_turn_at = last_turn_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now - last_turn_at >= timedelta(minutes=inactivity_minutes)


def volume_cap_is_due(unprocessed_turn_count: int, volume_cap: int) -> bool:
    """Return true when a session has reached its independent turn cap."""
    return unprocessed_turn_count >= volume_cap


def evaluate_triggers(
    last_turn_at: datetime | None,
    unprocessed_turn_count: int,
    now: datetime,
    inactivity_minutes: int,
    volume_cap: int,
    has_new_turns: bool = True,
) -> TriggerDecision:
    return TriggerDecision(
        inactivity_due=has_new_turns
        and inactivity_is_due(last_turn_at, now, inactivity_minutes)
        and unprocessed_turn_count > 0,
        volume_due=has_new_turns and volume_cap_is_due(unprocessed_turn_count, volume_cap),
    )


def load_session_trigger_state(
    database: Database,
    session_id: UUID,
    scope: str,
) -> SessionTriggerState:
    """Read pending turns and whether new evidence arrived since the last attempt."""
    last_turn_query = """
        select max(created_at) as last_turn_at
        from turns
        where session_id = %s and scope = %s
    """
    pending_query = """
        select count(*) as pending_count
        from turns as t
        where t.session_id = %s
          and t.scope = %s
          and not exists (
              select 1 from extraction_coverage as ec where ec.turn_id = t.id
          )
          and not exists (
              select 1 from atom_sources as source where source.turn_id = t.id
          )
    """
    last_turn = database.fetch_one(last_turn_query, (session_id, scope))
    pending = database.fetch_one(pending_query, (session_id, scope))
    new_turn_query = """
        select exists (
            select 1
            from turns as t
            left join extraction_attempts as ea
              on ea.session_id = t.session_id and ea.scope = t.scope
            where t.session_id = %s
              and t.scope = %s
              and not exists (
                  select 1 from extraction_coverage as ec where ec.turn_id = t.id
              )
              and not exists (
                  select 1 from atom_sources as source where source.turn_id = t.id
              )
              and (
                  ea.attempted_through_turn_id is null
                  or t.created_at > ea.attempted_through_created_at
                  or (
                      t.created_at = ea.attempted_through_created_at
                      and t.id > ea.attempted_through_turn_id
                  )
              )
        ) as has_new_turns
    """
    new_turns = database.fetch_one(new_turn_query, (session_id, scope))
    return SessionTriggerState(
        last_turn_at=last_turn["last_turn_at"] if last_turn else None,
        pending_count=int(pending["pending_count"]) if pending else 0,
        has_new_turns=bool(new_turns and new_turns["has_new_turns"]),
    )
