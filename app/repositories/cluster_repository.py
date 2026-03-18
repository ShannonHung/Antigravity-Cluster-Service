"""
app/repositories/cluster_repository.py

Abstract interface for cluster configuration access.

Dependency Inversion Principle:
  - Service layer depends on this abstract interface (ClusterRepository).
  - Concrete implementations (YamlClusterRepository, JsonClusterRepository, …)
    live in separate files and are swappable without touching callers.

Contract:
  get_kube_client_config(cluster) → KubeClientConfig
  list_clusters()                 → list[ClusterInfo]
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.kubernetes_models import ClusterInfo, KubeClientConfig


class ClusterRepository(ABC):
    """Abstract contract for cluster configuration access.

    Implementations may read from:
      - Local YAML kubeconfig files  (YamlClusterRepository)
      - Local JSON credential files  (JsonClusterRepository)
      - Remote API / secrets vault   (future)
    """

    @abstractmethod
    def get_kube_client_config(self, cluster: str) -> KubeClientConfig:
        """Resolve *cluster* name → a ``KubeClientConfig`` ready for the factory.

        Raises:
            ClusterNotFoundException: If the cluster cannot be found.
        """

    @abstractmethod
    def list_clusters(self) -> list[ClusterInfo]:
        """Return metadata for all known clusters (empty list if none found)."""
