# LangGraph orchestrator prototype (Nemotron-backed)

Autonomous workflows with an LLM-planned, HITL-gated recovery loop, matching the architecture
in `langgraph_orchestrator_architecture.pdf`. `orchestrator.py` and
`state.py` own orchestration; `recovery.py` owns the separate RecoveryAgent;
everything in `workflows/` is a pure workflow subgraph plugged into it via
`register_workflow`. Each workflow returns a failure envelope; only the
orchestrator asks the RecoveryAgent for a plan, shows it to the user, and
resumes after approval.

## Setup

```
pip install -r requirements.txt
```

The project uses the [NVIDIA AI Endpoints](https://python.langchain.com/docs/integrations/chat/nvidia_ai_endpoints/)
LangChain integration. Set your NVIDIA API key in `langgraph_prototype/.env`:

```
NVIDIA_API_KEY=your-nvidia-api-key
NVIDIA_MODEL=nvidia/nemotron-3-super-120b-a12b
```

`NVIDIA_MODEL` defaults to `nvidia/nemotron-3-super-120b-a12b` if omitted.

For the Jira workflow, add these variables to the same `.env` file:

```
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=your-token
# Optional; defaults to 3 for Jira Cloud
JIRA_API_VERSION=3
```

## Check the wiring first (free, no key needed)

```
python smoke_test.py
```

Runs all workflows with a **stubbed** model so you can confirm the
retry loop, envelope contract, and interrupt/resume logic are all wired
correctly before spending a single real API call. If you edit a
workflow's node logic, run this first.

## See it run against the real model

```
python demo.py
```

Runs the original workflows with a scripted "human" standing in for you,
printing each failure, the decision made, and the final outcome.
**Outcomes here can vary run to run** -- Luna might resolve
something on the first try that failed last time, or need an extra
retry it didn't need before. That's real model behavior, not a bug in
the orchestrator.

## Launch Conversational Web UI

Run the conversational workspace with compact execution traces and inline
Yes/No HITL approval cards:

```
python server.py
```

Then open **http://127.0.0.1:8000**.
- Send one natural-language request; the server infers `simple`, `medium`, or `complex`.
- Expand the assistant trace to inspect concise node summaries and state/output details.
- When a failure interrupts the graph, inspect Luna's recovery plan and
   either provide the requested correction, choose **Yes** to approve it, or
   choose **No** to stop. Demo Mode may use deterministic corrections; live
   mode never invents missing user data.
- Before the approval card appears, Luna receives the failed node, error
   status, error message, current checkpoint, and allowlisted tool catalog. It
   returns a bounded recovery plan: retry, refresh and retry, retry with a
   correction, ask for missing information, or stop.
- The Python graph validates that plan. Luna never executes a tool or
   selects an unchecked graph node, and the existing approval gate remains
   before retries with side effects.
- Turn on **Demo mode** for deterministic, zero-cost UI tests.

### Jira transition workflow

The Jira route is a real external workflow, not a scripted status switch:

```
A_inspect     GET issue + legal transitions
B_transition  Luna selects one listed transition and POSTs it
C_verify      GET issue again and confirm the resulting status
```

The orchestrator checkpoints the failed node and workflow context. If B fails
and you approve a retry, the graph resumes at B. It does not repeat A or
mutate the issue twice. If C fails because Jira has not reflected the change
yet, approval resumes at C.

If A returns HTTP 404, the recovery planner treats the issue key as invalid or
inaccessible instead of blindly retrying it. The approval card asks for a
corrected issue key or a permission fix, and the graph resumes at A after the
correction.

With live Jira configured, try:

```
Move ABC-123 to Approved.
```

To test the B checkpoint boundary without making the first attempt mutate Jira,
use Demo mode and send:

```
Move DEMO-1 to Approved and simulate a recoverable failure at step B.
```

Approve the retry. The trace should show `A_inspect` once, `B_transition` on
the failed and resumed attempts, and `C_verify` after the transition succeeds.
The workflow reads available transitions from Jira, so unsupported state
changes fail instead of being invented by the model.

## Run it yourself, interactively

```
python cli.py start medium --input '{"user_id": "u2"}'
```

If it pauses, you'll get a `thread_id` and the failure details. Resume it:

```
python cli.py resume <thread_id> --decision approve --rectification "youssef@example.com"
python cli.py resume <thread_id> --decision reject          # or give up instead
python cli.py status <thread_id>                             # peek without resuming
```

Start the Jira workflow directly with an issue key and target status:

```
python cli.py start jira --input '{"issue_key":"ABC-123","target_status":"Approved","instruction":"Move ABC-123 to Approved."}'
```

State is persisted to `orchestrator_state.db` (SQLite), so you can close
the terminal and resume later, or resume from a different shell entirely.

## The workflows

| Workflow  | Nodes | Model calls | What fails | How to fix it |
|-----------|-------|-----------------|-----------|----------------|
| `simple`  | 1 (`parse`) | 1 extraction call; recovery call when it fails | Text with no determinable amount, e.g. `"he mentioned a price but I didn't catch it"` | Provide the missing amount at approval |
| `medium`  | 4 (`fetch` → `validate` → `draft` → `review`) | 2 workflow calls; recovery call when a step fails | `user_id="u2"` has no email (deterministic); or the reviewer rejects the draft (real model variance) | Email fix at `validate`; a style note like `"less salesy"` at `review` |
| `medium`  | same | Recovery planner evaluates the fatal result, then stops | `user_id="does-not-exist"` | Nothing can fix a missing record without a new user id |
| `complex` | 5 (`extract` → `transform` → `sync_external` → `quality_gate` → `commit`) | 1 quality-gate call; recovery call after each failure | First 2 attempts always fail TRANSIENT (simulated); then too many unparseable amounts in the batch | Approve twice with no rectification, then any free-text go-ahead, e.g. `"yeah drop the bad ones"` |
| `complex` | same | Recovery planner evaluates the fatal result, then stops | An all-bad batch reaches `quality_gate` with nothing salvageable | FATAL; no safe recovery exists |
| `jira` | 3 (`A_inspect` → `B_transition` → `C_verify`) | 1 transition-planning call; recovery call after a failure | Jira rejects an invalid/stale transition, a status read is delayed, or the injected Demo failure occurs | Approve the planner's bounded recovery plan; reject to stop |

Try the complex one yourself:

```
python cli.py start complex --input '{"batch": [{"id":1,"amount":"100.00"},{"id":2,"amount":"250.5"},{"id":3,"amount":"NOT_A_NUMBER"},{"id":4,"amount":"75"}], "min_quality": 0.9}' --max-attempts 5
```

## Why some nodes are still plain code

`extract`/`transform` (parsing numbers) and `sync_external` (a simulated
flaky dependency) don't call the model. An LLM call should earn its
place by doing something that needs judgment -- extracting meaning from
messy text, drafting prose, interpreting a free-text instruction, or
planning recovery -- not replace deterministic logic that's cheaper,
faster, and more predictable as plain code. Recovery decisions are
model-driven and are stubbed in `smoke_test.py`.

## Adding a fourth workflow

1. Add `workflows/your_workflow.py` with a `build()` that returns a
   compiled `StateGraph`. Use the shared `llm.invoke_structured()` helper
   for structured model calls. Every terminal node must set `result` to
   `state.ok(...)` or `state.fail(...)`; do not add a local recovery agent
   or HITL interrupt.
2. Register it in `workflows/__init__.py`.
3. `python cli.py start your_workflow --input '{...}'`

## Swapping HITL for an approval agent later

Everything about the human step lives in `request_retry_approval` in
`orchestrator.py`. Replace its body with a model call that reads the
same `interrupt()` payload shape and returns
`{"decision": ..., "rectification": ...}` -- no other node, edge, or
workflow needs to change. The `quality_gate` node in `complex` is
already a small preview of this: a model interprets the human's intent,
a human just still has to say "go".
