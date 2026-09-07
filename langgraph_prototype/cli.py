"""
Interactive CLI.

    Set NVIDIA_API_KEY in .env before running.

  # start a run
  python cli.py start simple --input '{"raw_amount": "he mentioned a price but I did not catch it"}'
  ... prints a thread_id and, if it pauses, the failure details ...

  # resume it (can be a different terminal / later time -- state is on disk)
  python cli.py resume <thread_id> --decision approve --rectification "it was 1200.50"
  python cli.py resume <thread_id> --decision reject

  # check on a run without resuming it
  python cli.py status <thread_id>

Available workflows: simple, medium, complex, jira (see workflows/*.py for what
each one does and how to trigger/fix its failures).
"""

import argparse
import json
import uuid

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from orchestrator import build_orchestrator
import workflows  # noqa: F401 -- populates the registry

DB_PATH = "orchestrator_state.db"


def handle_output(out, thread_id):
    if out is None:
        print(f"No state found for thread_id '{thread_id}'.")
        return
    if "__interrupt__" in out:
        payload = out["__interrupt__"][0].value
        print(f"\n[PAUSED]  thread_id={thread_id}")
        print(json.dumps(payload, indent=2))
        print("\nResume with one of:")
        print(f'  python cli.py resume {thread_id} --decision approve --rectification "..."')
        print(f"  python cli.py resume {thread_id} --decision reject")
    else:
        result = out["last_result"]
        verdict = "SUCCEEDED" if result["status"] == "success" else "GAVE UP"
        print(f"\n[{verdict}]  thread_id={thread_id}  attempts={out['attempt']}")
        print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="start a new workflow run")
    p_start.add_argument("workflow", choices=["simple", "medium", "complex", "jira"])
    p_start.add_argument("--input", required=True, help='JSON dict, e.g. \'{"user_id": "u2"}\'')
    p_start.add_argument("--max-attempts", type=int, default=5)

    p_resume = sub.add_parser("resume", help="resume a paused run")
    p_resume.add_argument("thread_id")
    p_resume.add_argument("--decision", choices=["approve", "reject"], required=True)
    p_resume.add_argument("--rectification", default=None)

    p_status = sub.add_parser("status", help="show the current state of a run without resuming it")
    p_status.add_argument("thread_id")

    args = parser.parse_args()

    with SqliteSaver.from_conn_string(DB_PATH) as checkpointer:
        graph = build_orchestrator(checkpointer)

        if args.cmd == "start":
            thread_id = uuid.uuid4().hex[:8]
            config = {"configurable": {"thread_id": thread_id}}
            out = graph.invoke({
                "workflow_name": args.workflow,
                "workflow_input": json.loads(args.input),
                "attempt": 0,
                "max_attempts": args.max_attempts,
                "last_result": None,
                "rectification": None,
                "hitl_decision": None,
                "history": [],
            }, config)
            handle_output(out, thread_id)

        elif args.cmd == "resume":
            config = {"configurable": {"thread_id": args.thread_id}}
            resume_payload = {"decision": args.decision, "rectification": args.rectification}
            snapshot = graph.get_state(config)
            if not snapshot or not snapshot.values:
                print(f"No state found for thread_id '{args.thread_id}'.")
                return
            out = graph.invoke(Command(resume=resume_payload), config)
            handle_output(out, args.thread_id)

        elif args.cmd == "status":
            config = {"configurable": {"thread_id": args.thread_id}}
            snapshot = graph.get_state(config)
            if not snapshot or not snapshot.values:
                print(f"No state found for thread_id '{args.thread_id}'.")
                return
            print(json.dumps({
                "next": snapshot.next,
                "attempt": snapshot.values.get("attempt"),
                "last_result": snapshot.values.get("last_result"),
            }, indent=2))


if __name__ == "__main__":
    main()
