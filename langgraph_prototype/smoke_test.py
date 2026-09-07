"""
Fast, free sanity check for the graph wiring -- no proxy or
network needed. Monkeypatches llm.get_llm with a stub that returns
pre-scripted responses (in call order), so you can confirm the
retry/HITL loop and node wiring are correct before spending real API
calls. Run this after editing a workflow's node logic.

This tests OUR plumbing, not Luna's judgment -- it proves the graph
routes correctly given a set of model outputs, not that the model will
actually produce those outputs.
"""

import llm
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from orchestrator import build_orchestrator
import workflows  # noqa: F401 -- populates the registry


def fake_get_llm(plan):
    """Returns a get_llm() replacement that hands back items from `plan`,
    in order, one per .invoke()-style call (whether direct or via
    with_structured_output)."""
    state = {"i": 0}

    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeStructured:
        def __init__(self, value):
            self._value = value

        def invoke(self, *_a, **_k):
            return self._value

    class FakeLLM:
        def with_structured_output(self, *_a, **_k):
            value = plan[state["i"]]
            state["i"] += 1
            return FakeStructured(value)

        def invoke(self, *_a, **_k):
            value = plan[state["i"]]
            state["i"] += 1
            return FakeMessage(value)

    return lambda *a, **k: FakeLLM()


def drive(thread_id, workflow_name, workflow_input, max_attempts, decisions, checkpointer):
    from orchestrator import rectifier_agent
    rectifier_agent.enabled = False
    graph = build_orchestrator(checkpointer)
    config = {"configurable": {"thread_id": thread_id}}
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
        assert step < len(decisions), f"ran out of scripted decisions for {thread_id}"
        out = graph.invoke(Command(resume=decisions[step]), config)
        step += 1
    return out


def test_simple():
    from recovery import RecoveryDecision
    from workflows.simple import AmountExtraction
    llm.get_llm = fake_get_llm([
        AmountExtraction(amount=None, confidence="low"),       # attempt 0: fails
        RecoveryDecision(
            action="ask_user",
            resume_node="parse",
            question="What amount should the workflow use?",
            reason="The extraction is ambiguous and needs a human correction.",
        ),
        AmountExtraction(amount=1200.50, confidence="high"),   # attempt 1: succeeds
    ])
    out = drive("t-simple", "simple", {"raw_amount": "unclear text"}, 3,
                [{"decision": "approve", "rectification": "it's 1200.50"}], InMemorySaver())
    assert out["last_result"]["status"] == "success"
    assert out["last_result"]["payload"]["amount"] == 1200.50
    print("simple: OK")


def test_medium():
    from recovery import RecoveryDecision
    from workflows.medium import ReviewVerdict
    llm.get_llm = fake_get_llm([
        RecoveryDecision(
            action="ask_user",
            resume_node="validate",
            question="Provide the missing email address.",
            reason="Validation cannot continue without the user's email.",
        ),
        "Hi Youssef, welcome!!! Don't miss out, buy now!!!",                 # draft 1 (bad)
        ReviewVerdict(passes=False, reason="Contains exclamation points and sounds salesy."),
        RecoveryDecision(
            action="retry_with_correction",
            resume_node="draft",
            rectification="Use a friendlier, less salesy tone.",
            reason="The draft can be regenerated with a clearer editorial constraint.",
        ),
        "Hi Youssef, glad to have you with us.",                            # draft 2 (good)
        ReviewVerdict(passes=True, reason="Meets all rules."),
    ])
    out = drive("t-medium", "medium", {"user_id": "u2"}, 3,
                [{"decision": "approve", "rectification": "youssef@example.com"},
                 {"decision": "approve", "rectification": "tone it down, less salesy"}],
                InMemorySaver())
    assert out["last_result"]["status"] == "success"
    print("medium: OK")


def test_medium_fatal():
    from recovery import RecoveryDecision
    llm.get_llm = fake_get_llm([
        RecoveryDecision(
            action="stop",
            reason="No user record exists and no available tool can infer a user id.",
        ),
    ])
    out = drive("t-medium-fatal", "medium", {"user_id": "does-not-exist"}, 3, [], InMemorySaver())
    assert out["last_result"]["status"] == "failure"
    assert out["last_result"]["error_status"] == "FATAL"
    print("medium (fatal, recovery planner stopped): OK")


