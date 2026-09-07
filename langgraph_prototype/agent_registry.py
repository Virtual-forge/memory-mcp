"""Agent and Tool Registry for Generic Triad-Based Execution.

Provides uniform schema for registering agent capabilities, system prompts,
and tools with semantic metadata for automated precondition checking,
grounded option extraction, and recovery planning.
"""

from typing import Any, Callable, Dict, List, Optional
from pydantic import BaseModel, Field


class ToolMetadata(BaseModel):
    name: str = Field(..., description="Unique tool identifier (e.g. jira.get_transitions)")
    description: str = Field(..., description="Semantic purpose of tool")
    read_only: bool = Field(default=True, description="True if safe and side-effect free")
    target_entity: Optional[str] = Field(default=None, description="Entity impacted or queried")
    options_path: Optional[str] = Field(default=None, description="Key or expression to extract allowed choices")


class AgentManifest(BaseModel):
    name: str = Field(..., description="Unique agent identifier")
    purpose: str = Field(..., description="High-level goal and capabilities of agent")
    system_prompt: str = Field(..., description="Operational boundaries and rules for agent")
    tools: List[ToolMetadata] = Field(default_factory=list, description="Tools accessible to this agent")


class AgentRegistry:
    def __init__(self):
        self._agents: Dict[str, AgentManifest] = {}
        self._tool_handlers: Dict[str, Callable[..., Any]] = {}

    def register_agent(self, manifest: AgentManifest):
        self._agents[manifest.name] = manifest

    def register_tool_handler(self, name: str, handler: Callable[..., Any]):
        self._tool_handlers[name] = handler

    def get_agent(self, name: str) -> Optional[AgentManifest]:
        return self._agents.get(name)

    def get_tool_handler(self, name: str) -> Optional[Callable[..., Any]]:
        return self._tool_handlers.get(name)

    def get_agent_tools(self, agent_name: str) -> List[ToolMetadata]:
        agent = self.get_agent(agent_name)
        return agent.tools if agent else []

    def list_agents(self) -> List[AgentManifest]:
        return list(self._agents.values())


registry = AgentRegistry()

# Register Jira Operator Manifest
registry.register_agent(
    AgentManifest(
        name="jira",
        purpose="Inspect, transition, and verify Jira work items safely with verified legal status changes.",
        system_prompt=(
            "You are a dedicated Jira operations agent. Your responsibility is to inspect "
            "tickets, check allowed status transitions, safely perform transitions, and "
            "verify final statuses. You must never invent issue keys or bypass legal transitions."
        ),
        tools=[
            ToolMetadata(
                name="get_issue",
                description="Read Jira issue summary and current status.",
                read_only=True,
                target_entity="issue",
            ),
            ToolMetadata(
                name="get_transitions",
                description="Read legal Jira transitions currently available for the issue.",
                read_only=True,
                target_entity="status_transition",
                options_path="to_status",
            ),
            ToolMetadata(
                name="transition_issue",
                description="Apply one exact transition id returned by get_transitions.",
                read_only=False,
                target_entity="issue_status",
            ),
            ToolMetadata(
                name="search_issues",
                description="Search Jira tickets with a JQL query.",
                read_only=True,
                target_entity="issue_list",
            ),
        ],
    )
)
