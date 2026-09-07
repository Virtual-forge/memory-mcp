import json
import os
from copy import copy
from datetime import timedelta
from typing import Any, Literal, Optional
from uuid import UUID

from agno.agent import Agent
from agno.db.postgres import PostgresDb
from agno.models.openai import OpenAIChat
from agno.os import AgentOS
from agno.tools.function import Function
from agno.tools.mcp import MCPTools, StreamableHTTPClientParams

from agno.tools.user_feedback import UserFeedbackTools
from dotenv import load_dotenv

from memory_manager.config import Settings
from memory_manager.retrieval.recall import (
    MAX_RECALL_ATTEMPTS,
    RecallAttemptBudget,
    recall_sources_for_attempt,
)
from memory_manager.runtime import build_memory_capture

load_dotenv()

settings = Settings()
memory_capture = build_memory_capture(settings)

memory_mcp = MCPTools(
    name="memory-manager",
    transport="streamable-http",
    server_params=StreamableHTTPClientParams(
        url=os.getenv(
            "MEMORY_MCP_URL",
            "http://localhost:8000/mcp",
        ),
        timeout=timedelta(seconds=settings.memory_mcp_timeout_seconds),
    ),
    timeout_seconds=settings.memory_mcp_timeout_seconds,
    exclude_tools=["remember", "recall", "search_candidates", "list_projects"],
)


user_feedback = UserFeedbackTools(
    instructions="""
Use ask_user for structured choices, not a free-form assistant question.
Follow this project-selection protocol exactly. When list_projects returns one or more
projects, the next action must be an ask_user tool call before any recall or answer. The
result contains one record per registered project followed by a final synthetic record
named "Other options". Create one question with header "Project", question "Which
project should I use?", and one option for every returned record in that exact order,
including the final "Other options" option. Copy each real project name into the option
label and put its scope or root path in the description when useful. Do not invent
projects or UUIDs.

After the first question returns "Other options", the only next tool call allowed is a
second ask_user call with header "Other options", question "What should I do next?", and
exactly two options: "Recall memory" and "Cancel". Do not call recall, project_recall,
or answer in prose between those two ask_user calls. If the second question returns
"Cancel", report that project recall was cancelled. Only if it returns "Recall memory"
may you call recall using the original request as the query. Do not call project_recall
for this branch. If the user selects "Cancel" at any feedback step, do not call recall
or project_recall.

If list_projects fails because more than three projects are registered, ask the user for
an exact scope and retry; never silently drop projects or invent a UUID.
""".strip()
)




def _project_feedback_phase(run_response: Any) -> str | None:
    phase = None
    for tool_execution in getattr(run_response, "tools", None) or []:
        if getattr(tool_execution, "tool_name", None) != "ask_user":
            continue
        for question in getattr(tool_execution, "user_feedback_schema", None) or []:
            selected_options = getattr(question, "selected_options", None) or []
            if "Cancel" in selected_options:
                return "cancelled"
            if (
                getattr(question, "header", None) == "Other options"
                and "Recall memory" in selected_options
            ):
                return "generic_recall"
            if (
                getattr(question, "header", None) == "Project"
                and "Other options" in selected_options
            ):
                phase = "other_pending"
    return phase


def _original_user_message(source: Any) -> str | None:
    for message in getattr(source, "messages", None) or []:
        if getattr(message, "role", None) != "user":
            continue
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def _effective_recall_query(query: str, run_context: Any) -> str:
    return _original_user_message(run_context) or query


class _EditableMemoryFunction(Function):
    def process_entrypoint(self, strict: bool = False) -> None:
        super().process_entrypoint(strict=strict)
        self.parameters = {
            **self.parameters,
            "properties": {
                **self.parameters.get("properties", {}),
                "statement": {"type": "string"},
            },
            "required": [
                "statement",
                *[
                    name
                    for name in self.parameters.get("required", [])
                    if name != "statement"
                ],
            ],
        }
        self.user_input_schema = [
            field
            for field in self.user_input_schema or []
            if field.name in self.user_input_fields
        ]


def _decode_mcp_result(result: Any) -> dict[str, object]:
    if getattr(result, "isError", False):
        raise RuntimeError(f"memory MCP remember failed: {result.content}")

    structured_content = getattr(result, "structuredContent", None)
    if isinstance(structured_content, dict):
        return structured_content

    for content_item in getattr(result, "content", []):
        text = getattr(content_item, "text", None)
        if not text:
            continue
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
        return {"result": decoded}

    return {"result": str(result)}


OTHER_PROJECT_OPTION = {
    "name": "Other options",
    "scope": None,
    "aliases": [],
    "root_path": None,
    "selection_only": True,
    "description": "Choose this to use general memory recall.",
}

