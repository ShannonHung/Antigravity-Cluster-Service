"""
app/services/cluster_manager.py

ClusterManager — high-level cluster operations (listing, metadata).

Delegates to the injected ClusterRepository, which determines
the backing store (filesystem YAML, JSON credentials, remote API, …).
"""

from __future__ import annotations

import logging

from app.domain.kubernetes_models import ClusterListData
from app.repositories.cluster_repository import ClusterRepository

_logger = logging.getLogger(__name__)


class ClusterManager:
    """Orchestrates cluster-level operations.

    Args:
        cluster_repo: Any concrete ClusterRepository implementation.
    """

    def __init__(self, cluster_repo: ClusterRepository) -> None:
        self._repo = cluster_repo

    def list_clusters(self) -> ClusterListData:
        """Return metadata for all registered clusters."""
        clusters = self._repo.list_clusters()
        _logger.info("Listed %d cluster(s)", len(clusters))
        return ClusterListData(clusters=clusters)
