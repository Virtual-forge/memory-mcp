"""LLM-backed recovery planning shared by workflow graphs.

The planner proposes a bounded recovery action from structured failure data.
It never executes a tool or chooses an unchecked graph destination; callers
validate its plan before routing or performing side effects.
"""

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

import llm


RecoveryAction = Literal[
    "retry_same",
    "retry_with_correction",
    "refresh_and_retry",
    "ask_user",
    "stop",
]


class RecoveryDecision(BaseModel):
    action: RecoveryAction = Field(
        description="Bounded recovery action. Ask the user when missing information or permission blocks safe recovery."
    )
    resume_node: Optional[str] = Field(
        default=None,
        description="Failed node or necessary upstream node to resume, or null when stopping or asking the user.",
    )
    tool_name: Optional[str] = Field(
        default=None,
        description="One tool name from the supplied catalog, or null when no tool is needed.",
    )
    rectification: Optional[str] = Field(
        default=None,
        description="A safe process correction that can be applied after human approval; never invent user data.",
    )
    question: Optional[str] = Field(
        default=None,
        description="Short question to show the user when action is ask_user.",
    )
    reason: str = Field(
        description="One concise sentence explaining the recovery choice; do not reveal hidden chain-of-thought.",
    )


TOOL_CATALOGS: dict[str, list[dict[str, Any]]] = {
    "simple": [
        {
            "name": "parse_amount",
            "description": "Ask Luna to extract one amount from the supplied text.",
            "read_only": True,
            "side_effect": None,
        },
    ],
    "medium": [
        {
            "name": "lookup_user",
            "description": "Read the user record from the workflow's user store.",
            "read_only": True,
            "side_effect": None,
        },
        {
            "name": "draft_message",
            "description": "Ask Luna to draft a welcome message from the validated record.",
            "read_only": True,
            "side_effect": None,
        },
        {
            "name": "review_message",
            "description": "Ask Luna to evaluate the draft against the editorial rubric.",
            "read_only": True,
            "side_effect": None,
        },
    ],
    "complex": [
        {
            "name": "transform_batch",
            "description": "Parse and validate transaction amounts in the supplied batch.",
            "read_only": True,
            "side_effect": None,
        },
        {
            "name": "sync_external",
            "description": "Synchronize valid records with the external pricing dependency.",
            "read_only": True,
            "side_effect": None,
        },
        {
            "name": "quality_gate",
            "description": "Ask Luna whether a human instruction authorizes dropping invalid records.",
            "read_only": True,
            "side_effect": None,
        },
        {
            "name": "commit_records",
            "description": "Write surviving records to the target store.",
            "read_only": False,
            "side_effect": "writes records",
        },
    ],
    "jira": [
        {
            "name": "get_issue",
            "description": "Read Jira issue summary and current status.",
            "read_only": True,
            "side_effect": None,
        },
        {
            "name": "get_transitions",
            "description": "Read legal Jira transitions currently available for the issue.",
            "read_only": True,
            "side_effect": None,
        },
        {
            "name": "transition_issue",
            "description": "Apply one exact transition id returned by get_transitions.",
            "read_only": False,
            "side_effect": "writes Jira status",
        },
    ],
}


WORKFLOW_NODES: dict[str, set[str]] = {
    "simple": {"parse"},
    "medium": {"fetch", "validate", "draft", "review"},
    "complex": {"extract", "transform", "sync_external", "quality_gate", "commit"},
    "jira": {"A_inspect", "B_transition", "C_verify"},
}


def get_tool_catalog(workflow_name: str) -> list[dict[str, Any]]:
    return [dict(tool) for tool in TOOL_CATALOGS.get(workflow_name, [])]


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if any(secret in str(key).casefold() for secret in ("token", "secret", "password", "api_key")):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _state_context(state: dict[str, Any]) -> dict[str, Any]:
    context_keys = (
        "workflow_name",
        "workflow_input",
        "attempt",
        "max_attempts",
        "failed_step",
        "issue_key",
        "target_status",
        "current_status",
        "available_transitions",
        "record",
        "draft",
        "records",
    )
    return _redact({key: state[key] for key in context_keys if key in state})