OTHER_PROJECT_FEEDBACK_QUESTION = {
    "header": "Other options",
    "question": "What should I do next?",
    "options": [
        {"label": "Recall memory", "description": "Search general memory."},
        {"label": "Cancel", "description": "Cancel project recall."},
    ],
}


class _OtherProjectFeedbackFunction(Function):
    def process_entrypoint(self, strict: bool = False) -> None:
        super().process_entrypoint(strict=strict)
        self.parameters = {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 1,
                    "description": (
                        "Use exactly the supplied Other options question and its two "
                        "options: Recall memory and Cancel."
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "header": {
                                "type": "string",
                                "enum": [OTHER_PROJECT_FEEDBACK_QUESTION["header"]],
                            },
                            "question": {
                                "type": "string",
                                "enum": [OTHER_PROJECT_FEEDBACK_QUESTION["question"]],
                            },
                            "options": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": 2,
                                "description": "Use exactly Recall memory and Cancel options.",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "enum": ["Recall memory", "Cancel"],
                                        },
                                        "description": {"type": "string"},
                                    },
                                    "required": ["label"],
                                },
                            },
                        },
                        "required": ["header", "question", "options"],
                    },
                }
            },
            "required": ["questions"],
            "additionalProperties": False,
        }


def _ask_other_project(questions: list[dict[str, object]]) -> str:
    return "User feedback received"


other_project_feedback = _OtherProjectFeedbackFunction(
    name="ask_user",
    description=(
        "Ask exactly one fixed Other options question with exactly two options: Recall "
        "memory and Cancel. Do not change the header, question, option labels, or add options."
    ),
    entrypoint=_ask_other_project,
    show_result=False,
)
other_project_feedback.process_entrypoint()


async def _list_projects(
    scope: str | None = None,
    run_context: Any = None,
) -> list[dict[str, object]]:
    arguments: dict[str, object] = {}
    if scope is not None:
        arguments["scope"] = scope

    session = await memory_mcp.get_session_for_run(run_context=run_context)
    result = await session.call_tool("list_projects", arguments)
    decoded = _decode_mcp_result(result)
    projects = decoded.get("result", decoded.get("projects"))
    if not isinstance(projects, list) or not all(isinstance(project, dict) for project in projects):
        raise RuntimeError("memory MCP list_projects returned an invalid result shape")
    if not projects:
        return []
    return [dict(project) for project in projects] + [OTHER_PROJECT_OPTION.copy()]


list_projects = Function(
    name="list_projects",
    description=(
        "List registered project identities for project selection. With no scope, search "
        "all project scopes. The result contains every real project followed by a final "
        "selection-only record named Other options. Copy that final record into the first "
        "Project ask_user question; it is not a project and must never be passed as a "
        "project UUID."
    ),
    entrypoint=_list_projects,
    show_result=False,
)


def _recall_run_id(run_context: Any) -> str:
    for name in ("run_id", "session_id"):
        value = getattr(run_context, name, None)
        if value:
            return str(value)
    return "agent-run-without-id"


recall_budget = RecallAttemptBudget()


async def _recall_memory(
    query: str,
    mode: Literal["vector", "bm25", "hybrid"] | None = None,
    scope: str | None = None,
    top_k: int = 8,
    min_score: float | None = None,
    project_id: str | None = None,
    run_context: Any = None,
) -> dict[str, object]:
    attempt = recall_budget.consume(_recall_run_id(run_context))
    if attempt is None:
        return {
            "status": "exhausted",
            "attempt": MAX_RECALL_ATTEMPTS,
            "source_level": "L1+L2",
            "results": [],
            "can_retry": False,
            "message": (
                "The three memory lookup attempts for this user request are exhausted. "
                "Do not call recall again; answer from the available evidence or state "
                "that the memory does not establish the answer."
            ),
        }

    project_uuid = UUID(project_id) if project_id else None
    sources = recall_sources_for_attempt(attempt, project_uuid)
    arguments: dict[str, object] = {
        "query": _effective_recall_query(query, run_context),
        "sources": sources,
        "top_k": top_k,
        "all_scopes": scope is None,
    }
    if project_id is not None:
        arguments["project_id"] = project_id
    if mode is not None:
        arguments["mode"] = mode
    if scope is not None:
        arguments["scope"] = scope
    if min_score is not None:
        arguments["min_score"] = min_score

    session = await memory_mcp.get_session_for_run(run_context=run_context)
    result = await session.call_tool("recall", arguments)
    decoded = _decode_mcp_result(result)
    results = decoded.get("result", decoded.get("results"))
    if not isinstance(results, list):
        raise RuntimeError("memory MCP recall returned an invalid result shape")
    return {
        "status": "ok",
        "attempt": attempt,
        "source_level": "L2" if attempt < MAX_RECALL_ATTEMPTS else "L1+L2",
        "results": results,
        "can_retry": attempt < MAX_RECALL_ATTEMPTS,
    }


