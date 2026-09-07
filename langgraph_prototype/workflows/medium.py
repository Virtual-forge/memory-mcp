"""
Workflow 2 - medium: draft_welcome_message

Four nodes: fetch -> validate -> draft -> review.

  fetch     looks the user up in a fake "database". Unknown id -> FATAL,
            non-retryable, no approval asked.
  validate  requires an email on the record. Missing -> VALIDATION,
            retryable; the rectification is used as the missing email.
    draft     a real Luna call writes a short welcome message. Once
            past validate, a rectification is treated as a style note
            (e.g. "less salesy") and folded into the prompt.
    review    a second Luna call grades the draft against a rubric
            with structured output. If it fails, the reviewer's own
            reason becomes the error_message shown to the human.

Try it with user_id="u2" (missing email) or "does-not-exist" (fatal).
The review step can fail on its own too, purely from model output --
that's real variance, not a bug.
"""

from typing import TypedDict, Optional

from langgraph.graph import StateGraph, START, END
from pydantic import BaseModel, Field

import llm
from state import ok, fail

_FAKE_DB = {
    "u1": {"user_id": "u1", "name": "Amel", "email": "amel@example.com"},
    "u2": {"user_id": "u2", "name": "Youssef"},  # missing email on purpose
}


class ReviewVerdict(BaseModel):
    passes: bool = Field(description="True only if the message satisfies every rule.")
    reason: str = Field(description="One sentence: why it passes, or exactly which rule it breaks.")


class EnrichUserState(TypedDict, total=False):
    user_id: str
    attempt: int
    rectification: Optional[str]
    record: dict
    draft: str
    failed_step: str
    resume_node: Optional[str]
    recovery_plan: dict
    workflow_context: dict
    result: dict


def fetch_node(state: EnrichUserState) -> dict:
    record = _FAKE_DB.get(state["user_id"])
    if record is None:
        return {
            "result": fail("FATAL", f"No user record for id '{state['user_id']}'.", retry=False),
            "failed_step": "fetch",
        }
    return {"record": dict(record)}


def route_after_fetch(state: EnrichUserState) -> str:
    return "failed" if state.get("result") else "continue"


def validate_node(state: EnrichUserState) -> dict:
    record = dict(state["record"])
    if not record.get("email"):
        if state.get("rectification"):
            record["email"] = state["rectification"]
        else:
            return {
                "result": fail(
                    "VALIDATION",
                    f"Record '{record['user_id']}' is missing a required field: email.",
                    retry=True,
                ),
                "failed_step": "validate",
            }
    return {"record": record}


def route_after_validate(state: EnrichUserState) -> str:
    return "failed" if state.get("result") else "continue"


def route_at_start(state: EnrichUserState) -> str:
    resume_node = state.get("resume_node")
    if resume_node in {"validate", "draft", "review"} and state.get("workflow_context"):
        return resume_node
    return "fetch"


def draft_node(state: EnrichUserState) -> dict:
    record = state["record"]
    style_hint = ""
    # A rectification here (record already has an email) is a style note, not an email fix.
    if state.get("rectification") and "@" not in state["rectification"]:
        style_hint = f"\n\nRevision note from a human reviewer: {state['rectification']}"
    prompt = (
        f"Write a short welcome message (under 60 words) for {record['name']}. "
        "Friendly but not salesy, no exclamation points, mention their name exactly once."
        + style_hint
    )
    draft = llm.get_llm().invoke(prompt).content
    return {"draft": draft}


def review_node(state: EnrichUserState) -> dict:
    record = state["record"]
    prompt = (
        "You are a strict editor. A message passes only if it: "
        f"(1) is under 60 words, (2) mentions the name '{record['name']}' exactly once, "
        "(3) contains no exclamation points, (4) does not sound like a sales pitch.\n\n"
        f"Message:\n{state['draft']}"
    )
    verdict = llm.invoke_structured(ReviewVerdict, prompt)
    if not verdict.passes:
        return {
            "result": fail("VALIDATION", f"Draft rejected by reviewer: {verdict.reason}", retry=True),
            "failed_step": "review",
        }
    record = dict(record)
    record["message"] = state["draft"]
    return {"result": ok(payload=record)}


def build():
    builder = StateGraph(EnrichUserState)
    builder.add_node("fetch", fetch_node)
    builder.add_node("validate", validate_node)
    builder.add_node("draft", draft_node)
    builder.add_node("review", review_node)

    builder.add_conditional_edges(
        START,
        route_at_start,
        {"fetch": "fetch", "validate": "validate", "draft": "draft", "review": "review"},
    )
    builder.add_conditional_edges("fetch", route_after_fetch, {"failed": END, "continue": "validate"})
    builder.add_conditional_edges("validate", route_after_validate, {"failed": END, "continue": "draft"})
    builder.add_edge("draft", "review")
    builder.add_edge("review", END)
    return builder.compile()
