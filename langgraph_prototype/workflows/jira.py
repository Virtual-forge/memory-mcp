"""Resumable Jira transition workflow.

The graph deliberately keeps inspection, mutation, and verification as
separate top-level nodes so a retry resumes at the failed operation.
"""

import re
from typing import Literal, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

import llm
from jira_api import JiraApiError, JiraClient, JiraConfigurationError
from state import ErrorStatus, WorkflowResult, fail, ok


class TransitionChoice(BaseModel):
    action: Literal["transition", "cannot_apply"] = Field(
        description="Choose transition only when one listed transition is safe and valid; "
        "otherwise choose cannot_apply."
    )
    transition_id: Optional[str] = Field(
        default=None,
        description="Exact id of one listed Jira transition, or null when cannot_apply.",
    )
    reason: str = Field(description="One concise sentence explaining the choice.")


class JiraWorkflowState(TypedDict, total=False):
    workflow_name: str
    workflow_input: dict
    attempt: int
    max_attempts: int
    result: Optional[WorkflowResult]
    rectification: Optional[str]
    issue_key: str
    target_status: Optional[str]
    instruction: str
    issue: dict
    available_transitions: list[dict]
    current_status: str
    selected_transition: dict
    expected_status: str
    failed_step: str
    failure_injected: bool
    simulate_failure_at: Optional[str]
    resume_node: Optional[str]
    workflow_context: dict


def _input(state: JiraWorkflowState) -> dict:
    return state.get("workflow_input") or state


def _issue_key(state: JiraWorkflowState) -> str:
    raw = str(state.get("issue_key") or _input(state).get("issue_key") or "").strip()
    # Normalize space to hyphen if user wrote "SCRUM 80" instead of "SCRUM-80"
    match = re.match(r"^([A-Za-z]+)\s+(\d+)$", raw)
    if match:
        return f"{match.group(1).upper()}-{match.group(2)}"
    return raw.upper()


def _target_status(state: JiraWorkflowState) -> Optional[str]:
    target = _input(state).get("target_status") or state.get("target_status")
    return str(target).strip() if target else None


def _status_name(issue: dict) -> str:
    status = issue.get("fields", {}).get("status", {})
    return str(status.get("name") or status.get("id") or "unknown")


def _normalize_transition(transition: dict) -> dict:
    destination = transition.get("to") or {}
    return {
        "id": str(transition.get("id", "")),
        "name": str(transition.get("name") or ""),
        "to_status": str(destination.get("name") or destination.get("id") or ""),
    }


def _transition_text(transitions: list[dict]) -> str:
    return "\n".join(
        f"- id={item['id']}, action={item['name']}, destination={item['to_status']}"
        for item in transitions
    ) or "- no transitions are currently available"


def _failure(
    step: str,
    error_status: ErrorStatus,
    message: str,
    retry: bool,
    payload: object = None,
) -> dict:
    result = fail(error_status, message, retry, payload=payload)
    return {"result": result, "failed_step": step}


def _api_failure(step: str, exc: JiraApiError, issue_key: Optional[str] = None) -> dict:
    retryable = exc.retryable
    error_status: ErrorStatus = "TRANSIENT" if retryable else "RESOURCE"
    message = str(exc)
    if exc.status_code == 404 and issue_key:
        error_status = "VALIDATION"
        retryable = True
        message = (
            f"Jira issue '{issue_key}' was not found or is not visible to this account. "
            "Provide a valid issue key, or check the account's Browse Issues permission."
        )
    failure = _failure(step, error_status, message, retryable)
    if issue_key:
        failure["issue_key"] = issue_key
    return failure


def begin_attempt(state: JiraWorkflowState) -> dict:
    return {
        "attempt": state.get("attempt", 0) + 1,
    }


def inspect_issue(state: JiraWorkflowState) -> dict:
    issue_key = _issue_key(state)
    if not issue_key:
        return _failure("A_inspect", "FATAL", "No Jira issue key was provided.", False)

    try:
        client = JiraClient.from_env()
        issue = client.get_issue(issue_key)
        transitions = [
            _normalize_transition(item)
            for item in client.get_transitions(issue_key)
        ]
    except JiraConfigurationError as exc:
        return _failure("A_inspect", "RESOURCE", str(exc), False)
    except JiraApiError as exc:
        return _api_failure("A_inspect", exc, issue_key)

    return {
        "issue_key": issue_key,
        "target_status": _target_status(state),
        "instruction": str(_input(state).get("instruction") or ""),
        "issue": issue,
        "available_transitions": transitions,
        "current_status": _status_name(issue),
    }


def _planner_prompt(state: JiraWorkflowState, transitions: list[dict]) -> str:
    target = _target_status(state) or "not specified"
    rectification = state.get("rectification") or "none"
    return (
        "You are the transition planner for a Jira issue. Select only from the exact "
        "transition list below. Never invent a transition id.\n\n"
        f"Issue: {_issue_key(state)}\n"
        f"Current status: {state.get('current_status', 'unknown')}\n"
        f"Requested destination: {target}\n"
        f"User instruction: {state.get('instruction') or 'none'}\n"
        f"Recovery note: {rectification}\n\n"
        "Choose only a transition whose destination exactly matches the requested "
        "destination. If none is safe, return cannot_apply.\n\n"
        f"Available transitions:\n{_transition_text(transitions)}"
    )


