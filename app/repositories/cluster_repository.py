"""
app/repositories/cluster_repository.py

ClusterRepository — resolves a cluster name to a kubeconfig file path.

Design goals:
  - API layer never needs to know where configs are stored.
  - Backing store can be swapped (filesystem → DB / Vault) without touching
    any caller — just replace this class.
  - One kubeconfig file per cluster:  <base_path>/<cluster-name>.yaml
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.core.exceptions import ClusterNotFoundException
from app.domain.kubernetes_models import ClusterInfo

_logger = logging.getLogger(__name__)


class ClusterRepository:
    """Filesystem-backed store that maps cluster names to kubeconfig paths.

    Args:
        base_path: Directory that contains ``<cluster>.yaml`` files.
                   Sourced from ``Settings.KUBECONFIG_BASE_PATH`` so that
                   the path is configurable without touching code.
    """

    def __init__(self, base_path: str) -> None:
        self._base = Path(base_path)

    # ── Public API ────────────────────────────────────────────────────────────

    def get_kubeconfig(self, cluster: str) -> Path:
        """Return the absolute path to the kubeconfig file for *cluster*.

        Raises:
            ClusterNotFoundException: If no ``<cluster>.yaml`` exists in the
                configured base directory.
        """
        path = self._base / f"{cluster}.yaml"
        if not path.exists():
            _logger.info(
                "Cluster kubeconfig not found | cluster=%s | path=%s",
                cluster,
                path,
            )
            raise ClusterNotFoundException(
                f"Cluster '{cluster}' not found. "
                f"No kubeconfig at '{path}'.",
            )
        _logger.debug("Resolved kubeconfig | cluster=%s | path=%s", cluster, path)
        return path.resolve()

    def list_clusters(self) -> list[ClusterInfo]:
        """Return metadata for all registered clusters.

        Scans the base directory for ``*.yaml`` files; each file represents
        one cluster.  Returns an empty list when the directory is absent or
        contains no yaml files.
        """
        if not self._base.exists():
            _logger.warning(
                "Kubeconfig base directory does not exist | path=%s", self._base
            )
            return []

        clusters: list[ClusterInfo] = []
        for config_file in sorted(self._base.glob("*.yaml")):
            clusters.append(
                ClusterInfo(
                    name=config_file.stem,
                    kubeconfig_path=str(config_file.resolve()),
                )
            )

        _logger.debug("Listed %d cluster(s) from %s", len(clusters), self._base)
        return clusters
