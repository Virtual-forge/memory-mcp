from datetime import UTC, datetime, timedelta
from uuid import uuid4

from memory_manager.extraction.triggers import TriggerDecision
from memory_manager.pipeline import (
    ExtractionRun,
    ProjectExtractionRun,
    ProjectSynthesisRun,
    SynthesisRun,
)
from memory_manager.synthesis.triggers import evaluate_synthesis_triggers
from memory_manager.worker import MemoryWorker


class FakeDatabase:
    def __init__(self):
        self.session_rows = [{"session_id": uuid4(), "scope": "workspace"}]
        self.scope_rows = [{"scope": "workspace"}]
        self.project_extraction_rows = []
        self.project_synthesis_rows = []
        self.pending_count = 1
        self.last_pending_atom_at = datetime.now(UTC) - timedelta(minutes=16)

    def fetch_all(self, query, params=()):
        if "from project_documents" in query:
            return self.project_extraction_rows
        if "a.project_id is not null" in query:
            return self.project_synthesis_rows
        if "from turns" in query:
            return self.session_rows
        return self.scope_rows

    def fetch_one(self, query, params=()):
        return {
            "pending_count": self.pending_count,
            "last_pending_atom_at": self.last_pending_atom_at,
        }


class FakePipeline:
    def __init__(self):
        self.database = FakeDatabase()
        self.inactivity_minutes = 15
        self.synthesis_atom_volume = 50
        self.extraction_calls = []
        self.synthesis_calls = []
        self.project_extraction_calls = []
        self.project_synthesis_calls = []
        self.import_calls = []

    def run_extraction_if_due(self, session_id, scope, now):
        self.extraction_calls.append((session_id, scope))
        return ExtractionRun(
            turn_ids=(uuid4(),),
            indexed_atom_ids=(uuid4(),),
            ran=True,
            trigger=TriggerDecision(inactivity_due=True, volume_due=False),
        )

    def run_synthesis_job(self, scope):
        self.synthesis_calls.append(scope)
        return SynthesisRun(atom_ids=(uuid4(),), indexed_row_ids=(uuid4(),), ran=True)

    def run_project_extraction(self, project_id, scope):
        self.project_extraction_calls.append((project_id, scope))
        return ProjectExtractionRun(
            project_id=project_id,
            turn_ids=(uuid4(),),
            indexed_atom_ids=(uuid4(),),
            ran=True,
        )

    def run_project_synthesis_job(self, project_id, scope):
        self.project_synthesis_calls.append((project_id, scope))
        return ProjectSynthesisRun(
            project_id=project_id,
            atom_ids=(uuid4(),),
            indexed_section_ids=(uuid4(),),
            ran=True,
        )

    def import_project_documents(self, folder, project_id):
        self.import_calls.append((folder, project_id))
        return {"folder": folder, "project_id": project_id}


def test_synthesis_runs_after_inactivity_with_pending_atoms():
    now = datetime.now(UTC)
    decision = evaluate_synthesis_triggers(
        last_pending_atom_at=now - timedelta(minutes=16),
        pending_atom_count=1,
        now=now,
        inactivity_minutes=15,
        volume_cap=50,
    )

    assert decision.due is True
    assert decision.volume_due is False
    assert decision.inactivity_due is True


def test_worker_discovers_and_runs_due_jobs():
    pipeline = FakePipeline()
    worker = MemoryWorker(pipeline, poll_seconds=1)

    result = worker.run_once(datetime.now(UTC))

    assert len(result.extraction_runs) == 1
    assert len(result.synthesis_runs) == 1
    assert pipeline.extraction_calls == [
        (pipeline.database.session_rows[0]["session_id"], "workspace")
    ]
    assert pipeline.synthesis_calls == ["workspace"]
    assert result.errors == ()


def test_worker_discovers_and_runs_project_jobs():
    pipeline = FakePipeline()
    project_id = uuid4()
    pipeline.database.project_extraction_rows = [
        {"project_id": project_id, "scope": "project:demo"}
    ]
    pipeline.database.project_synthesis_rows = [
        {"project_id": project_id, "scope": "project:demo"}
    ]
    worker = MemoryWorker(pipeline, poll_seconds=1)

    result = worker.run_once(datetime.now(UTC))

    assert len(result.project_extraction_runs) == 1
    assert len(result.project_synthesis_runs) == 1
    assert pipeline.project_extraction_calls == [(project_id, "project:demo")]
    assert pipeline.project_synthesis_calls == [(project_id, "project:demo")]
    assert result.errors == ()


def test_worker_forwards_confirmed_project_import():
    pipeline = FakePipeline()
    project_id = uuid4()
    worker = MemoryWorker(pipeline, poll_seconds=1)

    result = worker.import_project("docs", project_id)

    assert result == {"folder": "docs", "project_id": project_id}
    assert pipeline.import_calls == [("docs", project_id)]
