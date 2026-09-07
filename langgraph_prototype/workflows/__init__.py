from orchestrator import register_workflow

from . import simple, medium, complex as complex_wf, jira

register_workflow("simple", simple.build())
register_workflow("medium", medium.build())
register_workflow("complex", complex_wf.build())
register_workflow("jira", jira.build())
