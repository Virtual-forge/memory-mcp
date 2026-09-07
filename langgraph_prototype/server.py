"""
FastAPI Server for LangGraph Orchestrator & Workflows with SSE streaming.
Supports real-time node execution steps, Luna agent reasoning events, SQLite
state persistence, and interactive Human-in-the-Loop (HITL) retry/rectification.
"""
import asyncio
import json
import os
import re
import sys
import uuid
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Ensure current directory is in sys.path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(CURRENT_DIR, ".env"))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import llm
from jira_api import JiraApiError
from orchestrator import build_orchestrator, WORKFLOW_REGISTRY
from recovery import RecoveryDecision
import workflows  # Registers simple, medium, complex

DB_PATH = os.path.join(CURRENT_DIR, "orchestrator_state.db")

app = FastAPI(title="LangGraph Agentic Orchestrator UI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global persistent checkpointer
_sqlite_cm = SqliteSaver.from_conn_string(DB_PATH)
_checkpointer = _sqlite_cm.__enter__()
_orchestrator = build_orchestrator(_checkpointer)


def _graph_for_workflow(workflow_name: str):
    return _orchestrator


def _graph_and_snapshot_for_thread(thread_id: str):
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = _orchestrator.get_state(config)
    return (_orchestrator, snapshot) if snapshot and snapshot.values else (None, None)


class _DemoJiraClient:
    _issues: dict = {}

    @classmethod
    def from_env(cls):
        return cls()

    def _issue(self, issue_key: str) -> dict:
        return self._issues.setdefault(
            issue_key,
            {"summary": f"Demo issue {issue_key}", "status": "Pending"},
        )

    def get_issue(self, issue_key: str) -> dict:
        issue = self._issue(issue_key)
        return {
            "key": issue_key,
            "fields": {
                "summary": issue["summary"],
                "status": {"name": issue["status"]},
            },
        }

    def get_transitions(self, issue_key: str) -> list[dict]:
        status = self._issue(issue_key)["status"]
        transitions = {
            "Pending": [
                {"id": "approve", "name": "Approve", "to": {"name": "Approved"}},
                {"id": "reject", "name": "Reject", "to": {"name": "Rejected"}},
            ],
            "Approved": [
                {"id": "pending", "name": "Return to pending", "to": {"name": "Pending"}},
            ],
            "Rejected": [],
        }
        return transitions.get(status, [])

    def search_issues(self, jql: str = "order by created DESC", max_results: int = 10) -> list[dict]:
        if not self._issues:
            self._issue("DEMO-1")
            self._issue("DEMO-2")
        return [
            {
                "key": key,
                "fields": {
                    "summary": data.get("summary", f"Demo issue {key}"),
                    "status": {"name": data.get("status", "Pending")},
                },
            }
            for key, data in list(self._issues.items())[:max_results]
        ]

    def transition_issue(self, issue_key: str, transition_id: str) -> None:
        transition = next(
            (
                item
                for item in self.get_transitions(issue_key)
                if item["id"] == str(transition_id)
            ),
            None,
        )
        if transition is None:
            raise JiraApiError(
                f"Demo Jira rejected transition '{transition_id}'.",
                status_code=400,
                retryable=False,
            )
        self._issue(issue_key)["status"] = transition["to"]["name"]


@app.on_event("shutdown")
def shutdown_event():
    global _sqlite_cm
    if _sqlite_cm:
        _sqlite_cm.__exit__(None, None, None)


class StartRunRequest(BaseModel):
    workflow: str = Field(..., description="Name of workflow: simple, medium, complex, jira")
    input_data: Dict[str, Any] = Field(..., description="Input dictionary for the workflow")
    max_attempts: int = Field(default=5, ge=1, le=10)
    mock_mode: bool = Field(default=False, description="Use deterministic offline mock responses for zero-cost demo")
    planning_summary: Optional[str] = Field(default=None, description="Summary of the live model's routing decision")
    planning_details: Optional[Dict[str, Any]] = Field(default=None, description="Structured routing decision details")


class ResumeRunRequest(BaseModel):
    thread_id: str
    decision: str = Field(..., description="'approve' or 'reject'")
    rectification: Optional[str] = Field(default=None, description="Human guidance or data correction")
    mock_mode: bool = Field(default=False)


class ChatIntent(BaseModel):
    workflow: Literal["simple", "medium", "complex", "jira"] = Field(
        description="The single workflow that best matches the user's request."
    )
    issue_key: Optional[str] = Field(
        default=None,
        description="Exact Jira issue key such as PROJ-123 when the request is about Jira.",
    )
    target_status: Optional[str] = Field(
        default=None,
        description="Requested Jira destination status, preserving the user's meaning.",
    )
    user_id: Optional[str] = Field(
        default=None,
        description="User id when the request asks to fetch or prepare a user message.",
    )
    raw_amount: Optional[str] = Field(
        default=None,
        description="Amount-bearing text when the request asks to extract a monetary amount.",
    )
    batch: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Transaction records when the user supplied an explicit batch.",
    )
    min_quality: Optional[float] = Field(
        default=None,
        description="Quality threshold when explicitly supplied for a batch request.",
    )
    simulate_failure_at: Optional[Literal["B_transition"]] = Field(
        default=None,
        description="Set only when the user explicitly asks to simulate the Jira B-step failure.",
    )
    reason: str = Field(
        description="One brief sentence explaining the routing decision; do not reveal hidden chain-of-thought.",
    )


