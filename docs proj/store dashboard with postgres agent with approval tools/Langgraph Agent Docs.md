# LangGraph Agent Architecture and Approval Lifecycle

This document describes the current implementation in this repository: how a user request enters the service, how the model selects and executes tools, where approval pauses the run, and how the run resumes after a Jira decision.

The service is a FastAPI application built around a checkpointed LangGraph. It uses a configurable chat model, PostgreSQL for both application data and graph checkpoints, and Jira as the human approval system for destructive SQL operations.

## System at a glance

```text
Client
  |
  | POST /chat
  v
FastAPI service (main.py)
  |
  v
runner.advance_graph()
  |
  v
Checkpointed LangGraph
  |                         \
  | model/tool loop          \ interrupt for destructive SQL
  v                           v
Chat model -> tool dispatch  Jira ticket + approval record
  |                           |
  v                           v
PostgreSQL tool execution   Reviewer -> Jira status
                              |
                    POST continue or POST reject
                              |
                              v
                    Command(resume=...) on same thread
```

The major ownership boundaries are:

| Area | Implementation | Responsibility |
| --- | --- | --- |
| HTTP and WebSocket API | [main.py](../main.py) | Starts the service, accepts chat requests, exposes session and approval endpoints, and pushes resolution events. |
| Graph runtime | [graph_runtime.py](../graph_runtime.py) | Opens the async PostgreSQL checkpointer, creates checkpoint tables, compiles the graph, and exposes the process-local graph. |
| Agent graph | [graph.py](../graph.py) | Calls the model, routes tool calls, detects approval pauses, invokes tools, and loops until a final answer exists. |
| Tools and policy gate | [tools.py](../tools.py) | Defines the SQL/table tools and the `DROP TABLE`/`ALTER TABLE`/`TRUNCATE TABLE` approval detector. |
| Run orchestration | [runner.py](../runner.py) | Invokes or resumes a graph run, translates LangGraph interrupts into API results, and emits WebSocket notifications. |
| Approval bookkeeping | [approvals.py](../approvals.py) | Creates approval IDs, creates Jira tickets, stores the approval-to-thread mapping, and reads Jira status. |
| Jira integration | [jira_client.py](../jira_client.py) | Builds the Jira issue and maps Jira workflow statuses to `pending`, `approved`, or `blocked`. |
| Database setup | [db.py](../db.py) | Creates the local approval table and provides synchronous PostgreSQL connections for tools. |
| Session metadata | [sessions.py](../sessions.py) | Stores session ownership and list metadata. Full chat history remains in LangGraph checkpoints. |
| WebSocket fan-out | [ws_manager.py](../ws_manager.py) | Sends pause and completion events to clients connected to a session. |

## Startup and runtime state

When FastAPI starts, the lifespan in [main.py](../main.py) performs these steps:

1. `ensure_schema()` creates `lg_run_approvals` if it does not exist.
2. `ensure_sessions_table()` creates `lg_sessions` if it does not exist.
3. `compiled_graph()` opens an `AsyncPostgresSaver` connection using `DATABASE_URL`.
4. `AsyncPostgresSaver.setup()` creates or upgrades LangGraph's checkpoint tables.
5. `build_graph().compile(checkpointer=checkpointer)` creates the graph used by all requests.
6. The compiled graph is stored in a process-local singleton until shutdown.
7. On shutdown, the checkpointer connection closes and the singleton is cleared.

The service expects the database to be available during startup. The SQL tools use synchronous `psycopg2` connections, while LangGraph checkpointing uses the async PostgreSQL saver.

Each graph invocation uses this configuration:

```python
{"configurable": {"thread_id": thread_id}}
```

The `thread_id` is the durable identity of a conversation. It selects the checkpoint history and is also stored on approval records and Jira tickets. A resume must use the same thread ID or LangGraph will not find the paused run.

## Architecture and control flow

