"""
tests/unit/test_cluster_repository.py

Unit tests for ClusterRepository.

No real filesystem needed outside of the temp dir created by pytest's
``tmp_path`` fixture — the repository is fully isolated.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ClusterNotFoundException
from app.domain.kubernetes_models import ClusterInfo
from app.repositories.cluster_repository import ClusterRepository


# ── Helpers ───────────────────────────────────────────────────────────────────

def _repo(tmp_path) -> ClusterRepository:
    """Build a repository pointing at a fresh temp directory."""
    return ClusterRepository(base_path=str(tmp_path))


def _write_kubeconfig(tmp_path, cluster_name: str) -> None:
    """Write a minimal YAML stub so the file exists."""
    (tmp_path / f"{cluster_name}.yaml").write_text(
        f"# kubeconfig for {cluster_name}\napiVersion: v1\n"
    )


# ── get_kubeconfig ────────────────────────────────────────────────────────────

def test_get_kubeconfig_returns_path(tmp_path):
    _write_kubeconfig(tmp_path, "prod")
    repo = _repo(tmp_path)

    path = repo.get_kubeconfig("prod")

    assert path.name == "prod.yaml"
    assert path.exists()


def test_get_kubeconfig_raises_when_missing(tmp_path):
    repo = _repo(tmp_path)

    with pytest.raises(ClusterNotFoundException) as exc_info:
        repo.get_kubeconfig("nonexistent")

    assert "nonexistent" in str(exc_info.value)


def test_get_kubeconfig_path_is_resolved(tmp_path):
    """Returned path should be absolute (resolve() applied)."""
    _write_kubeconfig(tmp_path, "staging")
    repo = _repo(tmp_path)

    path = repo.get_kubeconfig("staging")

    assert path.is_absolute()


# ── list_clusters ─────────────────────────────────────────────────────────────

def test_list_clusters_returns_all_yaml_files(tmp_path):
    _write_kubeconfig(tmp_path, "cluster-a")
    _write_kubeconfig(tmp_path, "cluster-b")
    repo = _repo(tmp_path)

    result = repo.list_clusters()

    assert len(result) == 2
    names = {c.name for c in result}
    assert names == {"cluster-a", "cluster-b"}


def test_list_clusters_returns_cluster_info_objects(tmp_path):
    _write_kubeconfig(tmp_path, "dev")
    repo = _repo(tmp_path)

    result = repo.list_clusters()

    assert len(result) == 1
    assert isinstance(result[0], ClusterInfo)
    assert result[0].name == "dev"
    assert result[0].kubeconfig_path.endswith("dev.yaml")


def test_list_clusters_ignores_non_yaml_files(tmp_path):
    _write_kubeconfig(tmp_path, "valid")
    (tmp_path / "readme.txt").write_text("ignore me")
    (tmp_path / "notes.json").write_text("{}")
    repo = _repo(tmp_path)

    result = repo.list_clusters()

    assert len(result) == 1
    assert result[0].name == "valid"


def test_list_clusters_empty_when_directory_empty(tmp_path):
    repo = _repo(tmp_path)
    assert repo.list_clusters() == []


def test_list_clusters_empty_when_directory_missing():
    repo = ClusterRepository(base_path="/tmp/this-path-does-not-exist-xyz")
    assert repo.list_clusters() == []


def test_list_clusters_sorted_alphabetically(tmp_path):
    for name in ["zzz-cluster", "aaa-cluster", "mmm-cluster"]:
        _write_kubeconfig(tmp_path, name)
    repo = _repo(tmp_path)

    result = repo.list_clusters()

    assert [c.name for c in result] == ["aaa-cluster", "mmm-cluster", "zzz-cluster"]
