from importlib import import_module
from types import SimpleNamespace, UnionType
from typing import get_type_hints
from uuid import UUID

import pytest

pytest.importorskip("agno")

from agno.tools.user_feedback import AskUserOption, AskUserQuestion

from memory_manager.retrieval.recall import RecallAttemptBudget, recall_sources_for_attempt

agent_module = import_module("agents.test_agent")


def test_dispatcher_hides_raw_remember_and_exposes_statement_input():
    assert agent_module.memory_mcp.exclude_tools == [
        "remember",
        "recall",
        "search_candidates",
        "list_projects",
    ]
    assert [field.name for field in agent_module.save_memory.user_input_schema or []] == [
        "statement"
    ]
    assert "statement" in agent_module.save_memory.parameters["properties"]


def test_dispatcher_allows_slow_mcp_responses():
    assert (
        agent_module.memory_mcp.timeout_seconds
        == agent_module.settings.memory_mcp_timeout_seconds
    )
    assert (
        agent_module.memory_mcp.server_params.timeout.total_seconds()
        == agent_module.settings.memory_mcp_timeout_seconds
    )


def test_dispatcher_attaches_native_structured_user_feedback():
    feedback = agent_module.user_feedback

    assert feedback in agent_module.agent.tools
    assert feedback.name == "user_feedback_tools"
    assert "next action must be an" in feedback.instructions
    assert "list_projects" in feedback.instructions
    assert "structured choices" in feedback.instructions
    assert "ask_user" in feedback.functions
    ask_user = feedback.functions["ask_user"]
    ask_user.process_entrypoint()
    assert ask_user.parameters["required"] == ["questions"]
    assert ask_user.parameters["properties"]["questions"]["type"] == "array"


def test_dispatcher_exposes_user_input_control_flow():
    control_flow = agent_module.user_control_flow

    assert control_flow in agent_module.agent.tools
    assert control_flow.name == "user_control_flow_tools"
    assert "get_user_input" in control_flow.functions
    get_user_input = control_flow.functions["get_user_input"]
    get_user_input.process_entrypoint()
    assert get_user_input.parameters["required"] == ["user_input_fields"]
    assert get_user_input.parameters["properties"]["user_input_fields"]["type"] == "array"


def test_dispatcher_routes_other_options_to_recall_or_cancel():
    other_options = SimpleNamespace(
        tools=[
            SimpleNamespace(
                tool_name="ask_user",
                user_feedback_schema=[
                    SimpleNamespace(
                        header="Project",
                        selected_options=["Other options"],
                    )
                ],
            )
        ]
    )
    recall_selection = SimpleNamespace(
        tools=[
            SimpleNamespace(
                tool_name="ask_user",
                user_feedback_schema=[
                    SimpleNamespace(
                        header="Other options",
                        selected_options=["Recall memory"],
                    )
                ],
            )
        ]
    )
    cancelled = SimpleNamespace(
        tools=[
            SimpleNamespace(
                tool_name="ask_user",
                user_feedback_schema=[
                    SimpleNamespace(
                        header="Other options",
                        selected_options=["Cancel"],
                    )
                ],
            )
        ]
    )

    assert agent_module.user_control_flow in agent_module.agent.tools
    assert agent_module.agent._tools_for_run(other_options) == [
        agent_module.other_project_feedback
    ]
    routed_feedback = agent_module.other_project_feedback
    routed_feedback.process_entrypoint()
    options = routed_feedback.parameters["properties"]["questions"]["items"]["properties"][
        "options"
    ]["items"]["properties"]["label"]["enum"]
    assert options == ["Recall memory", "Cancel"]
    assert agent_module.project_recall not in agent_module.agent._tools_for_run(other_options)
    assert agent_module.agent._tools_for_run(recall_selection) == [agent_module.recall_memory]
    assert agent_module.project_recall not in agent_module.agent._tools_for_run(cancelled)
    assert agent_module.agent._tools_for_run(cancelled) == []


def test_dispatcher_does_not_route_free_form_continuation_input():
    continuation = SimpleNamespace(
        messages=[
            SimpleNamespace(role="user", content="Tell me about this project."),
            SimpleNamespace(role="user", content="Use the other recall tool."),
        ],
        tools=[],
    )

    assert agent_module.agent._tools_for_run(continuation) == [
        tool for tool in agent_module.agent.tools if tool is not agent_module.user_control_flow
    ]


def test_dispatcher_cancel_overrides_other_options_recall():
    cancelled = SimpleNamespace(
        tools=[
            SimpleNamespace(
                tool_name="ask_user",
                user_feedback_schema=[
                    SimpleNamespace(
                        header="Other options",
                        selected_options=["Cancel"],
                    )
                ],
            )
        ],
    )

    assert agent_module.agent._tools_for_run(cancelled) == []