def test_recovery_prompt_contains_failure_and_tools():
    from recovery import RecoveryDecision, plan_recovery

    captured = {}

    class CapturingLLM:
        def invoke(self, prompt):
            captured["prompt"] = prompt
            return RecoveryDecision(
                action="retry_same",
                resume_node="A_inspect",
                question="Provide a valid issue key.",
                reason="The requested issue is not accessible, so retrying needs review.",
            )

    original_get_llm = llm.get_llm
    llm.get_llm = lambda **_kwargs: CapturingLLM()
    try:
        plan = plan_recovery(
            "jira",
            {
                "workflow_name": "jira",
                "workflow_input": {"issue_key": "REAL-123"},
                "attempt": 1,
                "max_attempts": 3,
                "failed_step": "A_inspect",
            },
            {
                "status": "failure",
                "error_status": "VALIDATION",
                "error_message": "Issue REAL-123 was not found.",
                "retry": True,
            },
            "A_inspect",
        )
        prompt = captured["prompt"]
        assert "Failure status: VALIDATION" in prompt
        assert "Failure message: Issue REAL-123 was not found." in prompt
        assert "Failed node: A_inspect" in prompt
        assert "get_issue" in prompt
        assert "transition_issue" in prompt
        assert plan["action"] == "ask_user"
        print("recovery (failure context and tool catalog): OK")
    finally:
        llm.get_llm = original_get_llm


def test_structured_output_uses_plain_json_and_local_validation():
    from recovery import RecoveryDecision

    class PlainMessage:
        content = (
            "Here is the result:\n"
            "```json\n"
            '{"action":"stop","resume_node":null,"tool_name":null,'
            '"rectification":null,"question":null,"reason":"Provider request is invalid."}\n'
            "```"
        )

    class PlainLLM:
        def invoke(self, prompt):
            assert "guided_json" not in prompt
            assert "JSON schema:" in prompt
            return PlainMessage()

    original_get_llm = llm.get_llm
    llm.get_llm = lambda **_kwargs: PlainLLM()
    try:
        decision = llm.invoke_structured(RecoveryDecision, "Classify this failure.", max_tokens=128)
        assert decision.action == "stop"
        assert decision.reason == "Provider request is invalid."
        print("structured output (plain JSON and local validation): OK")
    finally:
        llm.get_llm = original_get_llm


def test_resource_failure_stops_without_user_correction():
    from recovery import RecoveryDecision, plan_recovery

    class ResourceLLM:
        def invoke(self, _prompt):
            return RecoveryDecision(
                action="ask_user",
                question="Which Jira transition id should be used?",
                reason="The model suggested asking for a transition id.",
            )

    original_get_llm = llm.get_llm
    llm.get_llm = lambda **_kwargs: ResourceLLM()
    try:
        plan = plan_recovery(
            "jira",
            {"workflow_name": "jira", "failed_step": "B_transition"},
            {
                "status": "failure",
                "error_status": "RESOURCE",
                "error_message": "NVIDIA rejected guided_json.",
                "retry": True,
            },
            "B_transition",
        )
        assert plan["action"] == "stop"
        print("recovery (resource failure does not ask for workflow correction): OK")
    finally:
        llm.get_llm = original_get_llm


