"""Polling worker for asynchronous L1 extraction and L2 synthesis."""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event
from uuid import UUID

from memory_manager.pipeline import (
    ExtractionRun,
    MemoryPipeline,
    ProjectExtractionRun,
    ProjectSynthesisRun,
    SynthesisRun,
)
from memory_manager.synthesis.triggers import (
    evaluate_synthesis_triggers,
    load_synthesis_trigger_state,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerRun:
    extraction_runs: tuple[ExtractionRun, ...]
    synthesis_runs: tuple[SynthesisRun, ...]
    errors: tuple[str, ...]
    project_extraction_runs: tuple[ProjectExtractionRun, ...] = ()
    project_synthesis_runs: tuple[ProjectSynthesisRun, ...] = ()


class MemoryWorker:
    """Discover due work and execute LLM jobs outside agent request handling."""

    def __init__(
        self,
        pipeline: MemoryPipeline,
        poll_seconds: int = 30,
        synthesis_inactivity_minutes: int | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.pipeline = pipeline
        self.poll_seconds = poll_seconds
        self.synthesis_inactivity_minutes = (
            synthesis_inactivity_minutes
            if synthesis_inactivity_minutes is not None
            else pipeline.inactivity_minutes
        )
        if self.synthesis_inactivity_minutes <= 0:
            raise ValueError("synthesis_inactivity_minutes must be positive")

    def run_once(self, now: datetime | None = None) -> WorkerRun:
        check_time = now or datetime.now(UTC)
        extraction_runs: list[ExtractionRun] = []
        synthesis_runs: list[SynthesisRun] = []
        project_extraction_runs: list[ProjectExtractionRun] = []
        project_synthesis_runs: list[ProjectSynthesisRun] = []
        errors: list[str] = []

        projects = self.discover_project_extraction_projects()
        logger.info("checking project document extraction: projects=%d", len(projects))
        for project_id, scope in projects:
            try:
                result = self.pipeline.run_project_extraction(project_id, scope)
                if result.ran:
                    project_extraction_runs.append(result)
            except Exception as error:
                message = f"project extraction failed for {project_id}/{scope}: {error}"
                errors.append(message)
                logger.exception(message)

        sessions = self.discover_sessions()
        logger.info("polling memory pipeline: sessions=%d", len(sessions))
        for session_id, scope in sessions:
            try:
                result = self.pipeline.run_extraction_if_due(
                    session_id=session_id,
                    scope=scope,
                    now=check_time,
                )
                if result.ran:
                    extraction_runs.append(result)
            except Exception as error:
                message = f"extraction failed for {session_id}/{scope}: {error}"
                errors.append(message)
                logger.exception(message)

        scopes = self.discover_synthesis_scopes()
        logger.info("checking synthesis scopes: scopes=%d", len(scopes))
        for scope in scopes:
            state = load_synthesis_trigger_state(self.pipeline.database, scope)
            trigger = evaluate_synthesis_triggers(
                last_pending_atom_at=state.last_pending_atom_at,
                pending_atom_count=state.pending_count,
                now=check_time,
                inactivity_minutes=self.synthesis_inactivity_minutes,
                volume_cap=self.pipeline.synthesis_atom_volume,
            )
            if not trigger.due:
                continue
            try:
                result = self.pipeline.run_synthesis_job(scope)
                if result.ran:
                    synthesis_runs.append(result)
            except Exception as error:
                message = f"synthesis failed for {scope}: {error}"
                errors.append(message)
                logger.exception(message)

        projects = self.discover_project_synthesis_projects()
        logger.info("checking project synthesis: projects=%d", len(projects))
        for project_id, scope in projects:
            try:
                result = self.pipeline.run_project_synthesis_job(project_id, scope)
                if result.ran:
                    project_synthesis_runs.append(result)
            except Exception as error:
                message = f"project synthesis failed for {project_id}/{scope}: {error}"
                errors.append(message)
                logger.exception(message)

        result = WorkerRun(
            extraction_runs=tuple(extraction_runs),
            synthesis_runs=tuple(synthesis_runs),
            errors=tuple(errors),
            project_extraction_runs=tuple(project_extraction_runs),
            project_synthesis_runs=tuple(project_synthesis_runs),
        )
        logger.info(
            "poll complete: extractions=%d syntheses=%d project_extractions=%d "
            "project_syntheses=%d errors=%d",
            len(result.extraction_runs),
            len(result.synthesis_runs),
            len(result.project_extraction_runs),
            len(result.project_synthesis_runs),
            len(result.errors),
        )
        return result

    def discover_sessions(self) -> list[tuple[UUID, str]]:
        rows = self.pipeline.database.fetch_all(
            """
            select distinct session_id, scope
            from turns
            where not exists (
                select 1
                from project_document_chunks as project_chunk
                where project_chunk.turn_id = turns.id
            )
            order by scope, session_id
            """
        )
        return [(row["session_id"], str(row["scope"])) for row in rows]

    def discover_synthesis_scopes(self) -> list[str]:
        rows = self.pipeline.database.fetch_all(
            """
            select distinct a.scope
            from atoms as a
            where not a.source_deleted
              and a.project_id is null
              and not exists (
                  select 1 from synthesis_coverage as sc where sc.atom_id = a.id
              )
            order by a.scope
            """
        )
        return [str(row["scope"]) for row in rows]

    def discover_project_extraction_projects(self) -> list[tuple[UUID, str]]:
        rows = self.pipeline.database.fetch_all(
            """
            select distinct document.project_id, project.scope
            from project_documents as document
            join project_document_chunks as chunk on chunk.document_id = document.id
            join project_registry as project on project.id = document.project_id
            where document.is_active
              and chunk.is_active
              and not exists (
                  select 1
                  from extraction_coverage as coverage
                  where coverage.turn_id = chunk.turn_id
              )
              and not exists (
                  select 1
                  from atom_sources as source
                  where source.turn_id = chunk.turn_id
              )
            order by document.project_id
            """
        )
        return [(row["project_id"], str(row["scope"])) for row in rows]

    def discover_project_synthesis_projects(self) -> list[tuple[UUID, str]]:
        rows = self.pipeline.database.fetch_all(
            """
            select distinct a.project_id, a.scope
            from atoms as a
            where a.project_id is not null
              and not a.source_deleted
              and not exists (
                  select 1
                  from synthesis_coverage as coverage
                  where coverage.atom_id = a.id
              )
            order by a.project_id
            """
        )
        return [(row["project_id"], str(row["scope"])) for row in rows]

    def import_project(self, folder: str, project_id: UUID) -> object:
        """Run a confirmed project's folder import outside the agent request path."""
        return self.pipeline.import_project_documents(folder, project_id)

    def run_forever(self, stop_event: Event | None = None) -> None:
        stopper = stop_event or Event()
        logger.info("memory worker started: poll_seconds=%d", self.poll_seconds)
        while not stopper.is_set():
            self.run_once()
            stopper.wait(self.poll_seconds)
        logger.info("memory worker stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from memory_manager.config import Settings
    from memory_manager.runtime import build_memory_pipeline

    parser = argparse.ArgumentParser(description="Run memory extraction and synthesis jobs.")
    subparsers = parser.add_subparsers(dest="command")
    import_parser = subparsers.add_parser(
        "import-project",
        help="import supported Markdown documentation for a confirmed project",
    )
    import_parser.add_argument("folder", help="Markdown folder to scan recursively")
    import_parser.add_argument(
        "--project-id",
        required=True,
        type=UUID,
        help="confirmed project_registry UUID",
    )
    args = parser.parse_args()

    settings = Settings()
    worker = MemoryWorker(
        pipeline=build_memory_pipeline(settings),
        poll_seconds=settings.worker_poll_seconds,
        synthesis_inactivity_minutes=settings.synthesis_inactivity_minutes,
    )
    if args.command == "import-project":
        result = worker.import_project(args.folder, args.project_id)
        print(json.dumps(result, default=str))
        return
    worker.run_forever()


if __name__ == "__main__":
    main()