```mermaid
flowchart TD
    Client[Chat client] -->|POST /chat| API[FastAPI main.py]
    API --> Session[lg_sessions]
    API --> Runner[runner.advance_graph]
    Runner --> Graph[Checkpointed LangGraph]
    Graph --> Agent[agent node]
    Agent --> Model[ChatOpenAI or ChatNVIDIA]
    Model -->|tool calls| Dispatch[tool_dispatch node]
    Dispatch --> Detector[needs_approval]
    Detector -->|safe call| Tool[TOOLS_BY_NAME invoke]
    Tool --> DB[(PostgreSQL)]
    Detector -->|destructive execute_sql| Pause[LangGraph interrupt]
    Pause --> Checkpoint[(LangGraph checkpoints)]
    Runner -->|register approval| Approval[approvals.py]
    Approval --> Jira[Jira issue]
    Approval --> ApprovalDB[(lg_run_approvals)]
    Reviewer[Human reviewer] --> Jira
    Client -->|poll status| Status[GET /api/approvals/id]
    Status --> Jira
    Client -->|continue or reject| Resume[Approval endpoint]
    Resume --> Runner
    Runner -->|Command resume on same thread| Graph
    Resume -->|WebSocket event| Client
```

### The graph nodes

The graph in [graph.py](../graph.py) has two nodes:

- `agent`: Builds the model input, including `SYSTEM_PROMPT` when needed, invokes the bound chat model, and stores the model response plus any `tool_calls` in `pending_tool_calls`.
- `tool_dispatch`: Takes exactly one pending tool call, checks whether it needs approval, invokes it when allowed, and appends a `ToolMessage` to the conversation.

The conditional edges implement the loop:

```text
agent
  -> tool_dispatch when the model requested a tool
  -> END when the model returned no tool call

tool_dispatch
  -> tool_dispatch when more tool calls remain
  -> agent when all current tool calls are complete
```

A single tool call is dispatched per `tool_dispatch` execution. This is important for pause/resume behavior: the code reaches `interrupt()` before invoking a destructive tool, so replaying the paused node does not repeat a side effect that happened before the pause.

### Model and tool binding

At module import time, [graph.py](../graph.py) selects the model from configuration:

- `copilot2api`, `openai`, or `openai-compatible`: `ChatOpenAI` with `MODEL_BASE_URL`.
- `nvidia`: `ChatNVIDIA` with `MODEL_ID`.

The selected model is bound to `TOOLS`:

- `execute_sql(query)` runs SQL against the configured PostgreSQL database.
- `list_tables()` lists tables in the public schema.
- `mock_tool(action)` is a predictable governance test tool wrapped by `agentmesh.governance` and [policy.yaml](../policy.yaml).

The system prompt tells the model that ordinary `SELECT`, `INSERT`, `UPDATE`, and `DELETE` calls run immediately, while destructive table operations are automatically paused for approval. The model still calls `execute_sql` normally; the graph, not the model, enforces the pause.

## Normal request lifecycle

For a request that does not require approval, the path is:

1. The client sends `POST /chat` with a `message`, a `thread_id`, and optionally a `user_id`.
2. `touch_session()` upserts the `lg_sessions` row. The first message becomes the session title; later requests update `updated_at`.
3. `advance_graph()` calls `graph.ainvoke()` with the same `thread_id` in the LangGraph config.
4. The `agent` node sends the conversation to the configured model.
5. If the model returns no tool call, the graph ends and the last message becomes the response content.
6. If the model requests a safe tool, `tool_dispatch` invokes the corresponding LangChain tool immediately.
7. The tool result is stored as a `ToolMessage`.
8. If there are more tool calls, the graph dispatches the next one. Otherwise it returns to `agent` so the model can interpret the result and produce a final answer.
9. The API returns:

```json
{"paused": false, "content": "..."}
```

A request can therefore contain multiple model/tool turns before the final response. The checkpoint stores the graph state between turns and across HTTP requests.

## Approval lifecycle

Approval applies only when the model asks to run `execute_sql` and the SQL text matches the current detector in [tools.py](../tools.py):

```text
DROP TABLE
ALTER TABLE
TRUNCATE TABLE
```

The match is case-insensitive and requires the `TABLE` keyword after the operation. Other tools, and other SQL statements, do not enter this approval path in the current implementation.

### 1. The model proposes a destructive tool call

The model returns a tool call such as:

```json
{
  "name": "execute_sql",
  "id": "call-123",
  "args": {
    "query": "DROP TABLE audit_log"
  }
}
```

