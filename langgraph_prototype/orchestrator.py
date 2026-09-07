"""
The orchestrator: one graph that drives any workflow in WORKFLOW_REGISTRY.

The graph has three logic nodes:
  - run_workflow            invokes the target subgraph, normalizes its
                             output, and is the ONLY thing that touches
                             workflow internals.
    - recover_failure         asks the separate RecoveryAgent for a bounded plan.
  - request_retry_approval  the HITL gate. Swap its body for an approval
                             agent later and nothing else here changes.
"""

import re
from typing import Any, Literal, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from recovery import RecoveryAgent
from rectifier import RectifierAgent
from state import OrchestratorState

WORKFLOW_REGISTRY: dict = {}
recovery_agent = RecoveryAgent()
rectifier_agent = RectifierAgent()


def register_workflow(name: str, compiled_graph) -> None:
    WORKFLOW_REGISTRY[name] = compiled_graph


def run_workflow(state: OrchestratorState) -> dict:
    graph = WORKFLOW_REGISTRY[state["workflow_name"]]

    sub_input = dict(state["workflow_input"])
    workflow_context = state.get("workflow_context") or {}
    sub_input.update(workflow_context)
    sub_input["workflow_context"] = workflow_context
    sub_input["attempt"] = state["attempt"]
    sub_input["failed_step"] = state.get("failed_step")
    recovery_plan = state.get("recovery_plan") or {}
    sub_input["resume_node"] = recovery_plan.get("resume_node")
    sub_input["recovery_plan"] = recovery_plan
    if state.get("rectification"):
        sub_input["rectification"] = state["rectification"]

    sub_state = graph.invoke(sub_input)
    result = sub_state["result"]
    context_keys = (
        "record",
        "draft",
        "records",
        "batch",
        "issue",
        "current_status",
        "available_transitions",
        "selected_transition",
        "expected_status",
        "failure_injected",
    )
    workflow_context = {
        key: sub_state[key]
        for key in context_keys
        if key in sub_state
    }

    return {
        "last_result": result,
        "history": [result],
        "attempt": state["attempt"] + 1,
        "failed_step": sub_state.get("failed_step"),
        "workflow_context": workflow_context,
        "available_tools": [],
        "recovery_plan": None,
    }


def evaluate(state: OrchestratorState) -> Literal["succeed", "give_up", "recover"]:
    result = state["last_result"]
    if result["status"] == "success":
        return "succeed"
    max_att = state.get("max_attempts")
    if max_att is not None and max_att > 0 and state["attempt"] >= max_att:
        return "give_up"
    return "recover"


def recover_failure(state: OrchestratorState) -> dict:
    result = state["last_result"]
    plan = recovery_agent.plan(
        state["workflow_name"],
        state,
        result,
        state.get("failed_step"),
    )
    return {
        "recovery_plan": plan,
        "available_tools": plan.get("available_tools", []),
    }


def route_after_recovery(state: OrchestratorState) -> Literal["ask_approval", "give_up"]:
    plan = state.get("recovery_plan") or {}
    # When user hitl decision is explicit reject or plan explicitly stopped and no max_attempts, stop
    return "give_up" if plan.get("action") == "stop" else "ask_approval"


def request_retry_approval(state: OrchestratorState) -> dict:
    result = state["last_result"]
    plan = state.get("recovery_plan") or {}

    # Extract grounded options if available in state or workflow_context
    grounded_options = []
    wf_context = state.get("workflow_context") or {}
    transitions = wf_context.get("available_transitions") or []
    if isinstance(transitions, list):
        for t in transitions:
            if isinstance(t, dict) and "to_status" in t:
                grounded_options.append(t["to_status"])

    decision = interrupt({
        "workflow_name": state["workflow_name"],
        "attempt": state["attempt"],
        "status": result["status"],
        "error_status": result["error_status"],
        "error_message": result["error_message"],
        "failed_step": state.get("failed_step"),
        "recovery_plan": plan,
        "available_tools": state.get("available_tools", []),
        "grounded_options": grounded_options,
        "question": plan.get("question") or "Approve the recovery plan? Optionally supply a correction.",
    })
    if not isinstance(decision, dict):
        decision = {"decision": "reject"}
    rectification = decision.get("rectification")
    if not rectification and plan.get("action") == "retry_with_correction":
        rectification = plan.get("rectification")

    return {
        "hitl_decision": decision.get("decision", "reject"),
        "rectification": rectification,
    }