PRESET_EXAMPLES = {
    "simple": [
        {
            "label": "Ambiguous Text (Triggers Retry)",
            "description": "Luna fails to extract a clean number; human corrects with rectification hint.",
            "data": {"raw_amount": "he mentioned a price but I didn't really catch it, sorry"}
        },
        {
            "label": "Typo in Text (High Confidence)",
            "description": "Typo with currency notation that Luna parses successfully.",
            "data": {"raw_amount": "Total invoice came out to $1,450.75 after discount."}
        }
    ],
    "medium": [
        {
            "label": "Missing Email on User (u2)",
            "description": "Validation failure: user record lacks email. Human injects email or review notes.",
            "data": {"user_id": "u2"}
        },
        {
            "label": "Valid User (u1 - Happy Path)",
            "description": "Drafts personalized welcome message and grades against strict editorial rubric.",
            "data": {"user_id": "u1"}
        },
        {
            "label": "Fatal ID (does-not-exist)",
            "description": "Immediate FATAL give-up without wasting model calls.",
            "data": {"user_id": "does-not-exist"}
        }
    ],
    "complex": [
        {
            "label": "Mixed Batch with Flaky Dependency",
            "description": "Simulates 2 network timeouts, then enters quality gate requiring human go-ahead.",
            "data": {
                "batch": [
                    {"id": 1, "amount": "100.00"},
                    {"id": 2, "amount": "250.5"},
                    {"id": 3, "amount": "NOT_A_NUMBER"},
                    {"id": 4, "amount": "75"}
                ],
                "min_quality": 0.9
            }
        },
        {
            "label": "All-Bad Batch (FATAL)",
            "description": "Every row invalid; short-circuits to give up.",
            "data": {
                "batch": [
                    {"id": 1, "amount": "invalid"},
                    {"id": 2, "amount": "broken"}
                ],
                "min_quality": 0.9
            }
        }
    ],
    "jira": [
        {
            "label": "Resume a failed transition at B",
            "description": "Demo issue: inject a recoverable failure before the Jira mutation, then approve the retry.",
            "data": {
                "issue_key": "DEMO-1",
                "target_status": "Approved",
                "instruction": "Move DEMO-1 to Approved.",
                "simulate_failure_at": "B_transition"
            }
        }
    ]
}


@app.get("/api/config")
def get_config():
    jira_configured = all(
        os.environ.get(name)
        for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
    )
    return {
        "workflows": list(WORKFLOW_REGISTRY.keys()),
        "model": llm.MODEL_NAME,
        "nvidia_configured": bool(llm.API_KEY),
        "jira_configured": jira_configured,
        "presets": PRESET_EXAMPLES,
    }


def _serialize_for_sse(data: Any) -> str:
    try:
        return json.dumps(data, default=str)
    except Exception:
        return json.dumps({"raw": str(data)})


