from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from memory_manager.ingestion.project_docs import (
    ProjectDocumentImporter,
    iter_document_files,
    split_document,
)


class ImportDatabase:
    def __init__(self, project_id: UUID):
        self.project = {
            "id": project_id,
            "scope": "project:demo",
            "name": "Demo Project",
        }
        self.documents = []
        self.chunks = []
        self.turns = {}
        self.atom_ids_by_path = {}
        self.section_ids_by_atom = {}

    def fetch_one(self, query, params=()):
        return self.project

    @contextmanager
    def transaction(self):
        yield ImportConnection(self)


class ImportConnection:
    def __init__(self, database):
        self.database = database

    def cursor(self):
        return ImportCursor(self.database)


class ImportCursor:
    def __init__(self, database):
        self.database = database
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=()):
        normalized = " ".join(query.split()).lower()
        self.result = []
        if normalized.startswith("select id, relative_path, content_hash, is_active"):
            self.result = [dict(row) for row in self.database.documents]
        elif "select distinct source.atom_id" in normalized:
            relative_path = str(params[1])
            self.result = [
                {"atom_id": atom_id}
                for atom_id in self.database.atom_ids_by_path.get(relative_path, set())
            ]
        elif "update project_document_chunks as chunk" in normalized:
            relative_path = str(params[1])
            document_ids = {
                row["id"]
                for row in self.database.documents
                if row["relative_path"] == relative_path
            }
            for row in self.database.chunks:
                if row["document_id"] in document_ids:
                    row["is_active"] = False
        elif normalized.startswith("update project_documents"):
            relative_path = str(params[1])
            for row in self.database.documents:
                if row["relative_path"] == relative_path:
                    row["is_active"] = False
        elif normalized.startswith("update atoms as atom"):
            atom_ids = params[0]
            self.result = [{"id": atom_id} for atom_id in atom_ids]
        elif "select distinct link.section_id" in normalized:
            section_ids = set()
            for atom_id in params[0]:
                section_ids.update(self.database.section_ids_by_atom.get(atom_id, set()))
            self.result = [{"section_id": section_id} for section_id in section_ids]
        elif "select distinct link.atom_id" in normalized:
            self.result = []
        elif normalized.startswith("delete from synthesis_coverage"):
            self.result = []
        elif normalized.startswith("delete from project_sections"):
            self.result = [{"id": section_id} for section_id in params[1]]
        elif normalized.startswith("insert into project_documents"):
            project_id, relative_path, content_hash, byte_size = params
            existing = next(
                (
                    row
                    for row in self.database.documents
                    if row["relative_path"] == relative_path
                    and row["content_hash"] == content_hash
                ),
                None,
            )
            if existing is None:
                existing = {
                    "id": uuid4(),
                    "relative_path": relative_path,
                    "content_hash": content_hash,
                    "is_active": True,
                    "project_id": project_id,
                    "byte_size": byte_size,
                }
                self.database.documents.append(existing)
            else:
                existing["is_active"] = True
                existing["byte_size"] = byte_size
            self.result = [{"id": existing["id"]}]
        elif normalized.startswith("insert into turns"):
            source_event_id = params[3]
            turn_id = self.database.turns.setdefault(source_event_id, uuid4())
            self.result = [{"id": turn_id}]
        elif normalized.startswith("insert into project_document_chunks"):
            document_id, turn_id, chunk_index, heading, start_line, end_line = params
            existing = next(
                (
                    row
                    for row in self.database.chunks
                    if row["document_id"] == document_id
                    and row["chunk_index"] == chunk_index
                ),
                None,
            )
            if existing is None:
                self.database.chunks.append(
                    {
                        "document_id": document_id,
                        "turn_id": turn_id,
                        "chunk_index": chunk_index,
                        "heading": heading,
                        "start_line": start_line,
                        "end_line": end_line,
                        "is_active": True,
                    }
                )
            else:
                existing.update(
                    {
                        "turn_id": turn_id,
                        "heading": heading,
                        "start_line": start_line,
                        "end_line": end_line,
                        "is_active": True,
                    }
                )
        else:
            raise AssertionError(f"unhandled importer query: {normalized[:100]}")

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result)