`agent_node` stores this call in `pending_tool_calls` and routes to `tool_dispatch`.

### 2. The graph checks the approval boundary

`tool_dispatch_node` removes the next call from the pending list and evaluates:

```python
needs_approval(tool_name, tool_args)
```

For a destructive `execute_sql` call, it calls LangGraph's `interrupt()` with this payload:

```json
{
  "tool_call_id": "call-123",
  "tool_name": "execute_sql",
  "tool_args": {
    "query": "DROP TABLE audit_log"
  }
}
```

The interrupt happens before `TOOLS_BY_NAME[tool_name].invoke(tool_args)`. No SQL connection is opened and no destructive statement is executed at this point.

LangGraph persists the paused graph state through `AsyncPostgresSaver`. The state includes the conversation, the pending tool call, and the thread identity needed for a later resume.

### 3. The runner registers the paused run

`advance_graph()` sees `"__interrupt__"` in the graph result and passes the interrupt payload to `register_approval()`.

`register_approval()` then:

1. Generates a UUID as `approval_id`.
2. Looks up the original `user_id` from `lg_sessions` using `thread_id`.
3. Calls `create_approval_ticket()` in a worker thread because the Jira client is synchronous.
4. Creates a Jira issue containing the agent identity, operation, session ID, approval ID, tool name, approval type, requester, and tool arguments. Configured Jira custom fields are used when available; otherwise values are written into the description.
5. Inserts a row into `lg_run_approvals` with the approval ID, thread ID, Jira issue key, and original interrupt payload.
6. Returns the approval ID to the runner.

The approval table is the local join between the external Jira issue and the paused LangGraph thread:

| Column | Meaning |
| --- | --- |
| `approval_id` | Public correlation ID returned to the client. |
| `thread_id` | LangGraph conversation to resume. |
| `jira_issue_key` | Jira issue used for the human decision. |
| `tool_call_json` | Original interrupt payload for audit/debugging. |
| `created_at` | Time the local record was created. |

The initial API response is:

```json
{
  "paused": true,
  "approval_id": "generated-uuid"
}
```

At this point the original HTTP request is complete, but the graph run is still paused in the checkpoint store.

### 4. A reviewer decides in Jira

A reviewer examines the Jira issue and changes it through the configured Jira workflow. `get_simplified_status()` maps the raw Jira status name as follows:

| Jira status | Service status |
| --- | --- |
| `To Do` or `In Progress` | `pending` |
| `Approved` or `Done` | `approved` |
| `Rejected` or `Blocked` | `blocked` |
| Any unmapped status | `pending` |

The names are compared case-insensitively. The deployment's Jira workflow must use the configured names or the map in [config.py](../config.py) must be changed.

The service does not expose a Jira webhook route and does not transition Jira issues itself. A frontend can poll the approval status, or an external Jira Automation rule can call the resume endpoints after the Jira issue changes.

### 5. The client checks approval status

The client calls:

```text
GET /api/approvals/{approval_id}
```

The service uses `lg_run_approvals` to find the Jira issue, reads the current Jira status, and returns:

```json
{
  "approval_id": "generated-uuid",
  "jira_status": "Approved",
  "status": "approved"
}
```

An unknown approval ID returns `404`. Status is read from Jira on each request rather than copied into the local approval table.

### 6. The approved path resumes the same graph

After Jira reports `approved`, the client calls:

```text
POST /api/approvals/{approval_id}/continue
```

The endpoint:

1. Looks up the approval row and its `thread_id`.
2. Reads Jira again and rejects the request with `409` unless the simplified status is exactly `approved`.
3. Calls `advance_graph()` with `Command(resume={"approved": True})` and the stored thread ID.
4. LangGraph resumes at the interrupt. The approval decision is returned to `tool_dispatch_node`.
5. The node invokes the original `execute_sql` call.
6. The SQL result becomes a `ToolMessage` and the graph continues through any remaining tool calls and the agent's final response.
7. `notify_resolution()` sends a WebSocket event to the session.
8. The endpoint returns either a final response or a new paused response if another destructive call needs approval.

For a completed run, the WebSocket event is:

```json
{
  "type": "run_resumed",
  "approval_id": "generated-uuid",
  "status": "completed",
  "content": "..."
}
```

