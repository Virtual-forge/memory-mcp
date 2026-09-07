# Development and Operations

## Prerequisites

- Node.js compatible with the VS Code extension toolchain.
- npm.
- Python 3.9 or newer for the gateway and hook.
- PostgreSQL.
- A Jira project and API token if approvals should create real Jira issues.
- VS Code with the recommended development extensions from
  `.vscode/extensions.json`.

The hook itself uses only Python's standard library. The gateway uses third-
party packages imported by the source, but the repository does not currently
include a Python requirements file.

## Node Installation

From the repository root:

```powershell
npm install
```

This installs TypeScript, ESLint, esbuild, the VS Code test CLI, and the
`ws` dependency used by the extension.

## Python Installation

Create and activate a virtual environment from the repository root:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install fastapi uvicorn sqlalchemy requests python-dotenv pydantic
```

The gateway imports `fastapi`, `pydantic`, `sqlalchemy`, `requests`, and
`dotenv`. Uvicorn is needed to run `main.py`.

## PostgreSQL Setup

Set a connection string before starting the gateway:

```powershell
$env:DATABASE_URL = "postgresql://postgres:<password>@localhost:5432/governance_db"
```

The database itself must already exist. On startup, SQLAlchemy creates the
`agent_states` table automatically. There is no migration or seed script.

If `DATABASE_URL` is omitted, the code uses the default connection string in
`backendfiles for reading/database.py`.

## Jira Setup

Set the gateway's Jira variables in the same shell:

```powershell
$env:JIRA_BASE_URL = "https://your-company.atlassian.net"
$env:JIRA_EMAIL = "agent@example.com"
$env:JIRA_API_TOKEN = "your-api-token"
$env:JIRA_PROJECT_KEY = "GOV"
```

Configure a Jira webhook to send issue status changes to:

```text
POST http://<reachable-host>:8000/jira-webhook
```

The webhook must include the issue key and a status changelog entry. The
gateway recognizes these status names:

- Approval: `done`, `approved`, `resolved`
- Rejection: `rejected`, `closed`, `declined`

## Start the Gateway

Run from the directory containing both `main.py` and `database.py` so the
local import resolves:

```powershell
Set-Location ".\backendfiles for reading"
py .\main.py
```

The gateway listens on:

```text
http://localhost:8000
```

The extension's HTTP and WebSocket URLs are currently hard-coded to this
address. The hook can use another address through `GOVERNANCE_URL`.

## Build and Run the Extension

From the repository root:

```powershell
npm run compile
```

This runs type checking, ESLint, and esbuild. The bundle is written to
`dist/extension.js`.

To develop with continuous compilation:

```powershell
npm run watch
```

The default VS Code build task already runs this watch command. Press `F5` to
use `.vscode/launch.json`; its `preLaunchTask` runs `npm: compile` and then
opens an Extension Development Host with the compiled bundle.

Once the development host is open:

1. Use `@govAgent` in chat.
2. Ask for a deployment or a Jira transition to exercise an approval path.
3. Approve or reject the generated Jira issue.
4. Keep the chat request open for live WebSocket behavior, or reload VS Code
   and send `@govAgent continue` to exercise recovery.

## Standalone Hook Configuration

The hook looks for the file named by `APPROVAL_CONFIG`, defaulting to:

```text
hooks/approval_config.json
```

The path is resolved relative to the current working directory. The repository
does not include this file. A minimal configuration is:

```json
{
  "governance_url": "http://localhost:8000",
  "poll_interval_seconds": 3,
  "max_wait_seconds": 3600,
  "fail_mode": "ask",
  "watched_tools": [
    "deploy_to_production",
    "transitionJiraIssue",
    "mcp__*__transitionJiraIssue"
  ]
}
```

Supported `fail_mode` values are `allow`, `deny`, and `ask`. It is used when
the gateway is unreachable, an approval record disappears, or the wait times
out. `GOVERNANCE_URL` always overrides the JSON file's URL.

To invoke the hook manually, provide one JSON event on stdin:

```powershell
'{"tool_name":"deploy_to_production","tool_input":{"targetEnv":"production","command":"kubectl apply -f prod.yaml"},"session_id":"demo","tool_use_id":"call-1","cwd":"C:\\work\\Agent-Gov"}' | py ".\backendfiles for reading\hooks\approval_gate.py"
```

The hook logs diagnostics to stderr and prints one permission response to
stdout. An empty `watched_tools` array allows all calls immediately.

## npm Scripts

| Script | Purpose |
| --- | --- |
| `npm run compile` | Type-check, lint, and build a development bundle. |
| `npm run watch` | Run esbuild watch and TypeScript watch in parallel. |
| `npm run watch:esbuild` | Watch and rebuild `dist/extension.js`. |
| `npm run watch:tsc` | Watch TypeScript diagnostics without emitting. |
| `npm run package` | Type-check, lint, and produce a minified bundle. |
| `npm run compile-tests` | Compile tests to `out`. |
| `npm run pretest` | Compile tests, compile the extension, and lint. |
| `npm run check-types` | Run `tsc --noEmit`. |
| `npm run lint` | Run ESLint over `src`. |
| `npm test` | Run the VS Code integration test CLI. |

## Testing

The only checked-in test is the generated sample in
`src/test/extension.test.ts`. It verifies array lookup behavior and does not
exercise governance logic, the gateway, Jira, the WebSocket, or the hook.

Run the current test pipeline with:

```powershell
npm test
```

The test CLI consumes compiled files under `out/test/**/*.test.js`. The
standard VS Code test environment must be available for the test runner to
launch an Extension Development Host.

## Debugging Checklist

When an approval does not work, check the layers in this order:

1. Confirm PostgreSQL is reachable using `DATABASE_URL`.
2. Open `http://localhost:8000` or inspect the gateway terminal for startup
   and Jira errors.
3. Confirm Jira variables are set in the shell that launched the gateway.
4. Confirm the Jira issue was created and that its status transition matches a
   recognized mapping.
5. For a live extension request, inspect the Extension Host console for
   WebSocket and status-poll logs.
6. For a hook request, inspect stderr separately from the JSON on stdout.
7. Confirm the tool name matches the extension's governance set or the hook's
   watch patterns.
8. If VS Code was offline during approval, send `@govAgent continue` in the
   workspace that created the checkpoint.

## Change Workflow

When changing governance behavior:

1. Update the extension router or hook policy, depending on the entry point.
2. Keep checkpoint fields sufficient to reconstruct the tool call.
3. Keep pause and completion calls idempotent by preserving the thread ID.
4. Add focused tests for status mapping, tool classification, and failure
   behavior before changing shared endpoint contracts.
5. Run `npm run check-types`, `npm run lint`, and `npm test` for extension
   changes.
6. Exercise the gateway and hook with a local Jira or test double for Python
   changes.