def test_complex():
    from recovery import RecoveryDecision
    from workflows.complex import BatchDecision
    llm.get_llm = fake_get_llm([
        RecoveryDecision(
            action="retry_same",
            resume_node="sync_external",
            tool_name="sync_external",
            reason="The external timeout is transient and the sync operation is safe to retry.",
        ),
        RecoveryDecision(
            action="retry_same",
            resume_node="sync_external",
            tool_name="sync_external",
            reason="The second external timeout remains transient.",
        ),
        RecoveryDecision(
            action="ask_user",
            resume_node="quality_gate",
            question="Authorize dropping invalid records to continue.",
            reason="The quality gate needs an explicit policy decision for invalid rows.",
        ),
        BatchDecision(action="drop_invalid", explanation="Reviewer authorized dropping bad rows."),
    ])
    batch = [
        {"id": 1, "amount": "100.00"}, {"id": 2, "amount": "250.5"},
        {"id": 3, "amount": "NOT_A_NUMBER"}, {"id": 4, "amount": "75"},
    ]
    out = drive("t-complex", "complex", {"batch": batch, "min_quality": 0.9}, 5,
                [{"decision": "approve"}, {"decision": "approve"},
                 {"decision": "approve", "rectification": "yes drop the bad ones and go ahead"}],
                InMemorySaver())
    assert out["last_result"]["status"] == "success"
    assert out["last_result"]["payload"]["committed"] == 3
    print("complex: OK")


def test_complex_fatal():
    from recovery import RecoveryDecision
    llm.get_llm = fake_get_llm([
        RecoveryDecision(
            action="retry_same",
            resume_node="sync_external",
            tool_name="sync_external",
            reason="The first dependency timeout is transient.",
        ),
        RecoveryDecision(
            action="retry_same",
            resume_node="sync_external",
            tool_name="sync_external",
            reason="The second dependency timeout is transient.",
        ),
        RecoveryDecision(
            action="stop",
            resume_node="quality_gate",
            reason="Every record is invalid, so no safe recovery exists.",
        ),
    ])
    batch = [{"id": 1, "amount": "oops"}, {"id": 2, "amount": "nope"}]
    out = drive("t-complex-fatal", "complex", {"batch": batch, "min_quality": 0.9}, 5,
                [{"decision": "approve"}, {"decision": "approve"}], InMemorySaver())
    assert out["last_result"]["status"] == "failure"
    assert out["last_result"]["error_status"] == "FATAL"
    print("complex (fatal, recovery planner stopped): OK")


def test_jira_resumes_at_failed_step():
    from workflows import jira as jira_workflow
    from recovery import RecoveryDecision
    from workflows.jira import TransitionChoice

    class FakeJiraClient:
        issue_status = "Pending"
        get_issue_calls = 0
        get_transitions_calls = 0
        transition_calls = 0

        @classmethod
        def from_env(cls):
            return cls()

        def get_issue(self, _issue_key):
            type(self).get_issue_calls += 1
            return {
                "key": "DEMO-1",
                "fields": {
                    "summary": "Checkpoint test",
                    "status": {"name": type(self).issue_status},
                },
            }

        def get_transitions(self, _issue_key):
            type(self).get_transitions_calls += 1
            return [{"id": "approve", "name": "Approve", "to": {"name": "Approved"}}]

        def transition_issue(self, _issue_key, transition_id):
            assert transition_id == "approve"
            type(self).transition_calls += 1
            type(self).issue_status = "Approved"

    original_client = jira_workflow.JiraClient
    original_get_llm = llm.get_llm
    jira_workflow.JiraClient = FakeJiraClient
    llm.get_llm = fake_get_llm([
        RecoveryDecision(
            action="retry_same",
            resume_node="B_transition",
            reason="The injected failure occurred before any Jira mutation.",
        ),
        TransitionChoice(
            action="transition",
            transition_id="approve",
            reason="The approved transition is available.",
        )
    ])
    try:
        graph = build_orchestrator(InMemorySaver())
        config = {"configurable": {"thread_id": "t-jira-resume"}}
        out = graph.invoke(
            {
                "workflow_name": "jira",
                "workflow_input": {
                    "issue_key": "DEMO-1",
                    "target_status": "Approved",
                    "instruction": "Move DEMO-1 to Approved.",
                    "simulate_failure_at": "B_transition",
                },
                "attempt": 0,
                "max_attempts": 3,
                "last_result": None,
                "rectification": None,
                "hitl_decision": None,
                "history": [],
            },
            config,
        )
        assert "__interrupt__" in out
        assert FakeJiraClient.get_issue_calls == 1
        assert FakeJiraClient.transition_calls == 0

        out = graph.invoke(Command(resume={"decision": "approve"}), config)
        assert out["last_result"]["status"] == "success"
        assert out["last_result"]["payload"]["to_status"] == "Approved"
        assert FakeJiraClient.get_issue_calls == 3, (
            "A_inspect must not rerun after B fails; "
            f"get_issue_calls={FakeJiraClient.get_issue_calls}"
        )
        assert FakeJiraClient.transition_calls == 1
        print("jira (resume at failed B, not A): OK")
    finally:
        jira_workflow.JiraClient = original_client
        llm.get_llm = original_get_llm