### 7. The rejected path resumes without executing SQL

The client calls:

```text
POST /api/approvals/{approval_id}/reject
```

The endpoint requires Jira to report `blocked`; otherwise it returns `409`. It resumes the graph with:

```python
Command(resume={"approved": False, "note": "Rejected via Jira"})
```

The interrupt returns the decision to `tool_dispatch_node`. Instead of invoking the tool, the node creates a tool result like:

```text
Blocked by reviewer: Rejected via Jira
```

That result is passed back to the model, which can explain the blocked action in its final response. A rejected request therefore continues through the graph, but the destructive tool never runs.

### 8. Chained approvals

The model may request more than one tool, or may request another destructive tool after interpreting an earlier result. When a resumed invocation encounters another interrupt:

1. `advance_graph()` registers a new Jira ticket and local approval row.
2. It returns a new `approval_id` with `paused: true`.
3. `notify_resolution()` sends `run_paused` with the new ID instead of reporting completion.
4. The client repeats the status and decision cycle for the new approval.

Each approval ID maps to its own interrupt payload, Jira issue, and point in the same thread's checkpoint history.

## Persistence model

There are three distinct kinds of persisted state:

### LangGraph checkpoint state

Managed by `AsyncPostgresSaver` and created by its `setup()` method. This is the source of truth for resumable graph execution, including `messages` and `pending_tool_calls`.

### Approval bookkeeping

Managed by [db.py](../db.py) in `lg_run_approvals`. It does not contain the graph state. It only connects an externally visible approval ID and Jira issue to the LangGraph thread and records the interrupt payload.

### Session metadata

Managed by [sessions.py](../sessions.py) in `lg_sessions`. It stores `thread_id`, the initial `user_id`, a short title, and timestamps. Full chat history is reconstructed from the checkpointer by `GET /sessions/{thread_id}`; tool and system messages are intentionally omitted from that chat view.

## HTTP and WebSocket API

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/chat` | Start or continue a user conversation. Body: `message`, `thread_id`, optional `user_id`. Returns either final content or an approval ID. |
| `GET` | `/sessions` | List sessions, optionally filtered by `user_id`. |
| `GET` | `/sessions/{thread_id}` | Read human/assistant chat history reconstructed from graph checkpoints. |
| `GET` | `/api/approvals/{approval_id}` | Read the current Jira and simplified approval status. |
| `POST` | `/api/approvals/{approval_id}/continue` | Resume after Jira status is `approved`. |
| `POST` | `/api/approvals/{approval_id}/reject` | Resume after Jira status is `blocked` or `rejected`. |
| WebSocket | `/ws/sessions/{session_id}` | Receive `run_paused` and `run_resumed` events for a session. Incoming text is only used to keep the connection alive. |

The runner response shape is intentionally small:

```json
{"paused": false, "content": "final assistant text"}
```

or:

```json
{"paused": true, "approval_id": "approval-uuid"}
```

A client can use the HTTP response immediately and use the WebSocket as the asynchronous notification channel when a resumed run finishes or pauses again.

## Configuration and local execution

Configuration is loaded from environment variables by [config.py](../config.py), with `.env` support through `python-dotenv`.

| Variable | Default or requirement | Used for |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:qaszdeszqa@localhost:5432/test_db` | Application SQL, sessions, approvals, and LangGraph checkpoints. Change this outside the demo setup. |
| `MODEL_PROVIDER` | `copilot2api` | Selects the model adapter. |
| `MODEL_ID` | `gpt-5.6-luna` | Model identifier passed to the adapter. |
| `MODEL_BASE_URL` | `http://127.0.0.1:7777/v1` | OpenAI-compatible endpoint used by copilot2api mode. See [COPILOT2API_HANDOFF.md](../COPILOT2API_HANDOFF.md). |
| `MODEL_API_KEY` | `dummy` if no key is set | Non-empty value required by `ChatOpenAI`; copilot2api authenticates separately. |
| `JIRA_BASE_URL` | Required | Jira server URL. |
| `JIRA_EMAIL` | Required | Jira account used to create and read issues. |
| `JIRA_API_TOKEN` | Required | Jira API token. |
| `JIRA_PROJECT_KEY` | Required | Project where approval issues are created. |
| `JIRA_ISSUE_TYPE` | `Task` | Jira issue type for approvals. |
| `JIRA_FIELD_SESSION_ID` | `customfield_10145` | Jira field containing the LangGraph thread ID. |
| `JIRA_FIELD_APPROVAL_ID` | Unset | Optional dedicated approval ID field. |
| `JIRA_FIELD_TOOL_NAME` | Unset | Optional dedicated tool name field. |
| `JIRA_FIELD_AGENT_ID` | Unset | Optional dedicated agent identity field. |
| `JIRA_FIELD_APPROVAL_TYPE` | Unset | Optional Jira select field. The configured option must be `required`. |
| `JIRA_FIELD_REQUESTED_BY` | Unset | Optional requester field. |
| `JIRA_FIELD_TOOL_ARGS` | Unset | Optional tool argument field; otherwise arguments are written to the description. |
| `SERVER_PORT` | `7791` | FastAPI/Uvicorn port. |
| `FRONTEND_ORIGIN` | `http://localhost:5173` | Allowed CORS origin. |
| `AGENT_NAME` | `postgres_agent_langgraph` | Identity shown on Jira approval tickets. |