def transition_issue(state: JiraWorkflowState) -> dict:
    input_data = _input(state)
    if (
        input_data.get("simulate_failure_at") == "B_transition"
        and not state.get("failure_injected")
    ):
        return {
            **_failure(
                "B_transition",
                "TRANSIENT",
                "Injected recoverable failure at step B before the Jira mutation.",
                True,
            ),
            "failure_injected": True,
        }

    issue_key = _issue_key(state)
    try:
        client = JiraClient.from_env()
        current_issue = client.get_issue(issue_key)
        current_status = _status_name(current_issue)
    except JiraConfigurationError as exc:
        return _failure("B_transition", "RESOURCE", str(exc), False)
    except JiraApiError as exc:
        return _api_failure("B_transition", exc, issue_key)

    target = _target_status(state)
    if target and current_status.casefold() == target.casefold():
        selected = state.get("selected_transition") or {
            "id": None,
            "name": "Already at target",
            "to_status": current_status,
        }
        return {
            "issue": current_issue,
            "current_status": current_status,
            "available_transitions": list(state.get("available_transitions", [])),
            "selected_transition": selected,
            "expected_status": current_status,
        }

    try:
        transitions = [
            _normalize_transition(item)
            for item in client.get_transitions(issue_key)
        ]
    except JiraApiError as exc:
        return _api_failure("B_transition", exc, issue_key)

    if not transitions:
        return _failure(
            "B_transition",
            "VALIDATION",
            "Jira exposes no transitions for this issue.",
            True,
        )

    planning_state = {**state, "current_status": current_status}
    try:
        choice = llm.invoke_structured(
            TransitionChoice,
            _planner_prompt(planning_state, transitions),
            max_tokens=512,
        )
    except Exception as exc:
        return _failure("B_transition", "RESOURCE", f"Transition planner failed: {exc}", True)

    selected = next(
        (item for item in transitions if item["id"] == str(choice.transition_id)),
        None,
    )
    if choice.action != "transition" or selected is None:
        return {
            **_failure(
                "B_transition",
                "VALIDATION",
                f"Planner could not choose a valid Jira transition: {choice.reason}. "
                f"Available destinations: {', '.join(item['to_status'] for item in transitions)}",
                True,
            ),
            "available_transitions": transitions,
        }

    if target and selected["to_status"].casefold() != target.casefold():
        return {
            **_failure(
                "B_transition",
                "VALIDATION",
                f"Planner selected '{selected['to_status']}', but requested destination is '{target}'.",
                True,
            ),
            "available_transitions": transitions,
        }

    try:
        client.transition_issue(issue_key, selected["id"])
    except JiraConfigurationError as exc:
        return _failure("B_transition", "RESOURCE", str(exc), False)
    except JiraApiError as exc:
        if exc.status_code in {400, 409}:
            return _failure("B_transition", "VALIDATION", str(exc), True)
        return _api_failure("B_transition", exc, issue_key)

    return {
        "available_transitions": transitions,
        "selected_transition": selected,
        "expected_status": selected["to_status"],
    }


def verify_issue(state: JiraWorkflowState) -> dict:
    try:
        issue = JiraClient.from_env().get_issue(_issue_key(state))
    except JiraConfigurationError as exc:
        return _failure("C_verify", "RESOURCE", str(exc), False)
    except JiraApiError as exc:
        return _api_failure("C_verify", exc, _issue_key(state))

    actual_status = _status_name(issue)
    expected_status = state.get("expected_status") or _target_status(state)
    if expected_status and actual_status.casefold() != expected_status.casefold():
        return _failure(
            "C_verify",
            "TRANSIENT",
            f"Jira still reports '{actual_status}', expected '{expected_status}'.",
            True,
        )

    result = ok(
        payload={
            "issue_key": _issue_key(state),
            "from_status": state.get("current_status"),
            "to_status": actual_status,
            "transition": state.get("selected_transition"),
            "summary": issue.get("fields", {}).get("summary"),
        }
    )
    return {"issue": issue, "result": result}


def route_after_inspect(state: JiraWorkflowState) -> str:
    result = state.get("result")
    if not result:
        return "continue"
    return "stop"


def route_after_transition(state: JiraWorkflowState) -> str:
    result = state.get("result")
    if not result:
        return "continue"
    if result["status"] == "success":
        return "done"
    return "stop"


def route_after_verify(state: JiraWorkflowState) -> str:
    result = state.get("result")
    if result and result["status"] == "success":
        return "done"
    if not result:
        return "stop"
    return "stop"


def route_after_begin_attempt(state: JiraWorkflowState) -> str:
    return state.get("resume_node") or state.get("failed_step") or "A_inspect"


def build(checkpointer=None):
    builder = StateGraph(JiraWorkflowState)
    builder.add_node("begin_attempt", begin_attempt)
    builder.add_node("A_inspect", inspect_issue)
    builder.add_node("B_transition", transition_issue)
    builder.add_node("C_verify", verify_issue)

    builder.add_edge(START, "begin_attempt")
    builder.add_conditional_edges(
        "begin_attempt",
        route_after_begin_attempt,
        {
            "A_inspect": "A_inspect",
            "B_transition": "B_transition",
            "C_verify": "C_verify",
        },
    )
    builder.add_conditional_edges(
        "A_inspect",
        route_after_inspect,
        {"continue": "B_transition", "stop": END},
    )
    builder.add_conditional_edges(
        "B_transition",
        route_after_transition,
        {"continue": "C_verify", "stop": END, "done": END},
    )
    builder.add_conditional_edges(
        "C_verify",
        route_after_verify,
        {"stop": END, "done": END},
    )
    return builder.compile(checkpointer=checkpointer) if checkpointer else builder.compile()