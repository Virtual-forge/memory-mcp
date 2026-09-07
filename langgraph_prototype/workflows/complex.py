"""
Workflow 3 - complex: ingest_batch

Five nodes: extract -> transform -> sync_external -> quality_gate -> commit.

  extract, transform   deterministic. Parsing a number doesn't need a
                        model, and keeping it in code makes failures here
                        reproducible.
  sync_external        simulated flaky dependency -- fails the first two
                        attempts regardless of input. Nothing to fix,
                        just retry.
    quality_gate         if too many records are invalid, a real Luna
                        call reads whatever free-text instruction the
                        human gave (not just an exact keyword) and
                        *decides* whether that authorizes dropping the
                        bad records. This is a small preview of the
                        human decision eventually becoming an agent
                        decision: the model already interprets intent,
                        a human is just still the one who has to say "go".
  commit                "writes" the surviving records.

Try the batch below (needs 2 plain retries past sync_external, then any
free-text go-ahead like "yes drop the bad ones" at the quality gate), or
an all-bad batch to see the FATAL give-up path.
"""

from typing import TypedDict, Optional, Literal

from langgraph.graph import StateGraph, START, END
from pydantic import BaseModel, Field

import llm
from state import ok, fail


class BatchDecision(BaseModel):
    action: Literal["drop_invalid", "cannot_apply"] = Field(
        description="drop_invalid if the instruction clearly authorizes proceeding without the "
                    "bad records; cannot_apply if it doesn't say that.",
    )
    explanation: str = Field(description="One sentence justifying the action.")


class IngestBatchState(TypedDict, total=False):
    batch: list
    min_quality: float
    attempt: int
    rectification: Optional[str]
    records: list
    failed_step: str
    resume_node: Optional[str]
    recovery_plan: dict
    workflow_context: dict
    result: dict


def extract_node(state: IngestBatchState) -> dict:
    return {"batch": state["batch"]}


def transform_node(state: IngestBatchState) -> dict:
    records = []
    for r in state["batch"]:
        try:
            amount = float(r["amount"])
            records.append({**r, "amount": amount, "valid": True})
        except (TypeError, ValueError):
            records.append({**r, "amount": None, "valid": False,
                             "issue": f"unparseable amount '{r['amount']}'"})
    return {"records": records}


def sync_external_node(state: IngestBatchState) -> dict:
    if state.get("attempt", 0) < 2:
        return {
            "result": fail(
                "TRANSIENT",
                "External pricing service timed out (simulated flaky dependency).",
                retry=True,
            ),
            "failed_step": "sync_external",
        }
    return {}


def route_after_sync(state: IngestBatchState) -> str:
    return "failed" if state.get("result") else "continue"


def route_at_start(state: IngestBatchState) -> str:
    resume_node = state.get("resume_node")
    if resume_node in {"transform", "sync_external", "quality_gate", "commit"} and state.get("workflow_context"):
        return resume_node
    return "extract"


def quality_gate_node(state: IngestBatchState) -> dict:
    records = state["records"]
    total = len(records)
    invalid = [r for r in records if not r["valid"]]
    invalid_ratio = (len(invalid) / total) if total else 0
    min_quality = state.get("min_quality", 0.8)

    if invalid_ratio <= (1 - min_quality):
        return {"records": records}

    if len(invalid) == total:
        return {
            "result": fail(
                "FATAL",
                "Every record in the batch failed validation; nothing to salvage.",
                retry=False,
            ),
            "failed_step": "quality_gate",
        }

    issues = "; ".join(r["issue"] for r in invalid)

    if not state.get("rectification"):
        return {
            "result": fail(
                "VALIDATION",
                f"{len(invalid)}/{total} records are invalid ({issues}). "
                "Tell the reviewer how to proceed (e.g. authorize dropping them).",
                retry=True,
            ),
            "failed_step": "quality_gate",
        }

    prompt = (
        f"A batch has {len(invalid)}/{total} invalid records ({issues}). "
        f'A human reviewer said: "{state["rectification"]}". Decide what to do.'
    )
    decision = llm.invoke_structured(BatchDecision, prompt)
    if decision.action == "drop_invalid":
        return {"records": [r for r in records if r["valid"]]}
    return {
        "result": fail(
            "VALIDATION",
            f"Reviewer instruction couldn't be applied: {decision.explanation}",
            retry=True,
        ),
        "failed_step": "quality_gate",
    }


def route_after_quality(state: IngestBatchState) -> str:
    return "failed" if state.get("result") else "continue"


def commit_node(state: IngestBatchState) -> dict:
    return {"result": ok(payload={"committed": len(state["records"])})}


def build():
    builder = StateGraph(IngestBatchState)
    builder.add_node("extract", extract_node)
    builder.add_node("transform", transform_node)
    builder.add_node("sync_external", sync_external_node)
    builder.add_node("quality_gate", quality_gate_node)
    builder.add_node("commit", commit_node)

    builder.add_conditional_edges(
        START,
        route_at_start,
        {
            "extract": "extract",
            "transform": "transform",
            "sync_external": "sync_external",
            "quality_gate": "quality_gate",
            "commit": "commit",
        },
    )
    builder.add_edge("extract", "transform")
    builder.add_edge("transform", "sync_external")
    builder.add_conditional_edges("sync_external", route_after_sync,
                                   {"failed": END, "continue": "quality_gate"})
    builder.add_conditional_edges("quality_gate", route_after_quality,
                                   {"failed": END, "continue": "commit"})
    builder.add_edge("commit", END)
    return builder.compile()