def test_jira_retry_is_idempotent_after_timeout():
    from jira_api import JiraApiError
    from workflows import jira as jira_workflow
    from recovery import RecoveryDecision
    from workflows.jira import TransitionChoice

    class ApplyThenTimeoutClient:
        issue_status = "Pending"
        transition_calls = 0

        @classmethod
        def from_env(cls):
            return cls()

        def get_issue(self, _issue_key):
            return {
                "key": "DEMO-2",
                "fields": {
                    "summary": "Idempotency test",
                    "status": {"name": type(self).issue_status},
                },
            }

        def get_transitions(self, _issue_key):
            if type(self).issue_status == "Approved":
                return [{"id": "pending", "name": "Return to pending", "to": {"name": "Pending"}}]
            return [{"id": "approve", "name": "Approve", "to": {"name": "Approved"}}]

        def transition_issue(self, _issue_key, transition_id):
            assert transition_id == "approve"
            type(self).transition_calls += 1
            type(self).issue_status = "Approved"
            raise JiraApiError(
                "Jira applied the transition but the response timed out.",
                status_code=504,
                retryable=True,
            )

    original_client = jira_workflow.JiraClient
    original_get_llm = llm.get_llm
    jira_workflow.JiraClient = ApplyThenTimeoutClient
    llm.get_llm = fake_get_llm([
        TransitionChoice(
            action="transition",
            transition_id="approve",
            reason="The approved transition is available.",
        ),
        RecoveryDecision(
            action="refresh_and_retry",
            resume_node="B_transition",
            tool_name="get_issue",
            reason="The mutation may have applied despite the timeout, so reread Jira before retrying.",
        ),
    ])
    try:
        graph = build_orchestrator(InMemorySaver())
        config = {"configurable": {"thread_id": "t-jira-idempotent"}}
        out = graph.invoke(
            {
                "workflow_name": "jira",
                "workflow_input": {
                    "issue_key": "DEMO-2",
                    "target_status": "Approved",
                    "instruction": "Move DEMO-2 to Approved.",
                },
                "attempt": 0,
                "max_attempts": 3,
                "last_result": None,
                "rectification": None,
                "hitl_decision": None,
                "history": [],
            },
            config,
        )
        assert "__interrupt__" in out
        out = graph.invoke(Command(resume={"decision": "approve"}), config)
        assert out["last_result"]["status"] == "success"
        assert out["last_result"]["payload"]["to_status"] == "Approved"
        assert ApplyThenTimeoutClient.transition_calls == 1, (
            "A retry must not duplicate a Jira transition after an ambiguous timeout"
        )
        print("jira (idempotent retry after applied timeout): OK")
    finally:
        jira_workflow.JiraClient = original_client
        llm.get_llm = original_get_llm


