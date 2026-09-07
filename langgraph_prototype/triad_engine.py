"""Triad Execution Engine.

Executes actions adhering to the invariant:
  Phase 1: Precondition / Inspection (read state, determine valid choices)
  Phase 2: Mutation (execute change)
  Phase 3: Verification (confirm state changed)
"""

from typing import Any, Dict, List, Literal, Optional, TypedDict
from pydantic import BaseModel, Field

from agent_registry import registry
from state import ErrorStatus, WorkflowResult, fail, ok


class TriadStepSpec(BaseModel):
    step_id: str
    target_entity: str
    inspect_tool: Optional[str] = Field(default=None, description="Tool to read pre-state")
    inspect_args: Dict[str, Any] = Field(default_factory=dict)
    target_value: Optional[str] = Field(default=None, description="Requested target value e.g. target status")
    mutate_tool: str = Field(..., description="Tool that performs the modification")
    mutate_args: Dict[str, Any] = Field(default_factory=dict)
    verify_tool: Optional[str] = Field(default=None, description="Tool to read post-state")
    verify_args: Dict[str, Any] = Field(default_factory=dict)


class TriadExecutionState(TypedDict):
    spec: TriadStepSpec
    inspect_output: Optional[Any]
    grounded_options: List[str]
    precondition_met: bool
    mutation_output: Optional[Any]
    verification_output: Optional[Any]
    result: Optional[WorkflowResult]


def extract_grounded_options(inspect_result: Any, options_path: Optional[str] = None) -> List[str]:
    """Extract valid selectable options from an inspect tool result."""
    options: List[str] = []
    if isinstance(inspect_result, list):
        for item in inspect_result:
            if isinstance(item, dict):
                if options_path and options_path in item:
                    val = item[options_path]
                    if val and str(val) not in options:
                        options.append(str(val))
                elif "to_status" in item:
                    options.append(str(item["to_status"]))
                elif "name" in item:
                    options.append(str(item["name"]))
            elif isinstance(item, str) and item not in options:
                options.append(item)
    elif isinstance(inspect_result, dict):
        if options_path and options_path in inspect_result:
            val = inspect_result[options_path]
            if isinstance(val, list):
                options.extend(str(v) for v in val)
            elif val:
                options.append(str(val))
    return options


def check_precondition(
    target_value: Optional[str],
    grounded_options: List[str],
) -> tuple[bool, Optional[str]]:
    """Check if target requested value matches any available option."""
    if not target_value:
        return True, None
    if not grounded_options:
        # If no explicit list of options is restricted, pass
        return True, None

    for opt in grounded_options:
        if opt.casefold() == target_value.casefold():
            return True, opt

    return False, None
