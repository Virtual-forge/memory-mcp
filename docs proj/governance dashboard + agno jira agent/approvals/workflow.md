# Approval Workflow — What Actually Happens

## The part that's missing: how does an approval get *into* the system?

An approval row shows up in `ai.agno_approvals` when some Agno agent (elsewhere —
not `backend/chatbot/jira_agent.py`, which has no gated tools) calls a tool decorated
with `requires_confirmation=True` and Agno pauses the run, writing a row via the
`PostgresDb(approvals_table="agno_approvals")` it was configured with. Nothing in
*this* repo does that. This dashboard is a consumer of approval rows, not their
producer — which matches its README's framing as a governance dashboard, but is worth
being explicit about: cloning and running just this repo, with no other agent service
writing into `ai.agno_approvals`, gives you an empty dashboard forever.

## Step 1 (intended, not present in this repo) — Jira issue creation

`database/001_jira_listener_setup.sql` sets up a trigger so that any `INSERT` into
`ai.agno_approvals` with `status = 'pending'` (or `approval_type = 'audit'`) fires
`pg_notify('new_approval', NEW.id)`. Its comments describe an `approval_listener.py`
that would `LISTEN` on that channel, create the corresponding Jira issue (populating
the 9 custom fields), and record the mapping in `jira_sync`. **That file does not exist
in this repo.** So as shipped, a new approval row does not automatically produce a Jira
issue — someone (or some other, external process) has to create the Jira issue by hand,
with the custom fields lined up to the approval row's `id`/`run_id`/`session_id`/etc.

## Step 2 — a human reviews and decides, one of two ways

**Dashboard path (Jira-native)** — this is what the shipped React app actually uses:

1. `Dashboard.jsx` calls `listApprovals("all")` every 15 seconds
   (`setInterval(load, 15000)`) and on mount — this is real, working polling, unlike
   some of the "polling" claims elsewhere that don't hold up under inspection.
2. `listApprovals` hits `GET /api/jira/approvals?status=all` → `jira_dashboard.py` → a
   JQL search against Jira directly (`jira_client.search_issues`) → mapped through
   `jira_client.simplify_issue`, which parses custom fields plus a labeled
   `Agent:`/`Run:`/`Session:`/.../`Requirements:`/`Arguments:` description blob (a
   format this same codebase's Jira-issue-creation side is presumed to write, though
   that creation code isn't in this repo either — see Step 1).
3. Clicking Approve/Block on a `RequestCard` calls `resolveApproval(issueKey, decision)`
   → `POST /api/jira/approvals/{issue_key}/resolve` → `jira_client.transition_issue`
   looks up the issue's available transitions and fires the one whose target status
   name matches `"Approved"`/`"Rejected"` (case-insensitive) → `409` back to the UI if
   no such transition exists from the issue's current status (e.g. your workflow
   requires going through an intermediate status first).
4. **This call only transitions the Jira issue.** It does not, by itself, update
   `ai.agno_approvals` or resume any paused Agno run.

**Direct-DB path** — exists in `main.py` (`GET /api/approvals`, `POST
/api/approvals/{id}/resolve`), requires a JWT, and writes straight to
`ai.agno_approvals`. Nothing in the shipped frontend calls it. If you wanted an admin
to resolve requests without touching Jira at all, this is the path to wire up — it just
isn't wired up today.

## Step 3 — closing the loop back to `ai.agno_approvals`

The only mechanism in this repo that writes a resolved decision back into
`ai.agno_approvals` from a Jira-side transition is the webhook:
`POST /webhooks/jira-approval` (`jira_webhook.py`). But **nothing in this repo
registers a Jira Automation rule** that would call it — `jira_webhook.py`'s own
docstring includes the manual setup instructions (trigger: issue transitioned to
Approved/Rejected; action: send web request to this URL with the right headers/body) —
this is documentation of what you'd need to configure in Jira yourself, not something
this codebase provisions automatically. Until that Automation rule exists and is
correctly configured (including getting the custom field ID in the body template right
— the docstring's example uses `customfield_10050`, a placeholder), transitioning a
Jira issue via the dashboard has **no effect on `ai.agno_approvals`** — the dashboard's
own "resolved" state (as reflected in the UI) comes entirely from re-polling Jira's
issue status, not from any confirmation that the underlying approval row changed.

## Step 4 (also not in this repo) — resuming the paused Agno run

Nothing in this repo calls an AgentOS `/continue` or `/cancel`-style endpoint in
response to a resolved approval. The comment trail in `jira_dashboard.py` says "Agno OS
resumes" after the webhook updates the row, which implies Agno's own run engine is
expected to notice the row change and resume on its own (the same mechanism the
sibling `inventory-approval` repo's agent relies on for its native `requires_confirmation`
tools) — but confirming that behavior would require looking at whatever external agent
service is actually producing these approval rows, which is outside this repo.

## Net effect, end to end, as this repo actually ships

```
??? (external agent, not in this repo)
    creates a row in ai.agno_approvals
        │
        ▼ (trigger fires pg_notify — nothing listens)
??? (approval_listener.py, not in this repo)
    would create the Jira issue — doesn't happen automatically here
        │
        ▼ (someone creates the Jira issue by hand, with matching custom fields)
Dashboard polls GET /api/jira/approvals every 15s ──► shows the card
        │
        ▼ admin clicks Approve/Block
POST /api/jira/approvals/{key}/resolve ──► Jira issue transitions
        │
        ▼ (requires a Jira Automation rule you configure yourself — not provisioned here)
POST /webhooks/jira-approval ──► UPDATE ai.agno_approvals SET status = ...
        │
        ▼ (Agno's own resume mechanism — not code in this repo)
??? paused run resumes
```

Three of the six steps depend on infrastructure (a listener process, a configured Jira
Automation rule, and the agent service that originates approvals) that live outside
this repo. Treat this repo as the **middle** of that chain — the Jira-facing dashboard
UI and the webhook receiver — not the whole pipeline.
