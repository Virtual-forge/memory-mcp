from uuid import uuid4

import pytest

from memory_manager.config import Settings
from memory_manager.mcp.tools import (
    MemoryTools,
    parse_uuid,
    resolve_read_scope,
    validate_atom_category,
)
from memory_manager.projects import find_project_candidates
from memory_manager.retrieval.provenance import validate_source_table


def test_mcp_atom_category_guard():
    validate_atom_category("decision")
    with pytest.raises(ValueError, match="unsupported atom category"):
        validate_atom_category("project")


def test_mcp_ids_are_uuid_only():
    value = uuid4()
    assert parse_uuid(str(value)) == value
    with pytest.raises(ValueError, match="must be a UUID"):
        parse_uuid("not-an-id")


def test_read_scope_can_be_explicitly_broadened():
    assert resolve_read_scope("workspace", False, "default") == "workspace"
    assert resolve_read_scope(None, False, "default") == "default"
    assert resolve_read_scope(None, True, "default") is None
    with pytest.raises(ValueError, match="cannot be used together"):
        resolve_read_scope("workspace", True, "default")


def test_settings_derive_user_scope_when_no_scope_is_configured():
    settings = Settings(_env_file=None, memory_user_id="alice")

    assert settings.default_scope == "user:alice"
    assert settings.resolved_default_scope == "user:alice"


def test_settings_preserve_an_explicit_scope_override():
    settings = Settings(
        _env_file=None,
        memory_user_id="alice",
        default_scope="project:inventory",
    )

    assert settings.resolved_default_scope == "project:inventory"


def test_settings_use_a_longer_configurable_mcp_timeout():
    assert Settings(_env_file=None).memory_mcp_timeout_seconds == 60
    assert (
        Settings(_env_file=None, memory_mcp_timeout_seconds=120).memory_mcp_timeout_seconds
        == 120
    )

    with pytest.raises(ValueError, match="setting must be positive"):
        Settings(_env_file=None, memory_mcp_timeout_seconds=0)


def test_drill_down_source_table_is_allowlisted():
    validate_source_table("atoms")
    validate_source_table("projects")
    with pytest.raises(ValueError, match="unsupported provenance source table"):
        validate_source_table("turns")


def test_project_lookup_is_deterministic_and_scope_bound():
    class ProjectDatabase:
        def fetch_all(self, query, params=()):
            assert params == ["user:alice"]
            return [
                {
                    "id": "partial",
                    "scope": "user:alice",
                    "name": "Memory Manager Tools",
                    "aliases": [],
                    "root_path": None,
                },
                {
                    "id": "exact",
                    "scope": "user:alice",
                    "name": "Memory Manager",
                    "aliases": [],
                    "root_path": None,
                },
            ]

    assert [
        project["id"]
        for project in find_project_candidates(
            ProjectDatabase(), "memory manager", "user:alice"
        )
    ] == ["exact", "partial"]


def test_project_lookup_limits_candidates_for_structured_feedback():
    class ProjectDatabase:
        def fetch_all(self, query, params=()):
            return [
                {
                    "id": str(index),
                    "scope": "user:alice",
                    "name": f"Memory Manager {index}",
                    "aliases": [],
                    "root_path": None,
                }
                for index in range(5)
            ]

    assert len(
        find_project_candidates(ProjectDatabase(), "memory manager", "user:alice", limit=3)
    ) == 3


def test_project_service_discovers_projects_across_scopes_by_default():
    class ProjectDatabase:
        def fetch_all(self, query, params=()):
            assert params == []
            return [
                {
                    "id": "vscode",
                    "scope": "project:vscode-approval-extension",
                    "name": "VS Code Approval Extension",
                    "aliases": [],
                    "root_path": None,
                }
            ]

    tools = MemoryTools(ProjectDatabase(), object(), object(), "user:alice")

    assert tools.resolve_project("vs code") == [
        {
            "id": "vscode",
            "scope": "project:vscode-approval-extension",
            "name": "VS Code Approval Extension",
            "aliases": [],
            "root_path": None,
        }
    ]


def test_project_service_lists_all_registered_scopes_for_selection():
    class ProjectDatabase:
        def fetch_all(self, query, params=()):
            assert params == []
            return [
                {
                    "id": "project-a",
                    "scope": "project:a",
                    "name": "Project A",
                    "aliases": [],
                    "root_path": None,
                },
                {
                    "id": "project-b",
                    "scope": "project:b",
                    "name": "Project B",
                    "aliases": [],
                    "root_path": None,
                },
            ]

    tools = MemoryTools(ProjectDatabase(), object(), object(), "user:alice")

    assert [project["name"] for project in tools.list_projects()] == [
        "Project A",
        "Project B",
    ]


def test_project_service_preserves_explicit_project_scope_filter():
    class ProjectDatabase:
        def fetch_all(self, query, params=()):
            assert params == ["project:b"]
            return [
                {
                    "id": "project-b",
                    "scope": "project:b",
                    "name": "Project B",
                    "aliases": [],
                    "root_path": None,
                }
            ]

    tools = MemoryTools(ProjectDatabase(), object(), object(), "user:alice")

    assert tools.list_projects("project:b")[0]["scope"] == "project:b"


def test_project_service_rejects_unrepresentable_selection_size():
    class ProjectDatabase:
        def fetch_all(self, query, params=()):
            return [
                {
                    "id": f"project-{index}",
                    "scope": f"project:{index}",
                    "name": f"Project {index}",
                    "aliases": [],
                    "root_path": None,
                }
                for index in range(5)
            ]

    tools = MemoryTools(ProjectDatabase(), object(), object(), "user:alice")

    with pytest.raises(ValueError, match="supports at most 3 projects plus Other options"):
        tools.list_projects()