def _prompt(
    workflow_name: str,
    state: dict[str, Any],
    result: dict[str, Any],
    failed_step: Optional[str],
    tools: list[dict[str, Any]],
) -> str:
    from agent_registry import registry
    manifest = registry.get_agent(workflow_name)
    agent_context = ""
    if manifest:
        agent_context = (
            f"Agent Role & Purpose: {manifest.purpose}\n"
            f"Agent System Prompt & Constraints: {manifest.system_prompt}\n\n"
        )

    tool_text = json.dumps(tools, indent=2, default=str)
    state_text = json.dumps(_state_context(state), indent=2, default=str)
    return (
        "You are the recovery planner inside a checkpointed workflow. Analyze the "
        "failure and choose one bounded recovery action within the frame and capabilities "
        "of the responsible agent. You may propose a plan, but you do not execute tools. "
        "Never invent tool names, node names, Jira issue keys, transition ids, credentials, "
        "or user data. A human approval gate follows your plan before a retry.\n\n"
        f"{agent_context}"
        "Action rules:\n"
        "- retry_same: only for a safe, idempotent transient retry.\n"
        "- refresh_and_retry: reread current external state, then resume the failed node.\n"
        "- retry_with_correction: use only a correction already present in state; do not "
        "guess missing values.\n"
        "- ask_user: request missing input, a corrected identifier, permission, or an "
        "explicit decision. Use this for a missing or inaccessible Jira issue, or when "
        "requested status transition is not available so user can decide next steps.\n"
        "- stop: only when strictly unrecoverable and human input cannot help at all.\n"
        "Resume at the failed node by default. Choose a necessary upstream node only "
        "when the correction must regenerate its output; never skip required state. "
        "Select tool_name only from the catalog. Keep reason and question concise; do "
        "not reveal hidden chain-of-thought.\n\n"
        f"Workflow: {workflow_name}\n"
        f"Failed node: {failed_step or 'unknown'}\n"
        f"Failure status: {result.get('error_status')}\n"
        f"Failure message: {result.get('error_message')}\n"
        f"Workflow retry flag: {result.get('retry')}\n\n"
        f"Current state:\n{state_text}\n\n"
        f"Available tools:\n{tool_text}"
    )


def _as_dict(decision: RecoveryDecision) -> dict[str, Any]:
    if hasattr(decision, "model_dump"):
        return decision.model_dump()
    return decision.dict()


def _normalize(
    decision: RecoveryDecision,
    workflow_name: str,
    result: dict[str, Any],
    failed_step: Optional[str],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    values = _as_dict(decision)
    allowed_nodes = WORKFLOW_NODES.get(workflow_name, set())
    allowed_tools = {tool["name"] for tool in tools}
    if values.get("resume_node") not in allowed_nodes:
        values["resume_node"] = failed_step if failed_step in allowed_nodes else None
    if values.get("tool_name") not in allowed_tools:
        values["tool_name"] = None

    action = values["action"]
    retryable = bool(result.get("retry"))
    error_status = result.get("error_status")
    if error_status == "FATAL":
        action = "stop"
    if error_status == "RESOURCE":
        action = "stop"
    if action == "retry_same" and (not retryable or error_status != "TRANSIENT"):
        action = "ask_user" if error_status == "VALIDATION" else "stop"
    if action in {"retry_with_correction", "refresh_and_retry"} and not retryable:
        action = "ask_user" if error_status == "VALIDATION" else "stop"
    if action == "retry_with_correction" and not values.get("rectification"):
        action = "ask_user"
    if action == "ask_user" and not values.get("question"):
        values["question"] = "What correction or additional information should the workflow use?"
    if action in {"retry_same", "retry_with_correction", "refresh_and_retry"}:
        values["resume_node"] = values.get("resume_node") or failed_step
    values["action"] = action
    return values


class RecoveryAgent:
    """Suggest bounded recovery plans from workflow failure envelopes."""

    def plan(
        self,
        workflow_name: str,
        state: dict[str, Any],
        result: dict[str, Any],
        failed_step: Optional[str],
    ) -> dict[str, Any]:
        tools = get_tool_catalog(workflow_name)
        try:
            decision = llm.invoke_structured(
                RecoveryDecision,
                _prompt(workflow_name, state, result, failed_step, tools),
                max_tokens=768,
            )
            values = _normalize(decision, workflow_name, result, failed_step, tools)
        except Exception as exc:
            values = {
                "action": "stop",
                "resume_node": failed_step,
                "tool_name": None,
                "rectification": None,
                "question": None,
                "reason": f"Recovery planner failed: {exc}",
            }
        values["available_tools"] = tools
        return values


def plan_recovery(
    workflow_name: str,
    state: dict[str, Any],
    result: dict[str, Any],
    failed_step: Optional[str],
) -> dict[str, Any]:
    """Compatibility wrapper; new graph code should use RecoveryAgent."""
    return RecoveryAgent().plan(workflow_name, state, result, failed_step)