@pytest.mark.asyncio
async def test_dispatcher_project_listing_appends_other_project_option(monkeypatch):
    calls = []

    class FakeSession:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "result": [
                        {
                            "id": "project-id",
                            "name": "Memory Manager",
                            "scope": "project:memory",
                            "aliases": [],
                            "root_path": None,
                        }
                    ]
                },
                content=[],
            )

    async def fake_get_session_for_run(*, run_context=None, agent=None, team=None):
        return FakeSession()

    monkeypatch.setattr(agent_module.memory_mcp, "get_session_for_run", fake_get_session_for_run)

    projects = await agent_module.list_projects.entrypoint(
        scope="project:memory",
        run_context=SimpleNamespace(run_id="project-list-run"),
    )

    assert calls == [("list_projects", {"scope": "project:memory"})]
    assert [project["name"] for project in projects] == ["Memory Manager", "Other options"]
    assert projects[-1]["selection_only"] is True


@pytest.mark.asyncio
async def test_dispatcher_project_listing_keeps_empty_result_empty(monkeypatch):
    class FakeSession:
        async def call_tool(self, name, arguments):
            return SimpleNamespace(
                isError=False,
                structuredContent={"result": []},
                content=[],
            )

    async def fake_get_session_for_run(*, run_context=None, agent=None, team=None):
        return FakeSession()

    monkeypatch.setattr(agent_module.memory_mcp, "get_session_for_run", fake_get_session_for_run)

    assert await agent_module.list_projects.entrypoint() == []


def test_project_feedback_does_not_add_options_it_did_not_receive():
    question = AskUserQuestion(
        header="Project",
        question="Which project should I use?",
        options=[AskUserOption(label="Memory Manager")],
    )

    agent_module.user_feedback.functions["ask_user"].entrypoint([question])

    assert [option.label for option in question.options] == ["Memory Manager"]


def test_project_feedback_does_not_modify_other_options_feedback():
    question = AskUserQuestion(
        header="Other options",
        question="What should I do next?",
        options=[
            AskUserOption(label="Recall memory"),
            AskUserOption(label="Cancel"),
        ],
    )

    agent_module.user_feedback.functions["ask_user"].entrypoint([question])

    assert [option.label for option in question.options] == ["Recall memory", "Cancel"]


def test_dispatcher_exposes_required_project_recall():
    project_recall = agent_module.project_recall
    project_recall.process_entrypoint()

    assert project_recall in agent_module.agent.tools
    assert "project_id" in project_recall.parameters["required"]
    assert "confirmed project" in project_recall.description


def test_dispatcher_hides_internal_recall_results_from_user_stream():
    assert agent_module.recall_memory.show_result is False
    assert agent_module.project_recall.show_result is False


def test_dispatcher_schema_survives_agno_tool_rebuild():
    rebuilt = agent_module.save_memory.model_copy(deep=True)
    rebuilt.process_entrypoint()

    assert [field.name for field in rebuilt.user_input_schema or []] == ["statement"]
    assert [field.to_dict() for field in rebuilt.user_input_schema or []] == [
        {"name": "statement", "field_type": "str", "description": None, "value": None}
    ]


def test_dispatcher_optional_annotations_are_not_pep604_unions():
    annotations = get_type_hints(agent_module._save_memory)

    assert all(
        not isinstance(annotations[name], UnionType)
        for name in ("scope", "entity", "predicate")
    )


def test_agent_os_uses_persistent_database_for_run_continuation():
    assert agent_module.agent_os.db is not None
    assert agent_module.agent.db is agent_module.agent_os.db
    assert agent_module.agent_os.db.db_url == agent_module.settings.database_url


