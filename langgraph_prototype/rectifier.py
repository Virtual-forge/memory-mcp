"""Rectifier Agent: interprets human rectification text to adapt workflow execution.

The Rectifier sits between human approval and the next workflow attempt.
It inspects the user's freeform feedback/rectification, the current workflow state,
and available tools to produce an actionable plan:
  - patch_input: update workflow input fields (e.g. new issue key, email)
  - run_tool: invoke a read-only auxiliary tool (e.g. search Jira tickets)
  - retry: continue with standard recovery hint
  - stop: user explicitly aborted or no safe action exists
"""

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field

import llm


RectifierAction = Literal[
    "patch_input",
    "run_tool",
    "retry",
    "stop",
]


class RectificationPlan(BaseModel):
    action: RectifierAction = Field(
        description="The action orchestrator must take based on human rectification."
    )
    patched_input: Optional[dict[str, Any]] = Field(
        default=None,
        description="Dict of key-values to update in workflow_input if action is patch_input.",
    )
    tool_name: Optional[str] = Field(
        default=None,
        description="Tool name to execute if action is run_tool (e.g. 'search_issues').",
    )
    tool_args: Optional[dict[str, Any]] = Field(
        default=None,
        description="Arguments for the tool if action is run_tool.",
    )
    explanation: str = Field(
        description="Brief human-readable summary of what the rectifier decided and why.",
    )


def _rectifier_prompt(
    workflow_name: str,
    rectification_text: str,
    workflow_input: dict[str, Any],
    failed_step: Optional[str],
    last_error: Optional[str],
) -> str:
    return (
        "You are the Rectifier Agent in an agentic orchestrator system. "
        "A human operator has provided feedback/rectification after a workflow paused/failed. "
        "Your job is to read their exact words and decide how to adjust execution.\n\n"
        "Available actions:\n"
        "- patch_input: if the user provided specific values to fix/override (e.g., a new issue key like SCRUM-2, a new email, an amount). Provide patched_input.\n"
        "- run_tool: if the user asks to check, list, or search for information first (e.g., 'check available tickets', 'list issues in project', 'find open bugs'). Set tool_name='search_issues' and tool_args with jql if applicable.\n"
        "- retry: if the user just gave advice/hint on how to proceed without changing input or requesting a tool query.\n"
        "- stop: if the user explicitly wants to cancel, abort, or give up.\n\n"
        f"Workflow Name: {workflow_name}\n"
        f"Failed Step: {failed_step or 'None'}\n"
        f"Last Error: {last_error or 'None'}\n"
        f"Current Workflow Input: {workflow_input}\n"
        f"Human Feedback / Rectification: \"{rectification_text}\"\n\n"
        "Be helpful, accurate, and faithful to the user's intent."
    )


class RectifierAgent:
    """Interprets human rectification input and returns a structured RectificationPlan."""

    enabled: bool = True

    def interpret(
        self,
        workflow_name: str,
        rectification_text: str,
        workflow_input: dict[str, Any],
        failed_step: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> RectificationPlan:
        if not self.enabled or not rectification_text or not rectification_text.strip():
            return RectificationPlan(
                action="retry",
                explanation="Rectifier inactive or no rectification text provided.",
            )

        prompt = _rectifier_prompt(
            workflow_name,
            rectification_text,
            workflow_input,
            failed_step,
            last_error,
        )

        try:
            plan = llm.invoke_structured(
                RectificationPlan,
                prompt,
                max_tokens=512,
            )
            return plan
        except Exception as exc:
            return RectificationPlan(
                action="retry",
                explanation=f"Rectifier failed to parse intent ({exc}); falling back to standard retry.",
            )