def test_document_scan_includes_nested_markdown_and_preserves_relative_paths(tmp_path: Path):
    (tmp_path / "architecture").mkdir()
    (tmp_path / "operations" / "runbooks").mkdir(parents=True)
    (tmp_path / "README.md").write_text("# Overview\n\nProject overview.", encoding="utf-8")
    (tmp_path / "architecture" / "system.markdown").write_text(
        "# System\n\nThe system uses Postgres.", encoding="utf-8"
    )
    (tmp_path / "operations" / "runbooks" / "deploy.md").write_text(
        "# Deploy\n\nRun the deploy command.", encoding="utf-8"
    )
    (tmp_path / "architecture" / "notes.txt").write_text("ignore me", encoding="utf-8")

    assert [path.relative_to(tmp_path).as_posix() for path in iter_document_files(tmp_path)] == [
        "architecture/system.markdown",
        "operations/runbooks/deploy.md",
        "README.md",
    ]


def test_document_chunks_keep_heading_and_line_ranges():
    chunks = split_document(
        "# Architecture\n\nUses Postgres.\n\n## Retrieval\n\nUses Qdrant."
    )

    assert [(heading, start, end) for heading, start, end, _ in chunks] == [
        ("Architecture", 1, 3),
        ("Retrieval", 5, 7),
    ]


def test_import_versions_unchanged_changed_and_deleted_markdown_files(tmp_path: Path):
    (tmp_path / "architecture").mkdir()
    (tmp_path / "operations").mkdir()
    system_path = tmp_path / "architecture" / "system.md"
    runbook_path = tmp_path / "operations" / "runbook.markdown"
    system_path.write_text("# Architecture\n\nUses Postgres.", encoding="utf-8")
    runbook_path.write_text("# Operations\n\nDeploy with the worker.", encoding="utf-8")
    (tmp_path / "operations" / "notes.txt").write_text("ignore me", encoding="utf-8")

    project_id = uuid4()
    database = ImportDatabase(project_id)
    importer = ProjectDocumentImporter(database)

    first = importer.import_folder(tmp_path, project_id)
    assert first.imported_files == 2
    assert first.unchanged_files == 0
    assert first.deleted_files == 0
    assert first.imported_chunks == 2

    unchanged = importer.import_folder(tmp_path, project_id)
    assert unchanged.imported_files == 0
    assert unchanged.unchanged_files == 2
    assert unchanged.imported_chunks == 0

    atom_id = uuid4()
    section_id = uuid4()
    database.atom_ids_by_path["architecture/system.md"] = {atom_id}
    database.section_ids_by_atom[atom_id] = {section_id}
    system_path.write_text(
        "# Architecture\n\nUses Postgres and Qdrant.",
        encoding="utf-8",
    )
    runbook_path.unlink()

    changed = importer.import_folder(tmp_path, project_id)
    assert changed.imported_files == 1
    assert changed.unchanged_files == 0
    assert changed.deleted_files == 1
    assert changed.imported_chunks == 1
    assert changed.stale_atom_ids == (atom_id,)
    assert changed.removed_section_ids == (section_id,)

    active_paths = {
        row["relative_path"] for row in database.documents if row["is_active"]
    }
    assert active_paths == {"architecture/system.md"}
    assert sum(
        row["relative_path"] == "architecture/system.md" for row in database.documents
    ) == 2
    assert all(
        row["is_active"] is False
        for row in database.chunks
        if row["document_id"] != next(
            document["id"]
            for document in database.documents
            if document["is_active"]
            and document["relative_path"] == "architecture/system.md"
        )
    )


def test_import_requires_a_registered_project_before_loading_documents(tmp_path: Path):
    class MissingProjectDatabase:
        def fetch_one(self, query, params=()):
            return None

    with pytest.raises(KeyError, match="project not found"):
        ProjectDocumentImporter(MissingProjectDatabase()).import_folder(tmp_path, uuid4())


def test_deactivate_path_deactivates_chunks_before_marking_atoms_stale():
    project_id = uuid4()
    atom_id = uuid4()

    class Cursor:
        def __init__(self):
            self.queries = []
            self.last_query = ""

        def execute(self, query, params=()):
            self.queries.append(query)
            self.last_query = query

        def fetchall(self):
            if "select distinct source.atom_id" in self.last_query:
                return [{"atom_id": atom_id}]
            if "returning atom.id" in self.last_query:
                return [{"id": atom_id}]
            return []

    cursor = Cursor()
    stale_atom_ids, removed_section_ids = ProjectDocumentImporter._deactivate_path(
        cursor,
        project_id,
        "architecture/system.md",
    )

    assert stale_atom_ids == {UUID(str(atom_id))}
    assert removed_section_ids == set()
    chunk_update = next(
        index
        for index, query in enumerate(cursor.queries)
        if "update project_document_chunks" in query
    )
    atom_update = next(
        index for index, query in enumerate(cursor.queries) if "update atoms as atom" in query
    )
    assert chunk_update < atom_update