def test_jira_missing_issue_can_be_corrected():
    from jira_api import JiraApiError
    from workflows import jira as jira_workflow
    from recovery import RecoveryDecision
    from workflows.jira import TransitionChoice

    class CorrectedIssueClient:
        issue_status = "Pending"
        seen_keys = []
        transition_calls = 0

        @classmethod
        def from_env(cls):
            return cls()

        def get_issue(self, issue_key):
            type(self).seen_keys.append(issue_key)
            if issue_key == "MISSING-1":
                raise JiraApiError(
                    "Issue does not exist or is not visible.",
                    status_code=404,
                    retryable=False,
                )
            return {
                "key": issue_key,
                "fields": {
                    "summary": "Corrected issue",
                    "status": {"name": type(self).issue_status},
                },
            }

        def get_transitions(self, _issue_key):
            return [{"id": "approve", "name": "Approve", "to": {"name": "Approved"}}]

        def transition_issue(self, _issue_key, transition_id):
            assert transition_id == "approve"
            type(self).transition_calls += 1
            type(self).issue_status = "Approved"

    original_client = jira_workflow.JiraClient
    original_get_llm = llm.get_llm
    jira_workflow.JiraClient = CorrectedIssueClient
    llm.get_llm = fake_get_llm([
        RecoveryDecision(
            action="ask_user",
            resume_node="A_inspect",
            question="Provide a valid Jira issue key or check Browse Issues permission.",
            reason="Retrying an inaccessible issue key will produce the same 404.",
        ),
        TransitionChoice(
            action="transition",
            transition_id="approve",
            reason="The approved transition is available.",
        )
    ])
    try:
        graph = build_orchestrator(InMemorySaver())
        config = {"configurable": {"thread_id": "t-jira-correct-key"}}
        out = graph.invoke(
            {
                "workflow_name": "jira",
                "workflow_input": {
                    "issue_key": "MISSING-1",
                    "target_status": "Approved",
                    "instruction": "Move MISSING-1 to Approved.",
                },
                "attempt": 0,
                "max_attempts": 3,
                "last_result": None,
                "rectification": None,
                "hitl_decision": None,
                "history": [],
            },
            config,
        )
        interrupt_payload = out["__interrupt__"][0].value
        assert interrupt_payload["failed_step"] == "A_inspect"
        assert interrupt_payload["error_status"] == "VALIDATION"

        out = graph.invoke(
            Command(resume={"decision": "approve", "rectification": "Use DEMO-4 instead."}),
            config,
        )
        assert out["last_result"]["status"] == "success"
        assert out["last_result"]["payload"]["issue_key"] == "DEMO-4"
        assert CorrectedIssueClient.transition_calls == 1
        assert CorrectedIssueClient.seen_keys[:2] == ["MISSING-1", "DEMO-4"]
        print("jira (404 pauses and corrected key resumes): OK")
    finally:
        jira_workflow.JiraClient = original_client
        llm.get_llm = original_get_llm


def test_jira_verification_resumes_at_c():
    from workflows import jira as jira_workflow
    from recovery import RecoveryDecision
    from workflows.jira import TransitionChoice

    class EventuallyConsistentClient:
        issue_status = "Pending"
        transition_calls = 0
        delayed_reads = 0

        @classmethod
        def from_env(cls):
            return cls()

        def get_issue(self, _issue_key):
            status = type(self).issue_status
            if type(self).transition_calls and type(self).delayed_reads == 0:
                type(self).delayed_reads += 1
                status = "Pending"
            return {
                "key": "DEMO-3",
                "fields": {
                    "summary": "Verification checkpoint test",
                    "status": {"name": status},
                },
            }

        def get_transitions(self, _issue_key):
            return [{"id": "approve", "name": "Approve", "to": {"name": "Approved"}}]

        def transition_issue(self, _issue_key, transition_id):
            assert transition_id == "approve"
            type(self).transition_calls += 1
            type(self).issue_status = "Approved"

    original_client = jira_workflow.JiraClient
    original_get_llm = llm.get_llm
    jira_workflow.JiraClient = EventuallyConsistentClient
    llm.get_llm = fake_get_llm([
        TransitionChoice(
            action="transition",
            transition_id="approve",
            reason="The approved transition is available.",
        ),
        RecoveryDecision(
            action="refresh_and_retry",
            resume_node="C_verify",
            tool_name="get_issue",
            reason="Verification read is eventually consistent, so reread the issue.",
        ),
    ])
    try:
        graph = build_orchestrator(InMemorySaver())
        config = {"configurable": {"thread_id": "t-jira-verify"}}
        out = graph.invoke(
            {
                "workflow_name": "jira",
                "workflow_input": {
                    "issue_key": "DEMO-3",
                    "target_status": "Approved",
                    "instruction": "Move DEMO-3 to Approved.",
                },
                "attempt": 0,
                "max_attempts": 3,
                "last_result": None,
                "rectification": None,
                "hitl_decision": None,
                "history": [],
            },
            config,
        )
        assert out["__interrupt__"][0].value["failed_step"] == "C_verify"

        out = graph.invoke(Command(resume={"decision": "approve"}), config)
        assert out["last_result"]["status"] == "success"
        assert EventuallyConsistentClient.transition_calls == 1
        print("jira (verification retry resumes at C): OK")
    finally:
        jira_workflow.JiraClient = original_client
        llm.get_llm = original_get_llm


