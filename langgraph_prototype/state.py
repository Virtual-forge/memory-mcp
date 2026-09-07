"""
Shared contract between the orchestrator and every workflow subgraph.

Every workflow, regardless of how many internal nodes it has, must end by
setting `result` to a WorkflowResult. Workflows may also expose their failed
node and checkpoint context so the recovery planner can choose a safe retry.
"""

from typing import TypedDict, Literal, Optional, Any, Annotated
import operator

ErrorStatus = Literal["TRANSIENT", "VALIDATION", "RESOURCE", "FATAL"]


class WorkflowResult(TypedDict):
    status: Literal["success", "failure"]
    error_status: Optional[ErrorStatus]
    error_message: Optional[str]
    retry: bool                 # workflow's own opinion on whether this is worth retrying
    payload: Optional[Any]      # final output only


class OrchestratorState(TypedDict):
    workflow_name: str
    workflow_input: dict
    attempt: int
    max_attempts: int
    last_result: Optional[WorkflowResult]
    rectification: Optional[str]
    hitl_decision: Optional[Literal["approve", "reject"]]
    history: Annotated[list, operator.add]
    failed_step: Optional[str]
    workflow_context: dict
    available_tools: list[dict]
    recovery_plan: Optional[dict]
    rectification_plan: Optional[dict]
    tool_output: Optional[Any]


def ok(payload: Any = None) -> WorkflowResult:
    """A successful WorkflowResult."""
    return {
        "status": "success",
        "error_status": None,
        "error_message": None,
        "retry": False,
        "payload": payload,
    }


def fail(error_status: ErrorStatus, message: str, retry: bool, payload: Any = None) -> WorkflowResult:
    """A failed WorkflowResult. `retry=False` short-circuits straight to give_up,
    regardless of how many attempts are left."""
    return {
        "status": "failure",
        "error_status": error_status,
        "error_message": message,
        "retry": retry,
        "payload": payload,
    }