def _extract_reasoning_and_steps(subgraph_path: tuple, node_name: str, node_output: Any):
    """
    Format human-readable agent reasoning and execution details from node outputs.
    """
    if not isinstance(node_output, dict):
        return "", node_output

    summary = ""
    details = node_output

    if node_name == "parse":
        res = node_output.get("result", {})
        if res.get("status") == "success":
            amount = res.get("payload", {}).get("amount")
            summary = f"Luna successfully extracted amount: **${amount}** with high confidence."
        else:
            summary = f"Extraction rejected: {res.get('error_message')}"

    elif node_name == "fetch":
        if "record" in node_output:
            summary = f"Fetched record for user `{node_output['record'].get('user_id')}`: {node_output['record'].get('name')}"
        elif "result" in node_output:
            summary = f"Fetch failed: {node_output['result'].get('error_message')}"

    elif node_name == "validate":
        if "record" in node_output:
            summary = f"Validation passed. Email confirmed: `{node_output['record'].get('email')}`"
        elif "result" in node_output:
            summary = f"Validation failed: {node_output['result'].get('error_message')}"

    elif node_name == "draft":
        draft_text = node_output.get("draft", "")
        summary = f"Drafted welcome copy ({len(draft_text.split())} words):\n> \"{draft_text}\""

    elif node_name == "review":
        res = node_output.get("result", {})
        if res.get("status") == "success":
            summary = "Strict editorial review **PASSED** (all 4 rubric rules met)."
        else:
            summary = f"Editorial review **REJECTED**: {res.get('error_message')}"

    elif node_name == "extract":
        summary = f"Batch extraction received {len(node_output.get('batch', []))} incoming records."

    elif node_name == "transform":
        records = node_output.get("records", [])
        valid_cnt = sum(1 for r in records if r.get("valid"))
        summary = f"Transformed batch: {valid_cnt}/{len(records)} records valid."

    elif node_name == "sync_external":
        if "result" in node_output:
            summary = f"External dependency alert: {node_output['result'].get('error_message')}"
        else:
            summary = "External sync connection established successfully."

    elif node_name == "quality_gate":
        if "records" in node_output:
            summary = f"Quality gate authorized proceeding with {len(node_output['records'])} valid records."
        elif "result" in node_output:
            summary = f"Quality gate block: {node_output['result'].get('error_message')}"

    elif node_name == "commit":
        committed = node_output.get("result", {}).get("payload", {}).get("committed")
        summary = f"Database commit finished: {committed} records stored."

    elif node_name == "run_workflow":
        last_res = node_output.get("last_result", {})
        status = last_res.get("status")
        summary = f"Workflow completed attempt {node_output.get('attempt')}: **{status.upper()}**"

    elif node_name == "begin_attempt":
        summary = f"Starting Jira workflow attempt {node_output.get('attempt')}."

    elif node_name == "A_inspect":
        status = node_output.get("current_status")
        transitions = node_output.get("available_transitions", [])
        if status:
            destinations = ", ".join(item.get("to_status", "unknown") for item in transitions)
            summary = f"Inspected Jira issue in **{status}**. Available destinations: {destinations or 'none'}."
        elif node_output.get("last_result"):
            summary = f"Jira inspection failed: {node_output['last_result'].get('error_message')}"

    elif node_name == "B_transition":
        selected = node_output.get("selected_transition")
        if selected:
            summary = f"Applied Jira transition **{selected.get('name') or selected.get('id')}** to **{selected.get('to_status')}**."
        elif node_output.get("last_result"):
            summary = f"Jira transition failed: {node_output['last_result'].get('error_message')}"

    elif node_name == "C_verify":
        issue = node_output.get("issue", {})
        if issue:
            summary = f"Verified Jira issue status: **{_jira_status_name(issue)}**."
        elif node_output.get("last_result"):
            summary = f"Jira verification failed: {node_output['last_result'].get('error_message')}"

    elif node_name == "request_retry_approval":
        summary = f"HITL decision applied: **{node_output.get('hitl_decision', '').upper()}**."

    elif node_name == "recover_failure":
        plan = node_output.get("recovery_plan", {})
        action = plan.get("action", "stop")
        reason = plan.get("reason") or "No recovery reason provided."
        summary = f"Recovery planner chose **{action}**: {reason}"

    elif node_name == "interpret_rectification":
        plan = node_output.get("rectification_plan", {})
        action = plan.get("action", "retry")
        expl = plan.get("explanation", "")
        summary = f"Rectifier interpreted feedback: **{action}**. {expl}"
        details = plan

    elif node_name == "execute_rectifier_tool":
        plan = node_output.get("recovery_plan", {})
        summary = f"Rectifier executed tool. New question generated for operator."
        details = {"tool_output": node_output.get("tool_output")}

    return summary, details