def test_rectifier_tool_execution():
    from orchestrator import rectifier_agent, build_orchestrator
    from rectifier import RectificationPlan
    from workflows import jira as jira_workflow
    from recovery import RecoveryDecision

    rectifier_agent.enabled = True
    original_client = jira_workflow.JiraClient
    original_get_llm = llm.get_llm

    class MockJiraToolClient:
        @classmethod
        def from_env(cls):
            return cls()

        def get_issue(self, _key):
            from jira_api import JiraApiError
            raise JiraApiError("Issue not found", status_code=404)

        def search_issues(self, jql="order by created DESC", max_results=5):
            return [
                {"key": "SCRUM-10", "fields": {"summary": "Fix login bug", "status": {"name": "To Do"}}},
                {"key": "SCRUM-11", "fields": {"summary": "Add search bar", "status": {"name": "In Progress"}}},
            ]

    jira_workflow.JiraClient = MockJiraToolClient

    llm.get_llm = fake_get_llm([
        # 1. Recovery planner pauses on 404
        RecoveryDecision(
            action="ask_user",
            resume_node="A_inspect",
            question="Which issue key should be used?",
            reason="Issue not found.",
        ),
        # 2. Rectifier interprets user rectification asking to check tickets
        RectificationPlan(
            action="run_tool",
            tool_name="search_issues",
            tool_args={"jql": "project = SCRUM"},
            explanation="User asked to list available tickets.",
        ),
    ])

    try:
        graph = build_orchestrator(InMemorySaver())
        config = {"configurable": {"thread_id": "t-rectifier-search"}}
        out = graph.invoke(
            {
                "workflow_name": "jira",
                "workflow_input": {"issue_key": "UNKNOWN-99"},
                "attempt": 0,
                "max_attempts": 3,
                "last_result": None,
                "rectification": None,
                "hitl_decision": None,
                "history": [],
            },
            config,
        )
        assert out["__interrupt__"][0].value["failed_step"] == "A_inspect"

        # Resume with rectification asking to list tickets
        out2 = graph.invoke(
            Command(resume={"decision": "approve", "rectification": "can you check available tickets and return that list to me"}),
            config,
        )

        # Graph executed search_issues and re-paused with the tool results in the question!
        interrupt_val = out2["__interrupt__"][0].value
        assert "Rectifier executed tool 'search_issues'" in interrupt_val["question"]
        assert "SCRUM-10" in interrupt_val["question"]
        print("rectifier (tool execution and re-interrupt): OK")
    finally:
        jira_workflow.JiraClient = original_client
        llm.get_llm = original_get_llm
        rectifier_agent.enabled = False


if __name__ == "__main__":
    test_simple()
    test_medium()
    test_medium_fatal()
    test_recovery_prompt_contains_failure_and_tools()
    test_structured_output_uses_plain_json_and_local_validation()
    test_resource_failure_stops_without_user_correction()
    test_complex()
    test_complex_fatal()
    test_jira_resumes_at_failed_step()
    test_jira_retry_is_idempotent_after_timeout()
    test_jira_missing_issue_can_be_corrected()
    test_jira_verification_resumes_at_c()
    test_rectifier_tool_execution()
    test_recovery_prompt_contains_failure_and_tools()
    test_structured_output_uses_plain_json_and_local_validation()
    test_resource_failure_stops_without_user_correction()
    test_complex()
    test_complex_fatal()
    test_jira_resumes_at_failed_step()
    test_jira_retry_is_idempotent_after_timeout()
    test_jira_missing_issue_can_be_corrected()
    test_jira_verification_resumes_at_c()
    print("\nAll wiring checks passed -- no real model calls were made.")