@pytest.mark.asyncio
async def test_dispatcher_forwards_default_and_explicit_scopes(monkeypatch):
    calls = []

    class FakeSession:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(
                isError=False,
                structuredContent={"statement": arguments["statement"]},
                content=[],
            )

    async def fake_get_session_for_run(*, run_context=None, agent=None, team=None):
        return FakeSession()

    monkeypatch.setattr(agent_module.memory_mcp, "get_session_for_run", fake_get_session_for_run)

    await agent_module.save_memory.entrypoint(
        statement="  Keep this as a personal preference. ",
        category="preference",
        run_context=SimpleNamespace(run_id="run-1"),
    )
    await agent_module.save_memory.entrypoint(
        statement="Keep this in the shared project namespace.",
        category="decision",
        scope="project:memory",
        run_context=SimpleNamespace(run_id="run-2"),
    )

    assert calls == [
        (
            "remember",
            {
                "statement": "Keep this as a personal preference.",
                "category": "preference",
            },
        ),
        (
            "remember",
            {
                "statement": "Keep this in the shared project namespace.",
                "category": "decision",
                "scope": "project:memory",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_recall_dispatcher_stages_sources_and_stops_after_three_attempts(monkeypatch):
    calls = []

    class FakeSession:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(
                isError=False,
                structuredContent={"result": []},
                content=[],
            )

    async def fake_get_session_for_run(*, run_context=None, agent=None, team=None):
        return FakeSession()

    monkeypatch.setattr(agent_module, "recall_budget", RecallAttemptBudget())
    monkeypatch.setattr(agent_module.memory_mcp, "get_session_for_run", fake_get_session_for_run)
    context = SimpleNamespace(run_id="recall-run-1")

    first = await agent_module.recall_memory.entrypoint(
        query="first query",
        run_context=context,
    )
    second = await agent_module.recall_memory.entrypoint(
        query="refined query",
        run_context=context,
    )
    third = await agent_module.recall_memory.entrypoint(
        query="final query",
        run_context=context,
    )
    fourth = await agent_module.recall_memory.entrypoint(
        query="should not search",
        run_context=context,
    )

    assert [call[1]["sources"] for call in calls] == [
        recall_sources_for_attempt(1),
        recall_sources_for_attempt(2),
        recall_sources_for_attempt(3),
    ]
    assert [call[1]["all_scopes"] for call in calls] == [True, True, True]
    assert [first["source_level"], second["source_level"], third["source_level"]] == [
        "L2",
        "L2",
        "L1+L2",
    ]
    assert third["can_retry"] is False
    assert fourth["status"] == "exhausted"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_recall_dispatcher_honors_explicit_scope(monkeypatch):
    calls = []

    class FakeSession:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(
                isError=False,
                structuredContent={"result": []},
                content=[],
            )

    async def fake_get_session_for_run(*, run_context=None, agent=None, team=None):
        return FakeSession()

    monkeypatch.setattr(agent_module, "recall_budget", RecallAttemptBudget())
    monkeypatch.setattr(agent_module.memory_mcp, "get_session_for_run", fake_get_session_for_run)

    await agent_module.recall_memory.entrypoint(
        query="scoped query",
        scope="project:memory",
        run_context=SimpleNamespace(run_id="scoped-recall-run"),
    )

    assert calls[0][1]["scope"] == "project:memory"
    assert calls[0][1]["all_scopes"] is False


@pytest.mark.asyncio
async def test_recall_dispatcher_uses_original_request_as_query(monkeypatch):
    calls = []

    class FakeSession:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(
                isError=False,
                structuredContent={"result": []},
                content=[],
            )

    async def fake_get_session_for_run(*, run_context=None, agent=None, team=None):
        return FakeSession()

    monkeypatch.setattr(agent_module, "recall_budget", RecallAttemptBudget())
    monkeypatch.setattr(agent_module.memory_mcp, "get_session_for_run", fake_get_session_for_run)
    context = SimpleNamespace(
        run_id="continuation-query-run",
        messages=[
            SimpleNamespace(role="user", content="Original project question"),
            SimpleNamespace(role="user", content="Replacement memory question"),
        ],
    )

    await agent_module.recall_memory.entrypoint(
        query="Original project question",
        run_context=context,
    )

    assert calls[0][1]["query"] == "Original project question"


@pytest.mark.asyncio
async def test_project_recall_uses_confirmed_id_and_shared_budget(monkeypatch):
    calls = []
    project_id = UUID("25305faf-6315-40c6-b4fd-7a1cb89ccdbb")

    class FakeSession:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(
                isError=False,
                structuredContent={"result": []},
                content=[],
            )

    async def fake_get_session_for_run(*, run_context=None, agent=None, team=None):
        return FakeSession()

    monkeypatch.setattr(agent_module, "recall_budget", RecallAttemptBudget())
    monkeypatch.setattr(agent_module.memory_mcp, "get_session_for_run", fake_get_session_for_run)
    context = SimpleNamespace(run_id="shared-recall-run")

    first = await agent_module.project_recall.entrypoint(
        query="approval workflow",
        project_id=str(project_id),
        run_context=context,
    )
    second = await agent_module.recall_memory.entrypoint(
        query="approval workflow details",
        run_context=context,
    )
    third = await agent_module.project_recall.entrypoint(
        query="approval workflow exact steps",
        project_id=str(project_id),
        run_context=context,
    )

    assert [call[0] for call in calls] == ["recall", "recall", "recall"]
    assert calls[0][1]["project_id"] == str(project_id)
    assert calls[0][1]["all_scopes"] is True
    assert calls[0][1]["sources"] == recall_sources_for_attempt(1, project_id)
    assert calls[1][1]["sources"] == recall_sources_for_attempt(2)
    assert calls[2][1]["project_id"] == str(project_id)
    assert calls[2][1]["sources"] == recall_sources_for_attempt(3, project_id)
    assert first["attempt"] == 1
    assert second["attempt"] == 2
    assert third["attempt"] == 3