def _jira_status_name(issue: dict) -> str:
    return str(issue.get("fields", {}).get("status", {}).get("name") or "unknown")


def _run_with_generator(
    graph,
    payload: Any,
    config: dict,
    is_resume: bool = False,
    planning_summary: Optional[str] = None,
    planning_details: Optional[dict] = None,
):
    """
    Generator yielding Server-Sent Events (SSE) for LangGraph execution.
    """
    thread_id = config["configurable"]["thread_id"]
    yield f"event: start\ndata: {_serialize_for_sse({'thread_id': thread_id})}\n\n"

    if planning_summary:
        yield f"event: step\ndata: {_serialize_for_sse({
            'thread_id': thread_id,
            'subgraph': 'agent',
            'node': 'agent_plan',
            'summary': planning_summary,
            'details': planning_details or {},
        })}\n\n"

    try:
        stream_kwargs = {
            "config": config,
            "subgraphs": True,
            "stream_mode": "updates"
        }
        stream_target = Command(resume=payload) if is_resume else payload

        for subgraph_path, chunk in graph.stream(stream_target, **stream_kwargs):
            # Check for interrupt
            if "__interrupt__" in chunk:
                interrupt_objs = chunk["__interrupt__"]
                raw_val = interrupt_objs[0].value if interrupt_objs else {}
                yield f"event: interrupt\ndata: {_serialize_for_sse({'thread_id': thread_id, 'interrupt': raw_val})}\n\n"
                continue

            for node_name, node_output in chunk.items():
                summary, details = _extract_reasoning_and_steps(subgraph_path, node_name, node_output)
                step_evt = {
                    "thread_id": thread_id,
                    "subgraph": ":".join(subgraph_path) if subgraph_path else "orchestrator",
                    "node": node_name,
                    "summary": summary,
                    "details": details,
                }
                yield f"event: step\ndata: {_serialize_for_sse(step_evt)}\n\n"

        # Check final state after stream finishes
        state_snapshot = graph.get_state(config)
        if state_snapshot and state_snapshot.values:
            values = state_snapshot.values
            last_res = values.get("last_result")
            is_paused = bool(state_snapshot.next) and ("request_retry_approval" in state_snapshot.next)
            completed_evt = {
                "thread_id": thread_id,
                "attempt": values.get("attempt"),
                "is_paused": is_paused,
                "last_result": last_res,
                "history": values.get("history", []),
            }
            yield f"event: done\ndata: {_serialize_for_sse(completed_evt)}\n\n"
        else:
            yield f"event: done\ndata: {_serialize_for_sse({'thread_id': thread_id, 'is_paused': False})}\n\n"

    except Exception as exc:
        print(f"\n[ERROR:SERVER-STREAM] Execution error: {type(exc).__name__}: {exc}\n")
        yield f"event: error\ndata: {_serialize_for_sse({'error': str(exc), 'type': type(exc).__name__})}\n\n"


