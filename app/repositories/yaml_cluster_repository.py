"""
app/repositories/yaml_cluster_repository.py

YamlClusterRepository — loads cluster config from a kubeconfig YAML file.

File layout (one file per cluster):
    <KUBECONFIG_BASE_PATH>/
        <cluster-name>.yaml   ← standard kubeconfig YAML
        <cluster-name>.yaml
        …

The cluster name is the filename stem (without .yaml extension).
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.core.exceptions import ClusterNotFoundException
from app.domain.kubernetes_models import ClusterInfo, KubeClientConfig
from app.repositories.cluster_repository import ClusterRepository

_logger = logging.getLogger(__name__)


class YamlClusterRepository(ClusterRepository):
    """Filesystem-backed repository that resolves cluster names to kubeconfig paths.

    Args:
        base_path: Directory containing ``<cluster>.yaml`` kubeconfig files.
                   Sourced from ``Settings.KUBECONFIG_BASE_PATH``.
    """

    def __init__(self, base_path: str) -> None:
        self._base = Path(base_path)

    # ── ClusterRepository interface ───────────────────────────────────────────

    def get_kube_client_config(self, cluster: str) -> KubeClientConfig:
        """Resolve *cluster* to a KubeClientConfig backed by a YAML kubeconfig.

        Raises:
            ClusterNotFoundException: If no ``<cluster>.yaml`` exists.
        """
        path = self._base / f"{cluster}.yaml"
        if not path.exists():
            _logger.info(
                "YAML kubeconfig not found | cluster=%s | path=%s",
                cluster,
                path,
            )
            raise ClusterNotFoundException(
                f"Cluster '{cluster}' not found. "
                f"No kubeconfig at '{path}'.",
            )
        resolved = path.resolve()
        _logger.debug("Resolved YAML kubeconfig | cluster=%s | path=%s", cluster, resolved)
        return KubeClientConfig(
            cluster_name=cluster,
            source="yaml",
            kubeconfig_path=resolved,
        )

    def list_clusters(self) -> list[ClusterInfo]:
        """Scan the base directory for ``*.yaml`` files and return cluster metadata."""
        if not self._base.exists():
            _logger.warning(
                "Kubeconfig base directory does not exist | path=%s", self._base
            )
            return []

        clusters = [
            ClusterInfo(name=p.stem, source="yaml")
            for p in sorted(self._base.glob("*.yaml"))
        ]
        _logger.debug("YAML repo listed %d cluster(s) from %s", len(clusters), self._base)
        return clusters
