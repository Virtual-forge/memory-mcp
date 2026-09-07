"""Import project documentation into versioned, provenance-rich L0 turns."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from memory_manager.db.connection import Database

SUPPORTED_DOCUMENT_EXTENSIONS = frozenset({".md", ".markdown"})
IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        "target",
    }
)
HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class DocumentChunk:
    relative_path: str
    heading: str
    chunk_index: int
    start_line: int
    end_line: int
    content: str


@dataclass(frozen=True)
class ProjectImportResult:
    project_id: UUID
    project_name: str
    imported_files: int
    unchanged_files: int
    deleted_files: int
    imported_chunks: int
    stale_atom_ids: tuple[UUID, ...] = ()
    removed_section_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class _DocumentFile:
    relative_path: str
    content: str
    content_hash: str
    byte_size: int
    chunks: tuple[DocumentChunk, ...]


def iter_document_files(folder: str | Path) -> list[Path]:
    """Return recursive Markdown files in stable relative-path order."""
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"documentation folder does not exist: {folder}")

    paths: list[Path] = []
    for current, directories, filenames in os.walk(root):
        current_path = Path(current)
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in IGNORED_DIRECTORIES
            and not (current_path / directory).is_symlink()
        )
        for filename in sorted(filenames):
            path = current_path / filename
            if path.is_symlink() or path.suffix.casefold() not in SUPPORTED_DOCUMENT_EXTENSIONS:
                continue
            paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix().casefold())


def split_document(text: str, max_chars: int = 4_000) -> list[tuple[str, int, int, str]]:
    """Split text at headings and paragraph boundaries while retaining line ranges."""
    if max_chars <= 0:
        raise ValueError("document chunk size must be positive")
    lines = text.splitlines()
    if not lines:
        return []

    blocks = _document_blocks(lines)
    chunks: list[tuple[str, int, int, str]] = []
    current_heading = "Document"
    current_start = 0
    current_end = 0
    current_parts: list[str] = []

    def flush() -> None:
        nonlocal current_parts
        if current_parts:
            chunks.append(
                (
                    current_heading,
                    current_start,
                    current_end,
                    "\n\n".join(current_parts).strip(),
                )
            )
            current_parts = []

    for heading, start_line, end_line, block_lines in blocks:
        block_text = "\n".join(block_lines).strip()
        if not block_text:
            continue
        if heading != current_heading:
            flush()
            current_heading = heading
        candidate = "\n\n".join([*current_parts, block_text])
        if current_parts and len(candidate) > max_chars:
            flush()
        if len(block_text) <= max_chars:
            if not current_parts:
                current_start = start_line
            current_end = end_line
            current_parts.append(block_text)
            continue

        flush()
        for part_start, part_end, part_text in _split_long_block(
            block_lines, start_line, max_chars
        ):
            chunks.append((heading, part_start, part_end, part_text))

    flush()
    return chunks


def _document_blocks(lines: list[str]) -> list[tuple[str, int, int, list[str]]]:
    blocks: list[tuple[str, int, int, list[str]]] = []
    heading = "Document"
    block_start: int | None = None
    block_lines: list[str] = []

    def flush(end_line: int) -> None:
        nonlocal block_start, block_lines
        if block_start is not None and block_lines:
            blocks.append((heading, block_start, end_line, block_lines))
        block_start = None
        block_lines = []

    for line_number, line in enumerate(lines, start=1):
        match = HEADING_PATTERN.match(line)
        if match:
            flush(line_number - 1)
            heading = match.group(1).strip()
            block_start = line_number
            block_lines = [line]
        elif line.strip():
            if block_start is None:
                block_start = line_number
            block_lines.append(line)
        elif block_lines:
            flush(line_number - 1)
    flush(len(lines))
    return blocks


def _split_long_block(
    lines: list[str],
    start_line: int,
    max_chars: int,
) -> list[tuple[int, int, str]]:
    parts: list[tuple[int, int, str]] = []
    current_lines: list[str] = []
    current_start = start_line
    current_end = start_line

    def flush() -> None:
        nonlocal current_lines
        if current_lines:
            parts.append((current_start, current_end, "\n".join(current_lines).strip()))
            current_lines = []

    for offset, line in enumerate(lines):
        line_number = start_line + offset
        if len(line) > max_chars:
            flush()
            for piece_start in range(0, len(line), max_chars):
                parts.append(
                    (
                        line_number,
                        line_number,
                        line[piece_start : piece_start + max_chars],
                    )
                )
            current_start = line_number + 1
            current_end = current_start
            continue
        candidate = "\n".join([*current_lines, line])
        if current_lines and len(candidate) > max_chars:
            flush()
            current_start = line_number
        if not current_lines:
            current_start = line_number
        current_lines.append(line)
        current_end = line_number
    flush()
    return parts


class ProjectDocumentImporter:
    """Import one confirmed project's supported documentation files."""

    def __init__(
        self,
        database: Database,
        max_file_bytes: int = 1_000_000,
        chunk_chars: int = 4_000,
    ):
        if max_file_bytes <= 0:
            raise ValueError("maximum document size must be positive")
        if chunk_chars <= 0:
            raise ValueError("document chunk size must be positive")
        self.database = database
        self.max_file_bytes = max_file_bytes
        self.chunk_chars = chunk_chars

    def import_folder(self, folder: str | Path, project_id: UUID) -> ProjectImportResult:
        root = Path(folder).expanduser().resolve()
        project = self.database.fetch_one(
            """
            select id, scope, name
            from project_registry
            where id = %s
            """,
            (project_id,),
        )
        if project is None:
            raise KeyError(f"project not found: {project_id}")

        documents = self._load_documents(root)
        return self._persist_documents(project, documents)

    def _load_documents(self, root: Path) -> dict[str, _DocumentFile]:
        documents: dict[str, _DocumentFile] = {}
        for path in iter_document_files(root):
            content_bytes = path.read_bytes()
            if len(content_bytes) > self.max_file_bytes:
                raise ValueError(
                    f"documentation file exceeds size limit ({self.max_file_bytes} bytes): {path}"
                )
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"documentation file is not UTF-8 text: {path}") from exc
            relative_path = path.relative_to(root).as_posix()
            chunks = tuple(
                DocumentChunk(
                    relative_path=relative_path,
                    heading=heading,
                    chunk_index=index,
                    start_line=start_line,
                    end_line=end_line,
                    content=chunk,
                )
                for index, (heading, start_line, end_line, chunk) in enumerate(
                    split_document(content, self.chunk_chars)
                )
            )
            documents[relative_path] = _DocumentFile(
                relative_path=relative_path,
                content=content,
                content_hash=hashlib.sha256(content_bytes).hexdigest(),
                byte_size=len(content_bytes),
                chunks=chunks,
            )
        return documents

    def _persist_documents(
        self,
        project: dict[str, Any],
        documents: dict[str, _DocumentFile],
    ) -> ProjectImportResult:
        project_id = UUID(str(project["id"]))
        scope = str(project["scope"])
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select id, relative_path, content_hash, is_active
                    from project_documents
                    where project_id = %s
                    """,
                    (project_id,),
                )
                existing_rows = list(cursor.fetchall())
                active_by_path = {
                    str(row["relative_path"]): row
                    for row in existing_rows
                    if row["is_active"]
                }
                imported_files = 0
                unchanged_files = 0
                imported_chunks = 0
                stale_atom_ids: set[UUID] = set()
                removed_section_ids: set[UUID] = set()

                for relative_path, document in documents.items():
                    existing = active_by_path.get(relative_path)
                    if existing and str(existing["content_hash"]) == document.content_hash:
                        unchanged_files += 1
                        continue

                    stale, removed = self._deactivate_path(cursor, project_id, relative_path)
                    stale_atom_ids.update(stale)
                    removed_section_ids.update(removed)
                    cursor.execute(
                        """
                        insert into project_documents (
                            project_id, relative_path, content_hash, byte_size, is_active
                        ) values (%s, %s, %s, %s, true)
                        on conflict (project_id, relative_path, content_hash) do update set
                            byte_size = excluded.byte_size,
                            is_active = true,
                            imported_at = now()
                        returning id
                        """,
                        (
                            project_id,
                            relative_path,
                            document.content_hash,
                            document.byte_size,
                        ),
                    )
                    document_id = cursor.fetchone()["id"]
                    for chunk in document.chunks:
                        turn_id = self._upsert_document_turn(
                            cursor,
                            project_id,
                            scope,
                            document,
                            chunk,
                        )
                        cursor.execute(
                            """
                            insert into project_document_chunks (
                                document_id, turn_id, chunk_index, heading,
                                start_line, end_line, is_active
                            ) values (%s, %s, %s, %s, %s, %s, true)
                            on conflict (document_id, chunk_index) do update set
                                turn_id = excluded.turn_id,
                                heading = excluded.heading,
                                start_line = excluded.start_line,
                                end_line = excluded.end_line,
                                is_active = true
                            """,
                            (
                                document_id,
                                turn_id,
                                chunk.chunk_index,
                                chunk.heading or "Document",
                                chunk.start_line,
                                chunk.end_line,
                            ),
                        )
                        imported_chunks += 1
                    imported_files += 1

                seen_paths = set(documents)
                deleted_paths = {
                    str(row["relative_path"])
                    for row in existing_rows
                    if row["is_active"] and str(row["relative_path"]) not in seen_paths
                }
                for relative_path in deleted_paths:
                    stale, removed = self._deactivate_path(cursor, project_id, relative_path)
                    stale_atom_ids.update(stale)
                    removed_section_ids.update(removed)

        return ProjectImportResult(
            project_id=project_id,
            project_name=str(project["name"]),
            imported_files=imported_files,
            unchanged_files=unchanged_files,
            deleted_files=len(deleted_paths),
            imported_chunks=imported_chunks,
            stale_atom_ids=tuple(sorted(stale_atom_ids, key=str)),
            removed_section_ids=tuple(sorted(removed_section_ids, key=str)),
        )

    @staticmethod
    def _deactivate_path(
        cursor: Any,
        project_id: UUID,
        relative_path: str,
    ) -> tuple[set[UUID], set[UUID]]:
        cursor.execute(
            """
            select distinct source.atom_id
            from atom_sources as source
            join project_document_chunks as chunk on chunk.turn_id = source.turn_id
            join project_documents as document on document.id = chunk.document_id
            join atoms as atom on atom.id = source.atom_id
            where document.project_id = %s
              and document.relative_path = %s
              and atom.project_id = %s
            """,
            (project_id, relative_path, project_id),
        )
        candidate_atom_ids = {UUID(str(row["atom_id"])) for row in cursor.fetchall()}

        cursor.execute(
            """
            update project_document_chunks as chunk
            set is_active = false
            from project_documents as document
            where chunk.document_id = document.id
              and document.project_id = %s
              and document.relative_path = %s
            """,
            (project_id, relative_path),
        )
        cursor.execute(
            """
            update project_documents
            set is_active = false
            where project_id = %s and relative_path = %s
            """,
            (project_id, relative_path),
        )

        stale_atom_ids: set[UUID] = set()
        removed_section_ids: set[UUID] = set()
        if candidate_atom_ids:
            cursor.execute(
                """
                update atoms as atom
                set source_deleted = true
                where atom.id = any(%s)
                  and atom.project_id = %s
                  and not exists (
                      select 1
                      from atom_sources as remaining_source
                      join project_document_chunks as remaining_chunk
                        on remaining_chunk.turn_id = remaining_source.turn_id
                      join project_documents as remaining_document
                        on remaining_document.id = remaining_chunk.document_id
                      where remaining_source.atom_id = atom.id
                        and remaining_document.project_id = %s
                        and remaining_document.is_active
                        and remaining_chunk.is_active
                  )
                returning atom.id
                """,
                (list(candidate_atom_ids), project_id, project_id),
            )
            stale_atom_ids = {
                UUID(str(row["id"])) for row in cursor.fetchall()
            }
            cursor.execute(
                """
                select distinct link.section_id
                from project_section_atoms as link
                where link.atom_id = any(%s)
                """,
                (list(candidate_atom_ids),),
            )
            section_ids = {UUID(str(row["section_id"])) for row in cursor.fetchall()}
            if section_ids:
                cursor.execute(
                    """
                    select distinct link.atom_id
                    from project_section_atoms as link
                    join atoms as atom on atom.id = link.atom_id
                    where link.section_id = any(%s)
                      and atom.project_id = %s
                      and not atom.source_deleted
                    """,
                    (list(section_ids), project_id),
                )
                refresh_atom_ids = [row["atom_id"] for row in cursor.fetchall()]
                if refresh_atom_ids:
                    cursor.execute(
                        """
                        delete from synthesis_coverage
                        where atom_id = any(%s)
                        """,
                        (refresh_atom_ids,),
                    )
                cursor.execute(
                    """
                    delete from project_sections
                    where project_id = %s and id = any(%s)
                    returning id
                    """,
                    (project_id, list(section_ids)),
                )
                removed_section_ids = {
                    UUID(str(row["id"])) for row in cursor.fetchall()
                }
        return stale_atom_ids, removed_section_ids

    @staticmethod
    def _upsert_document_turn(
        cursor: Any,
        project_id: UUID,
        scope: str,
        document: _DocumentFile,
        chunk: DocumentChunk,
    ) -> UUID:
        source_event_id = (
            f"project-document:{project_id}:{document.relative_path}:"
            f"{document.content_hash}:{chunk.chunk_index}"
        )
        cursor.execute(
            """
            insert into turns (
                session_id, scope, source, content, tool_name, tool_call_id,
                source_event_id, source_path, source_heading, source_start_line,
                source_end_line, source_hash
            ) values (%s, %s, 'tool', %s, 'project_document', %s, %s, %s, %s, %s, %s, %s)
            on conflict (session_id, scope, source_event_id) do update set
                content = excluded.content,
                tool_call_id = excluded.tool_call_id,
                source_path = excluded.source_path,
                source_heading = excluded.source_heading,
                source_start_line = excluded.source_start_line,
                source_end_line = excluded.source_end_line,
                source_hash = excluded.source_hash
            returning id
            """,
            (
                project_id,
                scope,
                chunk.content,
                source_event_id,
                source_event_id,
                document.relative_path,
                chunk.heading or "Document",
                chunk.start_line,
                chunk.end_line,
                document.content_hash,
            ),
        )
        return UUID(str(cursor.fetchone()["id"]))