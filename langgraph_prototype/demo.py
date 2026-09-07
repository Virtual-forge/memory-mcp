"""
Runs all three workflows against the real Nemotron endpoint, with a
scripted "human" standing in for you so you can see the whole
fail -> pause -> decide -> retry loop without typing anything.

Needs NVIDIA_API_KEY set in .env.

Unlike a fully deterministic simulation, outcomes here can vary run to
run -- Nemotron might resolve something on the first try that failed
last time, or vice versa. That's real model behavior, not a bug. If a
run doesn't pause where you expected, that's worth noticing, not worth
"fixing" by re-scripting until it matches.

For a free, instant check that the graph wiring itself is correct
(no API key, no network, no real model), run smoke_test.py instead.
"""

import json
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from orchestrator import build_orchestrator
import workflows  # noqa: F401 -- populates the registry


def run_scripted(thread_id, workflow_name, workflow_input, max_attempts, script, checkpointer):
    graph = build_orchestrator(checkpointer)
    config = {"configurable": {"thread_id": thread_id}}

    print(f"\n{'=' * 72}\nWORKFLOW: {workflow_name}   (thread_id={thread_id})\n{'=' * 72}")
    print(f"input: {json.dumps(workflow_input)}")

    out = graph.invoke({
        "workflow_name": workflow_name,
        "workflow_input": workflow_input,
        "attempt": 0,
        "max_attempts": max_attempts,
        "last_result": None,
        "rectification": None,
        "hitl_decision": None,
        "history": [],
    }, config)

    step = 0
    while "__interrupt__" in out:
        payload = out["__interrupt__"][0].value
        print(f"\n-- attempt {payload['attempt']} failed --")
        print(f"   error_status : {payload['error_status']}")
        print(f"   error_message: {payload['error_message']}")

        # Reuse the last scripted decision if the model needed more
        # attempts than expected -- see the module docstring.
        decision = script[min(step, len(script) - 1)] if script else {"decision": "reject"}
        step += 1
        print(f"   >> human decision: {decision}")

        out = graph.invoke(Command(resume=decision), config)

    result = out["last_result"]
    verdict = "SUCCEEDED" if result["status"] == "success" else "GAVE UP"
    print(f"\n-- {verdict} after {out['attempt']} attempt(s) --")
    print(json.dumps(result, indent=2))
    return out


if __name__ == "__main__":
    checkpointer = InMemorySaver()

    # 1. Simple: text with no determinable amount, fixed with a direct rectification.
    run_scripted(
        thread_id="demo-simple-1",
        workflow_name="simple",
        workflow_input={"raw_amount": "he mentioned a price but I didn't really catch it, sorry"},
        max_attempts=3,
        script=[{"decision": "approve", "rectification": "it was 1200.50"}],
        checkpointer=checkpointer,
    )

    # 2. Medium: missing field, then a possible style rewrite of the draft.
    run_scripted(
        thread_id="demo-medium-1",
        workflow_name="medium",
        workflow_input={"user_id": "u2"},
        max_attempts=4,
        script=[
            {"decision": "approve", "rectification": "youssef@example.com"},
            {"decision": "approve", "rectification": "shorter, and no exclamation points"},
        ],
        checkpointer=checkpointer,
    )

    # 2b. Medium: unknown id -- FATAL, non-retryable, no LLM call ever made.
    run_scripted(
        thread_id="demo-medium-2",
        workflow_name="medium",
        workflow_input={"user_id": "does-not-exist"},
        max_attempts=3,
        script=[],
        checkpointer=checkpointer,
    )

    # 3. Complex: two plain retries past a flaky dependency, then a
    #    free-text go-ahead that Luna has to interpret at the quality gate.
    run_scripted(
        thread_id="demo-complex-1",
        workflow_name="complex",
        workflow_input={
            "batch": [
                {"id": 1, "amount": "100.00"},
                {"id": 2, "amount": "250.5"},
                {"id": 3, "amount": "NOT_A_NUMBER"},
                {"id": 4, "amount": "75"},
            ],
            "min_quality": 0.9,
        },
        max_attempts=5,
        script=[
            {"decision": "approve"},
            {"decision": "approve"},
            {"decision": "approve", "rectification": "yeah just go ahead and drop the bad ones"},
        ],
        checkpointer=checkpointer,
    )

    # 3b. Complex: an all-bad batch -- FATAL once it reaches the quality
    #     gate, since nothing can be salvaged no matter what the human says.
    run_scripted(
        thread_id="demo-complex-2",
        workflow_name="complex",
        workflow_input={
            "batch": [
                {"id": 1, "amount": "oops"},
                {"id": 2, "amount": "nope"},
            ],
            "min_quality": 0.9,
        },
        max_attempts=5,
        script=[
            {"decision": "approve"},
            {"decision": "approve"},
            {"decision": "approve", "rectification": "drop the bad ones and proceed"},
        ],
        checkpointer=checkpointer,
    )
