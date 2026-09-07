"""
Workflow 1 - simple: extract_amount

One node, one real LLM call. Luna reads a (possibly messy) piece of
text and tries to extract a single monetary amount as structured output.
If it isn't confident, that's a VALIDATION failure (retryable) -- the
human's rectification is folded into the prompt as a correction hint on
the next attempt.

Try it with something genuinely ambiguous -- a plain typo like
"$1,20O.50" is often something a strong model just fixes on its own, so
it may not fail at all. For a reliable failure, use text with no
determinable number, e.g.:
  "raw_amount": "he mentioned a price but I didn't really catch it, sorry"
"""

from typing import TypedDict, Optional, Literal

from langgraph.graph import StateGraph, START, END
from pydantic import BaseModel, Field

import llm
from state import ok, fail


class AmountExtraction(BaseModel):
    amount: Optional[float] = Field(
        default=None,
        description="The single numeric monetary amount found in the text, with no currency "
                    "symbol or thousands separators. Null if none can be confidently determined.",
    )
    confidence: Literal["high", "low"] = Field(
        description="'low' if the text is ambiguous, contradictory, or contains something you "
                    "cannot resolve with certainty; 'high' otherwise.",
    )


class ParseAmountState(TypedDict, total=False):
    raw_amount: str
    attempt: int
    rectification: Optional[str]
    failed_step: str
    resume_node: Optional[str]
    recovery_plan: dict
    workflow_context: dict
    result: dict


def parse_node(state: ParseAmountState) -> dict:
    hint = f"\n\nCorrection from a human reviewer: {state['rectification']}" if state.get("rectification") else ""
    prompt = (
        "Extract the single monetary amount mentioned in the text below. "
        "If no definite amount can be determined, say so.\n\n"
        f"Text: {state['raw_amount']}{hint}"
    )
    extraction = llm.invoke_structured(AmountExtraction, prompt)

    if extraction.amount is None or extraction.confidence == "low":
        return {
            "result": fail(
                "VALIDATION",
                f"Luna could not confidently extract an amount from '{state['raw_amount']}'.",
                retry=True,
            ),
            "failed_step": "parse",
        }
    return {"result": ok(payload={"amount": extraction.amount})}


def build():
    builder = StateGraph(ParseAmountState)
    builder.add_node("parse", parse_node)
    builder.add_edge(START, "parse")
    builder.add_edge("parse", END)
    return builder.compile()
