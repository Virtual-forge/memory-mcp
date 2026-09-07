# Agent Approval Dashboard — Documentation Wiki

Documentation for this repo (`dashboard`, `jira` branch), rewritten against the actual
code in `backend/` and `frontend/` rather than the top-level `README.md`'s claims. Where
the README or the previous version of this wiki said something the code doesn't
actually do, it's called out explicitly in [Known Gaps](./known-gaps.md) instead of
being silently repeated.

## Pages

1. [Architecture Overview](./architecture/overview.md) — real ports, the two approval
   code paths (only one is wired into the UI), why the chat widget bypasses the
   backend's own proxy
2. [Setup](./setup.md) — what each of the three processes actually needs to start,
   including env vars the code reads vs. ones the README lists that do nothing
3. [Database Schema](./database/schema.md) — `admins`, Agno's own `ai.agno_approvals`,
   `ai.tool_descriptions`, and the two tables this repo creates but never queries
4. [Backend API Routes](./backend/api-routes.md) — every route across `main.py` and
   `backend/jira/`, and which ones the frontend actually calls
5. [Agent](./agent/architecture.md) — `jira_agent.py`: a Jira Q&A/transition assistant,
   not a DDL-approval agent, despite the "approval" naming throughout this repo
6. [Approval Workflow](./approvals/workflow.md) — the full intended chain from a new
   approval row to a resumed agent run, and exactly which links in that chain exist in
   this repo vs. which are external prerequisites you have to supply yourself
7. [Frontend](./frontend/architecture.md) — component tree, `api.js`, and the chat
   widget's direct-to-AgentOS connection
8. [Known Gaps & Inconsistencies](./known-gaps.md) — every concrete place the old
   README/docs overstated what's implemented, plus hardcoded values worth knowing about

## One-paragraph summary

This is a dashboard for reviewing Jira-tracked approval requests, with an embedded chat
widget. The dashboard's live approval list and Approve/Block actions run entirely
through **Jira** (`GET`/`POST /api/jira/approvals...`, unauthenticated at the API
level) — a parallel, JWT-protected "direct database" approval API also exists in
`main.py` but isn't called by the shipped frontend. The chat widget talks straight to a
separate Agno AgentOS process (port 7780) whose only capability is Jira/Confluence MCP
tools — it has no database-operation tools and no approval-gating logic of its own,
unlike the sibling `inventory-approval` repo's agent. Several pieces implied by the
schema and by code comments — a listener that auto-creates Jira issues for new
approvals, and the Jira Automation rule that would call this repo's webhook — are
referenced but not present in this repo; see
[Approval Workflow](./approvals/workflow.md) for exactly where those gaps sit in the
chain.
