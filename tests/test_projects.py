import pytest

from memory_manager.projects import normalize_project_name, rank_project_candidates


def test_project_names_normalize_case_and_whitespace():
    assert normalize_project_name("  Memory   Manager ") == "memory manager"
    with pytest.raises(ValueError, match="must not be empty"):
        normalize_project_name("  ")


def test_project_candidates_prioritize_exact_and_alias_matches():
    projects = [
        {"id": "partial", "name": "Memory Manager Tools", "aliases": []},
        {"id": "alias", "name": "Workbench", "aliases": ["Memory Manager"]},
        {"id": "exact", "name": "Memory Manager", "aliases": []},
    ]

    matches = rank_project_candidates("memory manager", projects)

    assert [project["id"] for project in matches] == ["exact", "alias", "partial"]


def test_project_candidates_match_compact_product_name_variants():
    projects = [
        {"id": "vscode", "name": "VS Code Approval Extension", "aliases": []},
    ]

    matches = rank_project_candidates("vscode extension", projects)

    assert [project["id"] for project in matches] == ["vscode"]


def test_project_candidates_match_spaced_product_name_variants():
    projects = [
        {"id": "vscode", "name": "VS Code Approval Extension", "aliases": []},
    ]

    matches = rank_project_candidates("vs code", projects)

    assert [project["id"] for project in matches] == ["vscode"]