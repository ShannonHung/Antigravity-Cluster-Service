"""
app/services/cluster_manager.py

ClusterManager — high-level Kubernetes cluster orchestration.

Thin service that brokers between the API layer and ClusterRepository.
Keeping business logic here (not in routes) makes it easily testable
and replaceable without touching the HTTP interface.
"""

from __future__ import annotations

import logging

from app.domain.kubernetes_models import ClusterListData
from app.repositories.cluster_repository import ClusterRepository

_logger = logging.getLogger(__name__)


class ClusterManager:
    """Orchestrates cluster-level operations."""

    def __init__(self, cluster_repo: ClusterRepository) -> None:
        self._repo = cluster_repo

    # ── Public API ────────────────────────────────────────────────────────────

    def list_clusters(self) -> ClusterListData:
        """Return metadata for every registered cluster.

        Delegates to the repository so the API route never needs to know
        where configs are stored.
        """
        clusters = self._repo.list_clusters()
        _logger.info("Listed %d cluster(s)", len(clusters))
        return ClusterListData(clusters=clusters)
