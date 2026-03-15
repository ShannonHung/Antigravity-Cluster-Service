"""
app/api/v1/clusters.py

Cluster-level endpoints (v1).

Routes:
  GET /api/v1/clusters                      → list all registered clusters
  GET /api/v1/clusters/{cluster}/nodes      → list nodes in a cluster

All endpoints require the ``cluster_api`` scope.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.domain.kubernetes_models import ClusterListData, NodeListData
from app.domain.models import ApiResponse, User
from app.repositories.cluster_repository import ClusterRepository
from app.services.cluster_manager import ClusterManager
from app.services.kube_client import KubeClientFactory
from app.services.node_service import NodeService

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clusters", tags=["clusters"])


# ── Dependency providers ──────────────────────────────────────────────────────

def _get_cluster_repo() -> ClusterRepository:
    settings = get_settings()
    return ClusterRepository(settings.KUBECONFIG_BASE_PATH)


def _get_cluster_manager(
    repo: ClusterRepository = Depends(_get_cluster_repo),
) -> ClusterManager:
    return ClusterManager(cluster_repo=repo)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


# ── GET /api/v1/clusters ──────────────────────────────────────────────────────

@router.get(
    "",
    response_model=ApiResponse[ClusterListData],
    summary="List all registered clusters",
    description=(
        "Returns the names and kubeconfig paths of every cluster "
        "registered under the configured kubeconfig directory."
    ),
)
async def list_clusters(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    mgr: ClusterManager = Depends(_get_cluster_manager),
) -> ApiResponse[ClusterListData]:
    data = mgr.list_clusters()
    return ApiResponse(data=data, request_id=_request_id(request))


# ── GET /api/v1/clusters/{cluster}/nodes ─────────────────────────────────────

@router.get(
    "/{cluster}/nodes",
    response_model=ApiResponse[NodeListData],
    summary="List nodes in a cluster",
    description=(
        "Returns all nodes in the given cluster with their status, "
        "roles, version, and schedulability."
    ),
)
async def list_nodes(
    request: Request,
    cluster: str,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    repo: ClusterRepository = Depends(_get_cluster_repo),
) -> ApiResponse[NodeListData]:
    kubeconfig_path = repo.get_kubeconfig(cluster)
    kube = KubeClientFactory().get_core_v1(kubeconfig_path)
    data = NodeService().list_nodes(cluster=cluster, kube=kube)
    return ApiResponse(data=data, request_id=_request_id(request))
