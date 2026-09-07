from types import SimpleNamespace
from uuid import uuid4

from memory_manager.agno_capture import AgnoMemoryCapture


class RecordingPipeline:
    def __init__(self):
        self.turns = []

    def ingest_turn(self, turn):
        self.turns.append(turn)
        return None


def test_agno_hooks_capture_user_tools_and_final_response():
    pipeline = RecordingPipeline()
    capture = AgnoMemoryCapture(pipeline, scope="default")
    session_id = uuid4()
    context = SimpleNamespace(
        session_id=str(session_id),
        run_id="run-123",
        metadata={"memory_scope": "workspace"},
    )
    capture.pre_hook(
        run_input=SimpleNamespace(input_content="Remember the retrieval decision."),
        run_context=context,
    )
    capture.post_hook(
        run_output=SimpleNamespace(
            run_id="run-123",
            session_id=str(session_id),
            content="The decision is recorded.",
            messages=[
                SimpleNamespace(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        {
                            "id": "call-1",
                            "function": {
                                "name": "remember",
                                "arguments": {"content": "The retrieval decision."},
                            },
                        }
                    ],
                ),
                SimpleNamespace(
                    role="tool",
                    content={"ok": True},
                    tool_name="remember",
                    tool_call_id="call-1",
                ),
                SimpleNamespace(
                    role="assistant",
                    content="The decision is recorded.",
                    tool_calls=[],
                ),
            ],
        ),
        run_context=context,
    )

    assert [turn.source for turn in pipeline.turns] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert pipeline.turns[0].scope == "workspace"
    assert pipeline.turns[1].tool_call_id == "call-1"
    assert '"content": "The retrieval decision."' in pipeline.turns[1].content
    assert pipeline.turns[2].tool_name == "remember"
    assert pipeline.turns[0].source_event_id == "agno:run-123:user"
    assert pipeline.turns[3].source_event_id == "agno:run-123:message:2:content"