async def _project_recall_memory(
    query: str,
    project_id: str,
    mode: Literal["vector", "bm25", "hybrid"] | None = None,
    scope: str | None = None,
    top_k: int = 8,
    min_score: float | None = None,
    run_context: Any = None,
) -> dict[str, object]:
    return await _recall_memory(
        query=query,
        mode=mode,
        scope=scope,
        top_k=top_k,
        min_score=min_score,
        project_id=project_id,
        run_context=run_context,
    )


recall_memory = Function(
    name="recall",
    description=(
        "Search general or unscoped durable memory with a hard three-attempt budget per "
        "user request. Do not call this for a request that names or implies a specific "
        "project, repository, application, or extension; list and confirm the project "
        "then use project_recall instead. "
        "The first two calls search L2 only; the third and final call searches L1 atoms "
        "and L2. The result reports the attempt and whether another recall is allowed. "
        "When scope is omitted, this searches all scopes; an explicit scope narrows the "
        "search. "
        "Do not call recall after it reports exhausted."
    ),
    entrypoint=_recall_memory,
    show_result=False,
)

project_recall = Function(
    name="project_recall",
    description=(
        "Search durable memory for one confirmed project. project_id is the UUID returned "
        "by list_projects after the user confirms the project identity; never invent it. "
        "This uses the same hard three-attempt budget as recall: the first two calls search "
        "project L2 only, and the third and final call searches project atoms and L2. "
        "When scope is omitted, search all scopes while still filtering every result by "
        "project_id. Do not call this until project identity is confirmed."
    ),
    entrypoint=_project_recall_memory,
    show_result=False,
)


async def _save_memory(
    statement: str,
    category: Literal["fact", "preference", "decision", "event"] = "fact",
    scope: Optional[str] = None,  # noqa: UP045 - Agno mishandles PEP 604 unions in HITL schemas.
    entity: Optional[str] = None,  # noqa: UP045 - Agno mishandles PEP 604 unions in HITL schemas.
    predicate: Optional[str] = None,  # noqa: UP045 - Agno mishandles PEP 604 unions in HITL schemas.
    run_context: Any = None,
) -> dict[str, object]:
    if not statement.strip():
        raise ValueError("statement must not be empty")

    arguments: dict[str, object] = {
        "statement": statement.strip(),
        "category": category,
    }
    for name, value in (
        ("scope", scope),
        ("entity", entity),
        ("predicate", predicate),
    ):
        if value is not None:
            arguments[name] = value

    session = await memory_mcp.get_session_for_run(run_context=run_context)
    result = await session.call_tool("remember", arguments)
    return _decode_mcp_result(result)


save_memory = _EditableMemoryFunction(
    name="save_memory",
    description=(
        "Save an explicitly requested memory. Call this only for direct save, remember, "
        "store, or note requests."
    ),
    entrypoint=_save_memory,
    requires_user_input=True,
    user_input_fields=["statement"],
    show_result=True,
)
save_memory.process_entrypoint()


class _ProjectRoutingAgent(Agent):
    def _tools_for_run(self, run_response: Any) -> list[Any]:
        phase = _project_feedback_phase(run_response)
        if phase == "cancelled":
            return []
        if phase == "generic_recall":
            return [recall_memory]
        if phase == "other_pending":
            return [other_project_feedback]
        return [tool for tool in self.tools ]

    def get_tools(
        self,
        run_response: Any,
        run_context: Any,
        session: Any,
        user_id: str | None = None,
    ):
        routed_agent = copy(self)
        routed_agent.tools = self._tools_for_run(run_response)
        return Agent.get_tools(
            routed_agent,
            run_response=run_response,
            run_context=run_context,
            session=session,
            user_id=user_id,
        )

    async def aget_tools(
        self,
        run_response: Any,
        run_context: Any,
        session: Any,
        user_id: str | None = None,
        check_mcp_tools: bool = True,
    ):
        routed_agent = copy(self)
        routed_agent.tools = self._tools_for_run(run_response)
        return await Agent.aget_tools(
            routed_agent,
            run_response=run_response,
            run_context=run_context,
            session=session,
            user_id=user_id,
            check_mcp_tools=check_mcp_tools,
        )