def interpret_rectification(state: OrchestratorState) -> dict:
    """Consult the RectifierAgent to understand the user's intent."""
    rect_text = (state.get("rectification") or "").strip()
    last_err = (state.get("last_result") or {}).get("error_message")

    # Fast deterministic heuristic for common issue key replacements (keeps tests isolated & avoids extra LLM calls)
    if state.get("workflow_name") == "jira":
        match = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", rect_text, re.IGNORECASE)
        # If user explicitly asks to search/list/check tickets, let the Rectifier LLM handle it
        wants_search = any(w in rect_text.lower() for w in ["check", "list", "search", "tickets", "available", "find", "show"])
        if match and not wants_search:
            return {
                "workflow_input": {
                    **state.get("workflow_input", {}),
                    "issue_key": match.group(1).upper(),
                },
                "rectification_plan": {
                    "action": "patch_input",
                    "patched_input": {"issue_key": match.group(1).upper()},
                    "explanation": f"Detected issue key {match.group(1).upper()}.",
                }
            }

    # If no text or simple advice in standard tests, avoid LLM call to not desync test mock queues
    if not rect_text:
        return {"rectification_plan": {"action": "retry", "explanation": "No rectification provided."}}

    plan = rectifier_agent.interpret(
        workflow_name=state["workflow_name"],
        rectification_text=rect_text,
        workflow_input=state.get("workflow_input", {}),
        failed_step=state.get("failed_step"),
        last_error=last_err,
    )

    plan_dict = plan.model_dump() if hasattr(plan, "model_dump") else plan.dict()
    updates: dict[str, Any] = {"rectification_plan": plan_dict}

    if plan.action == "patch_input" and plan.patched_input:
        updates["workflow_input"] = {
            **state.get("workflow_input", {}),
            **plan.patched_input,
        }
    elif plan.action == "stop":
        updates["hitl_decision"] = "reject"

    return updates


def execute_rectifier_tool(state: OrchestratorState) -> dict:
    """Executes a tool requested by the Rectifier and sets up re-approval."""
    plan = state.get("rectification_plan") or {}
    tool_name = plan.get("tool_name")
    tool_args = plan.get("tool_args") or {}

    tool_result = None
    if state.get("workflow_name") == "jira" and tool_name == "search_issues":
        try:
            import workflows.jira
            client = getattr(workflows.jira, "JiraClient", None)
            if client is None:
                from jira_api import JiraClient
                client = JiraClient
            active_client = client.from_env()
            jql = tool_args.get("jql", "order by created DESC")
            raw_issues = active_client.search_issues(jql=jql, max_results=5)
            tool_result = [
                {
                    "key": i.get("key"),
                    "summary": (i.get("fields") or {}).get("summary"),
                    "status": ((i.get("fields") or {}).get("status") or {}).get("name"),
                }
                for i in raw_issues
            ]
        except Exception as exc:
            tool_result = f"Error executing search_issues: {exc}"

    question = (
        f"Rectifier executed tool '{tool_name}'. Results:\n{tool_result}\n\n"
        "How would you like to proceed? (e.g. choose an issue key)"
    )

    rec_plan = state.get("recovery_plan") or {}
    rec_plan = {**rec_plan, "question": question}

    return {
        "tool_output": tool_result,
        "recovery_plan": rec_plan,
    }


def route_after_approval(state: OrchestratorState) -> Literal["interpret", "give_up"]:
    return "interpret" if state.get("hitl_decision") == "approve" else "give_up"


def route_after_rectifier(state: OrchestratorState) -> Literal["run_tool", "retry", "give_up"]:
    if state.get("hitl_decision") == "reject":
        return "give_up"
    plan = state.get("rectification_plan") or {}
    action = plan.get("action")
    if action == "run_tool":
        return "run_tool"
    elif action == "stop":
        return "give_up"
    return "retry"


def build_orchestrator(checkpointer):
    builder = StateGraph(OrchestratorState)
    builder.add_node("run_workflow", run_workflow)
    builder.add_node("recover_failure", recover_failure)
    builder.add_node("request_retry_approval", request_retry_approval)
    builder.add_node("interpret_rectification", interpret_rectification)
    builder.add_node("execute_rectifier_tool", execute_rectifier_tool)

    builder.add_edge(START, "run_workflow")
    builder.add_conditional_edges(
        "run_workflow",
        evaluate,
        {"succeed": END, "give_up": END, "recover": "recover_failure"},
    )
    builder.add_conditional_edges(
        "recover_failure",
        route_after_recovery,
        {"ask_approval": "request_retry_approval", "give_up": END},
    )
    builder.add_conditional_edges(
        "request_retry_approval",
        route_after_approval,
        {"interpret": "interpret_rectification", "give_up": END},
    )
    builder.add_conditional_edges(
        "interpret_rectification",
        route_after_rectifier,
        {"run_tool": "execute_rectifier_tool", "retry": "run_workflow", "give_up": END},
    )
    builder.add_edge("execute_rectifier_tool", "request_retry_approval")
    return builder.compile(checkpointer=checkpointer)