@app.post("/api/run")
async def start_run(req: StartRunRequest):
    if req.workflow not in WORKFLOW_REGISTRY:
        raise HTTPException(status_code=400, detail=f"Unknown workflow: {req.workflow}")

    thread_id = uuid.uuid4().hex[:8]
    config = {"configurable": {"thread_id": thread_id}}
    graph = _graph_for_workflow(req.workflow)
    initial_state = {
        "workflow_name": req.workflow,
        "workflow_input": req.input_data,
        "attempt": 0,
        "max_attempts": req.max_attempts,
        "last_result": None,
        "rectification": None,
        "hitl_decision": None,
        "history": [],
    }

    # Setup mock if requested
    orig_get_llm = llm.get_llm
    orig_jira_client = workflows.jira.JiraClient
    if req.mock_mode:
        from smoke_test import fake_get_llm
        from workflows.simple import AmountExtraction
        from workflows.medium import ReviewVerdict
        from workflows.complex import BatchDecision
        from workflows.jira import TransitionChoice

        if req.workflow == "simple":
            has_rect = bool(req.input_data.get("rectification"))
            plan = [
                AmountExtraction(amount=1200.50 if has_rect else None, confidence="high" if has_rect else "low")
            ]
            if not has_rect:
                plan.append(RecoveryDecision(
                    action="retry_with_correction",
                    resume_node="parse",
                    rectification="Extract amount 1200.50 for the demo retry.",
                    reason="The demo mode supplies a deterministic correction for the retry.",
                ))
        elif req.workflow == "medium":
            user_id = str(req.input_data.get("user_id") or "")
            if user_id == "u2":
                plan = [RecoveryDecision(
                    action="retry_with_correction",
                    resume_node="validate",
                    rectification="support-contact@example.com",
                    reason="The demo record is missing an email and has a known correction.",
                )]
            elif user_id == "does-not-exist":
                plan = [RecoveryDecision(
                    action="stop",
                    resume_node="fetch",
                    reason="The demo user record does not exist.",
                )]
            else:
                plan = [
                    "Welcome to the platform, user.",
                    ReviewVerdict(passes=True, reason="All rubric rules strictly satisfied."),
                ]
        elif req.workflow == "jira":
            target = str(req.input_data.get("target_status") or "Approved").casefold()
            transition_id = {
                "approved": "approve",
                "rejected": "reject",
                "pending": "pending",
            }.get(target, "approve")
            if req.input_data.get("simulate_failure_at") == "B_transition":
                plan = [RecoveryDecision(
                    action="retry_same",
                    resume_node="B_transition",
                    reason="The demo failure occurred before the Jira mutation.",
                )]
            else:
                plan = [
                    TransitionChoice(
                        action="transition",
                        transition_id=transition_id,
                        reason="Demo planner selected a listed transition.",
                    )
                ]
            workflows.jira.JiraClient = _DemoJiraClient
        else:
            plan = [
                RecoveryDecision(
                    action="retry_same",
                    resume_node="sync_external",
                    tool_name="sync_external",
                    reason="The demo dependency timeout is safe to retry.",
                ),
                BatchDecision(action="drop_invalid", explanation="Reviewer authorized dropping bad rows.")
            ]
        llm.get_llm = fake_get_llm(plan)

    def stream_wrapper():
        try:
            for chunk in _run_with_generator(
                graph,
                initial_state,
                config,
                is_resume=False,
                planning_summary=req.planning_summary,
                planning_details=req.planning_details,
            ):
                yield chunk
        finally:
            if req.mock_mode:
                llm.get_llm = orig_get_llm
                workflows.jira.JiraClient = orig_jira_client

    return StreamingResponse(stream_wrapper(), media_type="text/event-stream")


class ChatRequest(BaseModel):
    message: str = Field(..., description="User prompt or intent")
    mock_mode: bool = Field(default=False)
    max_attempts: int = Field(default=5)


def _intent_prompt(user_msg: str) -> str:
    return (
        "You are the routing agent for a workflow orchestrator. Read the user's full "
        "request and choose the one workflow that should execute it. Extract only "
        "values explicitly present or unambiguously implied by the request. Do not "
        "invent a Jira issue key, status, user id, or transaction records.\n\n"
        "Workflow meanings:\n"
        "- simple: extract one monetary amount from messy text.\n"
        "- medium: fetch a user and draft/review a welcome message.\n"
        "- complex: process a transaction batch and run its quality gate.\n"
        "- jira: inspect a Jira issue, choose a legal transition, apply it, and verify it.\n\n"
        "For Jira, copy the exact issue key and requested destination status. Set "
        "simulate_failure_at only when the user explicitly asks to simulate failure at "
        "step B. Return one short routing reason; do not reveal hidden chain-of-thought.\n\n"
        f"User request:\n{user_msg}"
    )


