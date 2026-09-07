# Limitations and Security

This page records behavior that is visible in the current source. It is part
of the architecture documentation because these constraints affect how the
system should be deployed and extended.

## Security Constraints

### No authentication or authorization

The gateway exposes pause, status, completion, dashboard, WebSocket, and
webhook routes without authentication. Anyone who can reach port `8000` can
read checkpoint data or mark a thread completed.

The gateway also enables all CORS origins, methods, and headers. Restrict both
network access and CORS before using it outside a trusted development
environment.

### Sensitive data is persisted and logged

Tool inputs are stored in PostgreSQL and copied into Jira issue descriptions.
The source also logs thread IDs, Jira keys, and some error bodies. Do not put
secrets in tool input until the persistence and redaction model has been
designed.

### Development credentials are present in defaults

`database.py` contains a default PostgreSQL URL with a username and password.
Use `DATABASE_URL` in every real environment and remove development
credentials from source before distribution.

### Webhook trust is implicit

`/jira-webhook` accepts status updates without signature validation or another
proof that the request came from Jira. A production deployment needs webhook
authentication and replay protection.

## Functional Gaps

### Deployment is not a real deployment

The `deploy_to_production` tool records approval and returns a success message,
but it does not execute the requested shell command. The returned command is
descriptive data only. A real executor would need explicit sandboxing,
authorization, output capture, cancellation, and audit handling.

### The contributed command is not wired

`package.json` declares `agentGovernance.executeRestrictedAction`, but
`src/extension.ts` does not call `vscode.commands.registerCommand` for it.
The command appears in the manifest but has no implementation path.

### The hook is not automatically installed

The hook is source code only. There is no checked-in
`hooks/approval_config.json`, installer, VS Code hook registration, or external
agent configuration that invokes it. An external runner must be configured
separately.

### Tool policy is narrow and split across entry points

The extension governs only `deploy_to_production` and normalized names ending
in `transitionJiraIssue`. The hook uses its own configurable watch list. A
tool can therefore be governed in one path and allowed in the other unless
the policies are kept aligned.

### The test suite is still scaffold code

The checked-in test does not cover any governance behavior. Important missing
tests include:

- stable thread ID generation;
- exact and namespaced tool classification;
- pause idempotency;
- Jira status mapping;
- WebSocket notification and polling fallback;
- cancellation and timeout behavior;
- offline recovery and checkpoint reconstruction; and
- hook JSON input/output and fail modes.

### No backend dependency lock or migration process

The Python service has no `requirements.txt`, lock file, migration files, or
backend test suite. Reproducible deployment and schema evolution are not yet
defined.

## Reliability and Concurrency Notes

### Pause creation can leave an orphan Jira issue

`/api/v1/pause` creates the Jira issue before inserting the PostgreSQL row. If
the database commit fails afterward, Jira can contain an approval issue with
no corresponding local state.

### Jira polling is a fallback, not a queue

The gateway checks Jira when a client asks for status or pending approvals. It
does not run a background worker, so a decision is not imported until a client
polls or a webhook arrives.

### WebSocket state is process-local

The active connection map lives in one Python process. Multiple gateway
workers need shared event delivery or sticky routing for live approval events.

### Repeated resume is guarded only in the extension process

`recoveryInProgress` prevents duplicate resume calls within one extension host.
It is not a database lock and does not protect against two VS Code instances
resuming the same approved thread.

### Extension waiting has no deadline

The hook has `max_wait_seconds`, but the extension's WebSocket/polling wait
continues until approval, rejection, or cancellation. A user can therefore
leave a chat request pending indefinitely.

### External Jira calls lack a uniform timeout policy

The status synchronization GET uses a 10-second timeout. Jira issue creation
does not currently specify one, so a network problem can hold the pause
request longer than expected.

## Design Invariants for Future Changes

When extending this project, preserve these invariants unless the protocol is
intentionally versioned:

1. A tool must not run before its approval-required path has returned an
   approved state.
2. Every paused action must retain enough checkpoint data to reconstruct its
   tool name and input.
3. The same `thread_id` must be used for pause, status, notification, and
   completion.
4. Hook stdout must contain only the machine-readable permission response.
5. `COMPLETED` must remain terminal for recovery queries.
6. Jira status mapping must be consistent between webhook and polling paths.
7. Approval policy changes must be reflected in both the extension router and
   hook configuration when both entry points are in use.