"""Dependency-injected job facade for the independent memory pipeline stages."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from memory_manager.db.connection import Database
from memory_manager.extraction.judge import ExtractionJudge, extract_pending_turns
from memory_manager.extraction.triggers import (
    TriggerDecision,
    evaluate_triggers,
    load_session_trigger_state,
)
from memory_manager.indexing.qdrant_writer import QdrantWriter
from memory_manager.ingestion.project_docs import ProjectDocumentImporter, ProjectImportResult
from memory_manager.ingestion.turns import TurnWriter
from memory_manager.models.turn import TurnInput
from memory_manager.synthesis.judge import SynthesisJudge
from memory_manager.synthesis.project import ProjectSynthesisJudge, run_project_synthesis
from memory_manager.synthesis.routing import L2_TABLES
from memory_manager.synthesis.upsert import run_synthesis


@dataclass(frozen=True)
class IngestionResult:
    turn_id: UUID
    extraction: TriggerDecision


@dataclass(frozen=True)
class ExtractionRun:
    turn_ids: tuple[UUID, ...]
    indexed_atom_ids: tuple[UUID, ...]
    ran: bool
    trigger: TriggerDecision


@dataclass(frozen=True)
class SynthesisRun:
    atom_ids: tuple[UUID, ...]
    indexed_row_ids: tuple[UUID, ...]
    ran: bool


@dataclass(frozen=True)
class ProjectExtractionRun:
    project_id: UUID
    turn_ids: tuple[UUID, ...]
    indexed_atom_ids: tuple[UUID, ...]
    ran: bool


@dataclass(frozen=True)
class ProjectSynthesisRun:
    project_id: UUID
    atom_ids: tuple[UUID, ...]
    indexed_section_ids: tuple[UUID, ...]
    ran: bool


@dataclass(frozen=True)
class ProjectImportRun:
    import_result: ProjectImportResult
    extracted_turn_ids: tuple[UUID, ...]
    synthesized_atom_ids: tuple[UUID, ...]
    indexed_atom_ids: tuple[UUID, ...]
    indexed_section_ids: tuple[UUID, ...]


class MemoryPipeline:
    """Keep ingestion, extraction, synthesis, and indexing independently callable."""

    def __init__(
        self,
        database: Database,
        turn_writer: TurnWriter,
        extraction_judge: ExtractionJudge,
        synthesis_judge: SynthesisJudge,
        qdrant_writer: QdrantWriter,
        project_extraction_judge: ExtractionJudge | None = None,
        project_synthesis_judge: ProjectSynthesisJudge | None = None,
        project_document_importer: ProjectDocumentImporter | None = None,
        inactivity_minutes: int = 15,
        extraction_volume_cap: int = 20,
        synthesis_atom_volume: int = 50,
    ) -> None:
        self.database = database
        self.turn_writer = turn_writer
        self.extraction_judge = extraction_judge
        self.project_extraction_judge = project_extraction_judge or extraction_judge
        self.synthesis_judge = synthesis_judge
        self.qdrant_writer = qdrant_writer
        self.project_synthesis_judge = project_synthesis_judge
        self.project_document_importer = project_document_importer or ProjectDocumentImporter(
            database
        )
        self.inactivity_minutes = inactivity_minutes
        self.extraction_volume_cap = extraction_volume_cap
        self.synthesis_atom_volume = synthesis_atom_volume

    def ingest_turn(self, turn: TurnInput, now: datetime | None = None) -> IngestionResult:
        """Write L0 synchronously and return a scheduling decision to the caller."""
        turn_id = self.turn_writer.write_turn(turn)
        trigger = self.extraction_status(
            session_id=turn.session_id,
            scope=turn.scope,
            now=now or datetime.now(UTC),
        )
        return IngestionResult(turn_id=turn_id, extraction=trigger)

    def extraction_status(
        self,
        session_id: UUID,
        scope: str,
        now: datetime,
    ) -> TriggerDecision:
        state = load_session_trigger_state(self.database, session_id, scope)
        return evaluate_triggers(
            last_turn_at=state.last_turn_at,
            unprocessed_turn_count=state.pending_count,
            now=now,
            inactivity_minutes=self.inactivity_minutes,
            volume_cap=self.extraction_volume_cap,
            has_new_turns=state.has_new_turns,
        )

    def run_extraction_if_due(
        self,
        session_id: UUID,
        scope: str,
        now: datetime | None = None,
    ) -> ExtractionRun:
        """Run the L1 job only when one of the two independent triggers fires."""
        check_time = now or datetime.now(UTC)
        trigger = self.extraction_status(session_id, scope, check_time)
        if not trigger.due:
            return ExtractionRun((), (), False, trigger)

        turn_ids = extract_pending_turns(
            database=self.database,
            session_id=session_id,
            scope=scope,
            judge=self.extraction_judge,
            volume_cap=self.extraction_volume_cap,
        )
        atoms = load_atoms_for_turns(self.database, turn_ids, scope)
        superseded_ids = [
            atom["supersedes"] for atom in atoms if atom.get("supersedes") is not None
        ]
        superseded_atoms = load_atoms_by_ids(self.database, superseded_ids, scope)
        self.qdrant_writer.index_records(atoms + superseded_atoms, [])
        return ExtractionRun(
            turn_ids=tuple(turn_ids),
            indexed_atom_ids=tuple(atom["id"] for atom in atoms),
            ran=bool(turn_ids),
            trigger=trigger,
        )

    def run_synthesis_job(self, scope: str) -> SynthesisRun:
        """Run the independent atom-volume L2 job and index changed rows."""
        atom_ids = run_synthesis(
            database=self.database,
            scope=scope,
            judge=self.synthesis_judge,
            atom_volume=self.synthesis_atom_volume,
        )
        rows = load_l2_rows_for_atoms(self.database, atom_ids, scope)
        self.qdrant_writer.index_records([], rows)
        return SynthesisRun(
            atom_ids=tuple(atom_ids),
            indexed_row_ids=tuple(row["id"] for row in rows),
            ran=bool(atom_ids),
        )

    def run_project_extraction(
        self,
        project_id: UUID,
        scope: str | None = None,
    ) -> ProjectExtractionRun:
        project_scope = scope or self._project_scope(project_id)
        turn_ids = extract_pending_turns(
            database=self.database,
            session_id=project_id,
            scope=project_scope,
            judge=self.project_extraction_judge,
            volume_cap=self.extraction_volume_cap,
            project_id=project_id,
            allow_single_source=True,
        )
        atoms = load_atoms_for_turns(
            self.database,
            turn_ids,
            project_scope,
            project_id=project_id,
        )
        superseded_ids = [
            atom["supersedes"] for atom in atoms if atom.get("supersedes") is not None
        ]
        superseded_atoms = load_atoms_by_ids(
            self.database,
            superseded_ids,
            project_scope,
            project_id=project_id,
        )
        self.qdrant_writer.index_records(atoms + superseded_atoms, [])
        return ProjectExtractionRun(
            project_id=project_id,
            turn_ids=tuple(turn_ids),
            indexed_atom_ids=tuple(atom["id"] for atom in atoms),
            ran=bool(turn_ids),
        )

    def run_project_synthesis_job(
        self,
        project_id: UUID,
        scope: str | None = None,
    ) -> ProjectSynthesisRun:
        if self.project_synthesis_judge is None:
            raise RuntimeError("project synthesis judge is not configured")
        project = self.database.fetch_one(
            "select scope, name from project_registry where id = %s",
            (project_id,),
        )
        if project is None:
            raise KeyError(f"project not found: {project_id}")
        project_scope = str(project["scope"])
        if scope is not None and scope != project_scope:
            raise ValueError(f"project scope does not match: {project_id}")
        atom_ids = run_project_synthesis(
            database=self.database,
            project_id=project_id,
            project_name=str(project["name"]),
            scope=project_scope,
            judge=self.project_synthesis_judge,
            atom_volume=self.synthesis_atom_volume,
        )
        sections = load_project_sections_for_project(
            self.database,
            project_id,
            project_scope,
        )
        self.qdrant_writer.index_records([], sections)
        return ProjectSynthesisRun(
            project_id=project_id,
            atom_ids=tuple(atom_ids),
            indexed_section_ids=tuple(section["id"] for section in sections),
            ran=bool(atom_ids),
        )

    def import_project_documents(
        self,
        folder: str,
        project_id: UUID,
    ) -> ProjectImportRun:
        """Import, extract, synthesize, and index one confirmed project."""
        if self.project_synthesis_judge is None:
            raise RuntimeError("project synthesis judge is not configured")
        import_result = self.project_document_importer.import_folder(folder, project_id)
        obsolete_ids = [
            *import_result.stale_atom_ids,
            *import_result.removed_section_ids,
        ]
        if obsolete_ids:
            self.qdrant_writer.delete(obsolete_ids)
        project_scope = self._project_scope(project_id)
        extracted_turn_ids: list[UUID] = []
        indexed_atom_ids: list[UUID] = []
        while True:
            extraction_run = self.run_project_extraction(project_id, project_scope)
            if not extraction_run.ran:
                break
            extracted_turn_ids.extend(extraction_run.turn_ids)
            indexed_atom_ids.extend(extraction_run.indexed_atom_ids)
        synthesized_atom_ids: list[UUID] = []
        while True:
            synthesis_run = self.run_project_synthesis_job(project_id, project_scope)
            if not synthesis_run.ran:
                break
            synthesized_atom_ids.extend(synthesis_run.atom_ids)
        sections = load_project_sections_for_project(
            self.database,
            project_id,
            project_scope,
        )
        self.qdrant_writer.index_records([], sections)
        return ProjectImportRun(
            import_result=import_result,
            extracted_turn_ids=tuple(extracted_turn_ids),
            synthesized_atom_ids=tuple(synthesized_atom_ids),
            indexed_atom_ids=tuple(indexed_atom_ids),
            indexed_section_ids=tuple(section["id"] for section in sections),
        )

    def _project_scope(self, project_id: UUID) -> str:
        project = self.database.fetch_one(
            "select scope from project_registry where id = %s",
            (project_id,),
        )
        if project is None:
            raise KeyError(f"project not found: {project_id}")
        return str(project["scope"])


def load_atoms_for_turns(
    database: Database,
    turn_ids: list[UUID],
    scope: str,
    project_id: UUID | None = None,
) -> list[dict[str, object]]:
    if not turn_ids:
        return []
    project_clause = " and a.project_id is null"
    params: list[object] = [turn_ids, scope]
    if project_id is not None:
        project_clause = " and a.project_id = %s"
        params.append(project_id)
    return database.fetch_all(
        f"""
         select distinct a.id, a.scope, a.statement, a.category, a.origin,
               a.scene_name, a.predicate, a.source_deleted,
               a.supersedes,
             a.project_id,
               exists (
                   select 1
                   from atoms as newer
                   where newer.supersedes = a.id and newer.scope = a.scope
               ) as superseded,
               e.name as entity
        from atoms as a
        join atom_sources as source on source.atom_id = a.id
        left join entities as e on e.id = a.entity_id
            where source.turn_id = any(%s) and a.scope = %s
              {project_clause}
        """,
            params,
    )


def load_atoms_by_ids(
    database: Database,
    atom_ids: list[object],
    scope: str,
    project_id: UUID | None = None,
) -> list[dict[str, object]]:
    if not atom_ids:
        return []
    project_clause = " and a.project_id is null"
    params: list[object] = [atom_ids, scope]
    if project_id is not None:
        project_clause = " and a.project_id = %s"
        params.append(project_id)
    return database.fetch_all(
        f"""
         select a.id, a.scope, a.statement, a.category, a.origin,
               a.scene_name, a.predicate, a.source_deleted,
               a.supersedes,
             a.project_id,
               exists (
                   select 1
                   from atoms as newer
                   where newer.supersedes = a.id and newer.scope = a.scope
               ) as superseded,
               e.name as entity
        from atoms as a
        left join entities as e on e.id = a.entity_id
            where a.id = any(%s) and a.scope = %s
              {project_clause}
        """,
            params,
    )


def load_l2_rows_for_atoms(
    database: Database,
    atom_ids: list[UUID],
    scope: str,
) -> list[dict[str, object]]:
    if not atom_ids:
        return []
    rows: list[dict[str, object]] = []
    for category, definition in sorted(L2_TABLES.items()):
        if definition.project_scoped:
            continue
        task_fields = ", row.owner, row.deadline, row.status" if definition.has_task_fields else ""
        rows.extend(
            database.fetch_all(
                f"""
                select distinct row.id, row.scope, %s as category,
                       row.title, row.summary{task_fields}
                from {definition.table_name} as row
                join {definition.junction_table} as link
                  on link.{definition.foreign_key} = row.id
                where link.atom_id = any(%s) and row.scope = %s
                """,
                (category, atom_ids, scope),
            )
        )
    return rows


def load_project_sections_for_project(
    database: Database,
    project_id: UUID,
    scope: str,
) -> list[dict[str, object]]:
    return database.fetch_all(
        """
        select id, scope, 'project_sections' as category, title, summary, project_id
        from project_sections
        where project_id = %s and scope = %s
        """,
        (project_id, scope),
    )
