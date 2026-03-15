"""
app/domain/kubernetes_models.py

Pydantic models for Kubernetes cluster management operations.

Layers:
  - Request models  : validated HTTP request bodies (DrainOptions, DrainRequest)
  - Response models : HTTP response payloads (NodeActionData, NodeListData, …)

Response convention (mirrors the rest of the codebase):
  Success → ApiResponse[T] → {"data": <T>, "request_id": "..."}
  Node action success body:
    {"status": "success", "cluster": "…", "node": "…", "action": "…"}
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────────────────────
# Request models
# ──────────────────────────────────────────────────────────────────────────────

class DrainOptions(BaseModel):
    """Maps 1-to-1 onto ``kubectl drain`` flags.

    Having a structured model keeps the HTTP body clean and lets the service
    layer pass options through without unpacking.
    """

    ignore_daemonsets: bool = Field(
        default=True,
        description="Pass --ignore-daemonsets; skip DaemonSet-owned pods.",
    )
    delete_emptydir_data: bool = Field(
        default=False,
        description="Pass --delete-emptydir-data; remove pods using emptyDir volumes.",
    )
    force: bool = Field(
        default=False,
        description="Pass --force; delete pods not managed by a controller.",
    )
    disable_eviction: bool = Field(
        default=False,
        description=(
            "Bypass PDB by using Delete instead of Eviction API. "
            "Equivalent to --disable-eviction."
        ),
    )
    grace_period_seconds: Optional[int] = Field(
        default=None,
        description="Override pod termination grace period (--grace-period). "
                    "None means use each pod's own setting.",
    )
    timeout_seconds: int = Field(
        default=300,
        ge=1,
        description="Total time to wait for all pods to be deleted (--timeout).",
    )


class DrainRequest(BaseModel):
    """HTTP request body for POST …/drain."""

    options: DrainOptions = Field(
        default_factory=DrainOptions,
        description="Fine-grained drain behaviour flags.",
    )
    dry_run: bool = Field(
        default=False,
        description="Validate without performing any changes.",
    )
    reason: Optional[str] = Field(
        default=None,
        description="Human-readable reason for the drain (logged, not sent to K8s).",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Response / domain models
# ──────────────────────────────────────────────────────────────────────────────

class NodeActionData(BaseModel):
    """Unified response body for cordon / uncordon / drain actions."""

    status: str = "success"
    cluster: str
    node: str
    action: str  # "cordon" | "uncordon" | "drain"
    dry_run: bool = False


class NodeCondition(BaseModel):
    """Summarised condition entry for a node."""

    type: str
    status: str


class NodeInfo(BaseModel):
    """A single Kubernetes node's key attributes."""

    name: str
    status: str                          # "Ready" | "NotReady" | "Unknown"
    roles: list[str] = Field(default_factory=list)
    version: str = ""                    # kubelet version
    unschedulable: bool = False          # True when cordoned


class NodeListData(BaseModel):
    """Response body for GET …/{cluster}/nodes."""

    cluster: str
    nodes: list[NodeInfo]


class ClusterInfo(BaseModel):
    """Metadata about a registered cluster."""

    name: str
    kubeconfig_path: str


class ClusterListData(BaseModel):
    """Response body for GET /clusters."""

    clusters: list[ClusterInfo]