Typical local setup:

```powershell
pip install -r requirements.txt
python main.py
```

The model proxy, if using the default copilot2api configuration, must also be running on `127.0.0.1:7777`. PostgreSQL and Jira must be reachable before the service starts because configuration and Jira client initialization happen during import, and database schema/checkpoint setup happens during application startup.

## Behavioral guarantees and current limits

### Guarantees provided by the current code

- The approval check is performed before a destructive tool is invoked.
- Safe SQL operations execute without Jira approval under the current detector.
- A paused run is resumed through LangGraph's checkpoint, not by reconstructing the prompt manually.
- The original `thread_id` is preserved from the chat request through the approval record and Jira issue.
- The rejected branch records a blocked tool result and does not call the SQL tool.
- A second approval in the same conversation produces a new approval ID and is reported to the client.

### Limits to account for

- The current approval detector only covers `DROP TABLE`, `ALTER TABLE`, and `TRUNCATE TABLE` when sent through `execute_sql`. Other destructive or privilege-changing SQL is not automatically approved by this code.
- `execute_sql` is a general SQL execution tool. The service should be treated as trusted infrastructure and should not be exposed publicly without authentication, authorization, rate limiting, and database least-privilege controls.
- The API has no user authentication. `user_id` is supplied by the caller, and the approval endpoints authorize by approval ID plus Jira status, not by an authenticated user identity.
- Jira status names are deployment-specific. Unmapped Jira statuses become `pending`, which prevents continuation but may make a ticket appear stuck.
- There is no built-in Jira webhook endpoint. Jira Automation or a client must call the continue/reject route after a decision.
- The process-local graph singleton assumes one service process. A multi-process deployment needs a shared checkpointer and a deployment strategy that preserves safe concurrent resume behavior.
- The approval row is written after Jira issue creation. If database bookkeeping fails after Jira succeeds, an orphan Jira issue can exist and must be reconciled operationally.
- `policy.yaml` currently governs the test `mock_tool`; it is not the SQL approval mechanism. SQL approval is implemented by `needs_approval()` and LangGraph `interrupt()`.

## End-to-end example

A request such as:

```text
Drop the audit_log table.
```

moves through the system as follows:

```text
1. Client -> POST /chat
2. Model -> execute_sql({"query": "DROP TABLE audit_log"})
3. tools.needs_approval -> True
4. graph.tool_dispatch -> interrupt(payload)
5. runner -> create Jira issue + lg_run_approvals row
6. API -> {"paused": true, "approval_id": "..."}
7. Reviewer -> changes Jira issue to Approved
8. Client/Automation -> POST /api/approvals/{id}/continue
9. API -> Command(resume={"approved": true}) on the original thread
10. graph.tool_dispatch -> execute_sql(...)
11. PostgreSQL -> SQL result
12. graph.agent -> final assistant response
13. WebSocket -> run_resumed event
```

For a rejection, steps 8 through 10 instead resume with `approved: false`, create a blocked `ToolMessage`, and never execute the SQL statement.