def _plan_chat_intent(user_msg: str) -> ChatIntent:
    return llm.invoke_structured(
        ChatIntent,
        _intent_prompt(user_msg),
        max_tokens=512,
    )


def _model_details(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _build_input_from_intent(intent: ChatIntent, user_msg: str):
    if intent.workflow == "jira":
        return "jira", {
            "issue_key": (intent.issue_key or "").strip().upper(),
            "target_status": intent.target_status.strip() if intent.target_status else None,
            "instruction": user_msg,
            "simulate_failure_at": intent.simulate_failure_at,
        }

    if intent.workflow == "medium":
        return "medium", {"user_id": (intent.user_id or "").strip()}

    if intent.workflow == "complex":
        batch = intent.batch
        if batch is None:
            batch = [
                {"id": 1, "amount": "100.00"},
                {"id": 2, "amount": "250.5"},
                {"id": 3, "amount": "NOT_A_NUMBER"},
                {"id": 4, "amount": "75"},
            ]
        return "complex", {
            "batch": batch,
            "min_quality": intent.min_quality if intent.min_quality is not None else 0.9,
        }

    return "simple", {"raw_amount": intent.raw_amount or user_msg}


def _classify_and_build_input(user_msg: str):
    """
    Intelligently infer target workflow and initial parameters from user's conversational message.
    """
    msg_lower = user_msg.lower().strip()

    issue_match = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", user_msg, re.IGNORECASE)
    if issue_match or any(word in msg_lower for word in ("jira", "transition", "ticket", "issue")):
        target_match = re.search(
            r"\b(?:to|into|back to)\s+(pending|approved|rejected)\b",
            msg_lower,
        )
        if not target_match:
            statuses = re.findall(r"\b(pending|approved|rejected)\b", msg_lower)
            target_status = statuses[-1].title() if statuses else None
        else:
            target_status = target_match.group(1).title()
        simulate_failure = bool(
            re.search(r"(?:simulate|force|inject).*(?:step\s*)?b|fail.*step\s*b", msg_lower)
        )
        return "jira", {
            "issue_key": issue_match.group(1).upper() if issue_match else "",
            "target_status": target_status,
            "instruction": user_msg,
            "simulate_failure_at": "B_transition" if simulate_failure else None,
        }
    
    # 1. Batch / ingestion keywords
    if any(k in msg_lower for k in ["batch", "ingest", "records", "sync", "rows", "csv"]):
        # Default sample batch
        return "complex", {
            "batch": [
                {"id": 1, "amount": "100.00"},
                {"id": 2, "amount": "250.5"},
                {"id": 3, "amount": "NOT_A_NUMBER"},
                {"id": 4, "amount": "75"}
            ],
            "min_quality": 0.9
        }

    # 2. User welcome / editorial review / user lookup keywords
    if any(k in msg_lower for k in ["user", "welcome", "draft", "editorial", "email", "u1", "u2"]):
        uid = "u2" if ("u2" in msg_lower or "missing" in msg_lower) else ("does-not-exist" if "fatal" in msg_lower or "unknown" in msg_lower else "u1")
        return "medium", {"user_id": uid}

    # 3. Default to simple amount extraction
    return "simple", {"raw_amount": user_msg}


@app.post("/api/chat")
async def chat_start(req: ChatRequest):
    try:
        if req.mock_mode:
            workflow, input_data = _classify_and_build_input(req.message)
            planning_summary = f"Demo planner routed request to **{workflow}**."
            planning_details = {"source": "demo", "workflow": workflow}
        else:
            intent = await asyncio.to_thread(_plan_chat_intent, req.message)
            workflow, input_data = _build_input_from_intent(intent, req.message)
            reason = intent.reason.strip() or "The request matches this workflow."
            planning_summary = f"Nemotron routed request to **{workflow}**. {reason}"
            planning_details = {"source": "Nemotron", "decision": _model_details(intent)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Nemotron routing failed: {exc}") from exc

    start_req = StartRunRequest(
        workflow=workflow,
        input_data=input_data,
        max_attempts=req.max_attempts,
        mock_mode=req.mock_mode,
        planning_summary=planning_summary,
        planning_details=planning_details,
    )
    return await start_run(start_req)


@app.post("/api/resume")
async def resume_run(req: ResumeRunRequest):
    config = {"configurable": {"thread_id": req.thread_id}}
    graph, state_snapshot = _graph_and_snapshot_for_thread(req.thread_id)
    if not state_snapshot or not state_snapshot.values:
        raise HTTPException(status_code=404, detail=f"No run found for thread_id {req.thread_id}")

    wf_name = state_snapshot.values.get("workflow_name")
    failed_step = state_snapshot.values.get("failed_step")

    # Demo mode may use deterministic corrections; live mode must not invent user data.
    rectification = req.rectification
    if req.mock_mode and req.decision == "approve" and not rectification:
        if wf_name == "complex" and failed_step == "quality_gate":
            rectification = "Drop invalid records and proceed with valid ones"

    resume_payload = {
        "decision": req.decision,
        "rectification": rectification,
    }

    orig_get_llm = llm.get_llm
    orig_jira_client = workflows.jira.JiraClient
    if req.mock_mode:
        from smoke_test import fake_get_llm
        from workflows.simple import AmountExtraction
        from workflows.medium import ReviewVerdict
        from workflows.complex import BatchDecision
        from workflows.jira import TransitionChoice

        if wf_name == "simple":
            plan = [AmountExtraction(amount=1200.50, confidence="high")]
        elif wf_name == "medium":
            plan = [
                "Welcome to our service.",
                ReviewVerdict(passes=True, reason="Revision note applied successfully."),
            ]
        elif wf_name == "jira":
            if failed_step == "C_verify":
                plan = []
            else:
                target = str(
                    state_snapshot.values.get("target_status")
                    or state_snapshot.values.get("workflow_input", {}).get("target_status")
                    or "Approved"
                ).casefold()
                transition_id = {
                    "approved": "approve",
                    "rejected": "reject",
                    "pending": "pending",
                }.get(target, "approve")
                plan = [
                    TransitionChoice(
                        action="transition",
                        transition_id=transition_id,
                        reason="Demo planner selected a listed recovery transition.",
                    )
                ]
            workflows.jira.JiraClient = _DemoJiraClient
        else:
            attempt = int(state_snapshot.values.get("attempt") or 0)
            if failed_step == "sync_external" and attempt < 2:
                plan = [RecoveryDecision(
                    action="retry_same",
                    resume_node="sync_external",
                    tool_name="sync_external",
                    reason="The demo dependency timeout is safe to retry.",
                )]
            elif failed_step == "sync_external":
                plan = [RecoveryDecision(
                    action="ask_user",
                    resume_node="quality_gate",
                    question="Authorize dropping invalid records to continue.",
                    reason="The dependency recovered; the remaining failure needs a quality decision.",
                )]
            else:
                plan = [BatchDecision(action="drop_invalid", explanation="Dropping corrupted records authorized.")]
        llm.get_llm = fake_get_llm(plan)

    def stream_wrapper():
        try:
            for chunk in _run_with_generator(graph, resume_payload, config, is_resume=True):
                yield chunk
        finally:
            if req.mock_mode:
                llm.get_llm = orig_get_llm
                workflows.jira.JiraClient = orig_jira_client

    return StreamingResponse(stream_wrapper(), media_type="text/event-stream")


@app.get("/api/status/{thread_id}")
def get_run_status(thread_id: str):
    config = {"configurable": {"thread_id": thread_id}}
    _graph, snapshot = _graph_and_snapshot_for_thread(thread_id)
    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail=f"Thread '{thread_id}' not found.")

    is_paused = bool(snapshot.next) and ("request_retry_approval" in snapshot.next)
    return {
        "thread_id": thread_id,
        "next": snapshot.next,
        "is_paused": is_paused,
        "attempt": snapshot.values.get("attempt"),
        "workflow_name": snapshot.values.get("workflow_name"),
        "workflow_input": snapshot.values.get("workflow_input"),
        "last_result": snapshot.values.get("last_result"),
        "history": snapshot.values.get("history", []),
    }


# Serve static single page application
STATIC_DIR = os.path.join(CURRENT_DIR, "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def serve_index():
    index_file = os.path.join(STATIC_DIR, "chat.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>LangGraph Orchestrator Frontend: static/chat.html not found.</h1>")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
