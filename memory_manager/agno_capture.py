"""Capture Agno run events as L0 turns without running extraction inline."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from memory_manager.ingestion.turns import TurnWriter
from memory_manager.models.turn import TurnInput, TurnSource


@dataclass(frozen=True)
class CapturedEvent:
    source: TurnSource
    content: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    source_event_id: str | None = None


class TurnIngestor(Protocol):
    def ingest_turn(self, turn: TurnInput) -> object:
        ...


class L0Pipeline:
    """Minimal ingestion facade used by the agent process."""

    def __init__(self, turn_writer: TurnWriter) -> None:
        self.turn_writer = turn_writer

    def ingest_turn(self, turn: TurnInput) -> UUID:
        return self.turn_writer.write_turn(turn)


class AgnoMemoryCapture:
    """Adapt Agno pre/post hooks to the synchronous L0 ingestion boundary."""

    def __init__(self, pipeline: TurnIngestor, scope: str) -> None:
        self.pipeline = pipeline
        self.scope = scope

    def pre_hook(self, *, run_input: Any, run_context: Any, **_: Any) -> None:
        content = serialize_content(getattr(run_input, "input_content", None))
        if not content:
            return
        self.record(
            session_id=session_id_from(run_context),
            scope=scope_from(run_context, self.scope),
            source="user",
            content=content,
            source_event_id=event_id(run_id_from(run_context), "user"),
        )

    def post_hook(self, *, run_output: Any, run_context: Any, **_: Any) -> None:
        run_id = run_id_from(run_output, run_context)
        events = list(iter_run_events(run_output, run_id=run_id))
        if not events:
            content = serialize_content(getattr(run_output, "content", None))
            if content:
                events = [
                    CapturedEvent(
                        source="assistant",
                        content=content,
                        source_event_id=event_id(run_id, "assistant"),
                    )
                ]
        self.record_events(
            session_id=session_id_from(run_output, run_context),
            scope=scope_from(run_context, self.scope),
            events=events,
        )

    def record_events(
        self,
        session_id: UUID,
        scope: str,
        events: Iterable[CapturedEvent],
    ) -> None:
        for captured in events:
            self.record(
                session_id=session_id,
                scope=scope,
                source=captured.source,
                content=captured.content,
                tool_name=captured.tool_name,
                tool_call_id=captured.tool_call_id,
                source_event_id=captured.source_event_id,
            )

    def record(
        self,
        *,
        session_id: UUID,
        scope: str,
        source: TurnSource,
        content: str,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        source_event_id: str | None = None,
    ) -> None:
        self.pipeline.ingest_turn(
            TurnInput(
                session_id=session_id,
                scope=scope,
                source=source,
                content=content,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                source_event_id=source_event_id,
            )
        )


def iter_run_events(
    run_output: Any,
    run_id: str | None = None,
) -> Iterable[CapturedEvent]:
    """Extract current-run assistant/tool messages from an Agno RunOutput."""
    saw_assistant = False
    for message_index, message in enumerate(getattr(run_output, "messages", None) or []):
        role = str(getattr(message, "role", ""))
        if role == "assistant":
            tool_calls = getattr(message, "tool_calls", None) or []
            for tool_index, tool_call in enumerate(tool_calls):
                tool_name, tool_call_id, tool_args = tool_call_fields(tool_call)
                yield CapturedEvent(
                    source="assistant",
                    content=serialize_content(tool_args) or "{}",
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    source_event_id=event_id(run_id, f"message:{message_index}:tool:{tool_index}"),
                )
            content = serialize_content(getattr(message, "content", None))
            if content:
                saw_assistant = True
                yield CapturedEvent(
                    source="assistant",
                    content=content,
                    source_event_id=event_id(run_id, f"message:{message_index}:content"),
                )
        elif role == "tool":
            content = serialize_content(getattr(message, "content", None))
            if content:
                call_id = optional_text(getattr(message, "tool_call_id", None))
                if call_id is None:
                    call_id = event_id(run_id, f"message:{message_index}:tool-result") or (
                        f"agno-message-{message_index}-tool-result"
                    )
                yield CapturedEvent(
                    source="tool",
                    content=content,
                    tool_name=optional_text(getattr(message, "tool_name", None)),
                    tool_call_id=call_id,
                    source_event_id=event_id(run_id, f"message:{message_index}:tool-result"),
                )
    if not saw_assistant:
        content = serialize_content(getattr(run_output, "content", None))
        if content:
            yield CapturedEvent(
                source="assistant",
                content=content,
                source_event_id=event_id(run_id, "assistant"),
            )


def tool_call_fields(tool_call: Any) -> tuple[str | None, str | None, Any]:
    if isinstance(tool_call, dict):
        function = tool_call.get("function") or {}
        return (
            optional_text(tool_call.get("tool_name") or function.get("name")),
            optional_text(tool_call.get("tool_call_id") or tool_call.get("id")),
            tool_call.get("tool_args", tool_call.get("arguments", function.get("arguments"))),
        )
    return (
        optional_text(getattr(tool_call, "tool_name", None)),
        optional_text(getattr(tool_call, "tool_call_id", None)),
        getattr(tool_call, "tool_args", None),
    )


def session_id_from(*values: Any) -> UUID:
    for value in values:
        candidate = getattr(value, "session_id", None)
        if candidate is None:
            continue
        try:
            return UUID(str(candidate))
        except ValueError:
            return uuid5(NAMESPACE_URL, f"agno-session:{candidate}")
    raise ValueError("Agno run did not provide a session_id")


def run_id_from(*values: Any) -> str | None:
    for value in values:
        candidate = getattr(value, "run_id", None)
        if candidate is not None:
            return str(candidate)
    return None


def event_id(run_id: str | None, suffix: str) -> str | None:
    return f"agno:{run_id}:{suffix}" if run_id else None


def scope_from(run_context: Any, default_scope: str) -> str:
    metadata = getattr(run_context, "metadata", None) or {}
    scope = metadata.get("memory_scope") if isinstance(metadata, dict) else None
    return str(scope or default_scope)


def optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def serialize_content(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    try:
        text = json.dumps(value, ensure_ascii=True, default=str, sort_keys=True)
    except (TypeError, ValueError):
        text = str(value)
    return text.strip() or None
