"""
app/api/v1/nodes.py

Node-level operation endpoints (v1).

Routes:
  POST /api/v1/clusters/{cluster}/nodes/{node}/cordon    → cordon a node
  POST /api/v1/clusters/{cluster}/nodes/{node}/uncordon  → uncordon a node
  POST /api/v1/clusters/{cluster}/nodes/{node}/drain     → drain a node

All endpoints require the ``cluster_api`` scope.

Drain dry-run:
  When ``body.dry_run=True`` the handler short-circuits BEFORE calling
  the service, returning a synthetic success response.  This guarantees
  that no Kubernetes API calls are made in dry-run mode.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.domain.kubernetes_models import DrainRequest, NodeActionData
from app.domain.models import ApiResponse, User
from app.repositories.cluster_repository import ClusterRepository
from app.services.kube_client import KubeClientFactory
from app.services.node_service import NodeService

_logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/clusters",
    tags=["nodes"],
)


# ── Dependency providers ──────────────────────────────────────────────────────

def _get_cluster_repo() -> ClusterRepository:
    settings = get_settings()
    return ClusterRepository(settings.KUBECONFIG_BASE_PATH)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


# ── POST …/cordon ─────────────────────────────────────────────────────────────

@router.post(
    "/{cluster}/nodes/{node}/cordon",
    response_model=ApiResponse[NodeActionData],
    summary="Cordon a node",
    description=(
        "Marks the node as unschedulable so no new pods are placed on it. "
        "Existing pods remain running."
    ),
)
async def cordon_node(
    request: Request,
    cluster: str,
    node: str,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    repo: ClusterRepository = Depends(_get_cluster_repo),
) -> ApiResponse[NodeActionData]:
    kubeconfig_path = repo.get_kubeconfig(cluster)
    kube = KubeClientFactory().get_core_v1(kubeconfig_path)
    data = NodeService().cordon(cluster=cluster, node_name=node, kube=kube)
    return ApiResponse(data=data, request_id=_request_id(request))


# ── POST …/uncordon ───────────────────────────────────────────────────────────

@router.post(
    "/{cluster}/nodes/{node}/uncordon",
    response_model=ApiResponse[NodeActionData],
    summary="Uncordon a node",
    description="Re-enables scheduling on a previously cordoned node.",
)
async def uncordon_node(
    request: Request,
    cluster: str,
    node: str,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    repo: ClusterRepository = Depends(_get_cluster_repo),
) -> ApiResponse[NodeActionData]:
    kubeconfig_path = repo.get_kubeconfig(cluster)
    kube = KubeClientFactory().get_core_v1(kubeconfig_path)
    data = NodeService().uncordon(cluster=cluster, node_name=node, kube=kube)
    return ApiResponse(data=data, request_id=_request_id(request))


# ── POST …/drain ──────────────────────────────────────────────────────────────

@router.post(
    "/{cluster}/nodes/{node}/drain",
    response_model=ApiResponse[NodeActionData],
    summary="Drain a node",
    description=(
        "Cordons the node, then evicts (or deletes) all eligible pods. "
        "DaemonSet pods are skipped by default; mirror/static pods are always skipped.\n\n"
        "Set ``dry_run=true`` to validate the request without making any changes."
    ),
)
async def drain_node(
    request: Request,
    cluster: str,
    node: str,
    body: DrainRequest = DrainRequest(),
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))] = None,
    repo: ClusterRepository = Depends(_get_cluster_repo),
) -> ApiResponse[NodeActionData]:
    _logger.info(
        "Drain requested | cluster=%s | node=%s | dry_run=%s | reason=%s",
        cluster,
        node,
        body.dry_run,
        body.reason,
    )

    # Short-circuit: dry-run never touches the cluster.
    if body.dry_run:
        data = NodeActionData(
            cluster=cluster,
            node=node,
            action="drain",
            dry_run=True,
        )
        return ApiResponse(data=data, request_id=_request_id(request))

    kubeconfig_path = repo.get_kubeconfig(cluster)
    kube = KubeClientFactory().get_core_v1(kubeconfig_path)
    data = NodeService().drain(
        cluster=cluster,
        node_name=node,
        kube=kube,
        options=body.options,
    )
    return ApiResponse(data=data, request_id=_request_id(request))