agent = _ProjectRoutingAgent(
    name="Workbench",
    tools=[
        memory_mcp,
        list_projects,
        user_feedback,
        
        recall_memory,
        project_recall,
        save_memory,
    ],
    model=OpenAIChat(
        id="gpt-5.6-luna",
        api_key=os.environ["LLM_API_KEY"],
        base_url=os.environ["LLM_BASE_URL"],
    ),
    instructions="""
You have access to the MCP memory tool and should use it to recall prior conversations
and saved user information.
- ROUTING PRIORITY: First classify the latest user request. If it names or implies a
    specific project, repository, application, or extension, list that project before
    any generic recall. This includes wording such as "in my project" together with a
    project hint. Never call recall without project_id first for such a request.
- For a project query, call list_projects with no scope unless the user explicitly
    supplied one. The very next action MUST be ask_user with one structured option for
    every returned project; do not ask for a project name or confirmation in ordinary
    prose. For example, "tell me about approval in this project" must list all projects,
    show them through ask_user, then call project_recall with the UUID mapped from the
    selected option. The first selector's final Other options option must route to a second
    ask_user question with exactly Recall memory and Cancel options. Cancel at either feedback
    step must stop the project lookup without calling project_recall. If no projects are
    registered, report that instead of guessing. If more than three projects are registered,
    narrow with an exact scope and retry rather than truncating the options.
- Scope is an optional narrowing filter, not something to guess. If the user or runtime
    explicitly gives you a scope, pass that exact scope. Otherwise recall searches all
    scopes automatically. For search_candidates, fetch, or drill_down, use
    all_scopes=true when no reliable scope is known so relevant memories are not hidden
    by an unknown namespace.
- Never invent scope strings. Treat the scope returned with a memory as the exact scope
    for a later targeted fetch, update, forget, or drill-down operation.
- For save_memory, omit scope for the configured user unless the user explicitly says
    the memory belongs to a shared project or another namespace.
- HARD RULE: call save_memory only when the latest user message directly asks you to save
    something, using language such as "remember this", "save this", "store this", or
    "note this for later". A statement like "I prefer...", "I decided...", or "I use..."
    is context for the automatic L0 -> L1 -> L2 pipeline, not permission to call remember.
- If save intent is ambiguous, do not call remember. The tool also requires user
    input so the user can edit the statement before it is stored.
- After save_memory succeeds, simply say that the memory has been saved to atoms. Do not
    mention the confirmation, review, editing, or user-input step.
- Do not save information unless the user explicitly asks you to remember it.
- Before responding, check memory for previously saved details that are relevant to the
    current conversation. For a project query, complete project resolution and
    confirmation above, then use project_recall. Never spend a generic recall attempt
    before that confirmation. For other requests, call recall normally. Both recall tools
    share one hard budget of at most three calls per user request: the first call searches
    L2 only, the second call also searches L2 only, and the third and final call searches
    L1 atoms together with L2. Use the second call only when the first result is empty or
    does not address the question, and use the third call only when the second result is
    still empty or inadequate.
- Never call search_candidates as an alternate way to search memory. If recall reports
    that its budget is exhausted, do not call recall again; answer from the evidence
    already returned or state that memory does not establish the answer.
- Treat recalled records as evidence, not as permission to invent missing details.
- If the user explicitly asks for more detail, exact wording, evidence, rationale, or
    history about a recalled record, call drill_down with that record's exact id and
    source_table. For an L2 record this follows its links to L0; for an L1 atom it
    follows its direct provenance. Do not use drill_down to search for unrelated
    records, and do not call it proactively when a summary answers the question.
- For a follow-up that asks for details about an already returned record, use drill_down
    directly rather than spending a new recall attempt.
- For project documentation, do not use generic recall, search_candidates, fetch, or
    drill_down without the confirmed project_id after a real project is selected. Use
    project_recall after the structured selection, passing the original user request as
    the query. The only exception is Other options -> Recall memory: use recall with the
    original user request as the query and do not call project_recall. The selected
    project ID is authoritative for project_recall. The Other options branch never selects
    a project and must use its dedicated Recall memory or Cancel question.
- Project confirmation is separate from recall and does not consume a recall attempt.
- Use memory to recall earlier facts, preferences, or tasks when the user references them.
""",
    pre_hooks=[memory_capture.pre_hook],
    post_hooks=[memory_capture.post_hook],
)


agno_db = PostgresDb(db_url=settings.database_url)
agent_os = AgentOS(agents=[agent], db=agno_db, tracing=True)
app = agent_os.get_app()


def main() -> None:
    app_path = "agents.test_agent:app" if __package__ else "test_agent:app"
    agent_os.serve(app=app_path, reload=False, port=7780)


if __name__ == "__main__":
    main()