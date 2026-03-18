"""
tests/unit/test_cluster_repository.py

Unit tests for YamlClusterRepository (the concrete filesystem-backed impl).
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ClusterNotFoundException
from app.domain.kubernetes_models import ClusterInfo, KubeClientConfig
from app.repositories.yaml_cluster_repository import YamlClusterRepository


# ── Helpers ───────────────────────────────────────────────────────────────────

def _repo(tmp_path) -> YamlClusterRepository:
    return YamlClusterRepository(base_path=str(tmp_path))


def _write_kubeconfig(tmp_path, cluster_name: str) -> None:
    (tmp_path / f"{cluster_name}.yaml").write_text(
        f"# kubeconfig for {cluster_name}\napiVersion: v1\n"
    )


# ── get_kube_client_config ────────────────────────────────────────────────────

def test_get_kube_client_config_returns_yaml_config(tmp_path):
    _write_kubeconfig(tmp_path, "prod")
    cfg = _repo(tmp_path).get_kube_client_config("prod")

    assert isinstance(cfg, KubeClientConfig)
    assert cfg.source == "yaml"
    assert cfg.cluster_name == "prod"
    assert cfg.kubeconfig_path is not None
    assert cfg.kubeconfig_path.name == "prod.yaml"


def test_get_kube_client_config_raises_when_missing(tmp_path):
    with pytest.raises(ClusterNotFoundException) as exc_info:
        _repo(tmp_path).get_kube_client_config("nonexistent")

    assert "nonexistent" in str(exc_info.value)


def test_get_kube_client_config_path_is_absolute(tmp_path):
    _write_kubeconfig(tmp_path, "staging")
    cfg = _repo(tmp_path).get_kube_client_config("staging")
    assert cfg.kubeconfig_path.is_absolute()


# ── list_clusters ─────────────────────────────────────────────────────────────

def test_list_clusters_returns_all_yaml_files(tmp_path):
    _write_kubeconfig(tmp_path, "cluster-a")
    _write_kubeconfig(tmp_path, "cluster-b")

    result = _repo(tmp_path).list_clusters()

    assert len(result) == 2
    names = {c.name for c in result}
    assert names == {"cluster-a", "cluster-b"}


def test_list_clusters_returns_cluster_info_with_source(tmp_path):
    _write_kubeconfig(tmp_path, "dev")
    result = _repo(tmp_path).list_clusters()

    assert len(result) == 1
    assert isinstance(result[0], ClusterInfo)
    assert result[0].name == "dev"
    assert result[0].source == "yaml"


def test_list_clusters_ignores_non_yaml_files(tmp_path):
    _write_kubeconfig(tmp_path, "valid")
    (tmp_path / "readme.txt").write_text("ignore me")
    (tmp_path / "creds.json").write_text("{}")

    result = _repo(tmp_path).list_clusters()

    assert len(result) == 1
    assert result[0].name == "valid"


def test_list_clusters_empty_when_directory_empty(tmp_path):
    assert _repo(tmp_path).list_clusters() == []


def test_list_clusters_empty_when_directory_missing():
    repo = YamlClusterRepository(base_path="/tmp/does-not-exist-xyz-abc")
    assert repo.list_clusters() == []


def test_list_clusters_sorted_alphabetically(tmp_path):
    for name in ["zzz-cluster", "aaa-cluster", "mmm-cluster"]:
        _write_kubeconfig(tmp_path, name)

    result = _repo(tmp_path).list_clusters()

    assert [c.name for c in result] == ["aaa-cluster", "mmm-cluster", "zzz-cluster"]
