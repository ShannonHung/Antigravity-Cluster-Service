"""
tests/unit/test_node_service.py

Unit tests for NodeService.

CoreV1Api is fully mocked — no Kubernetes cluster required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from kubernetes.client.exceptions import ApiException

from app.core.exceptions import KubeApiException, NodeNotFoundException
from app.domain.kubernetes_models import DrainOptions, NodeActionData, NodeListData
from app.services.node_service import NodeService


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_kube() -> MagicMock:
    """Return a MagicMock that stands in for CoreV1Api."""
    return MagicMock()


def _make_node(
    name: str = "worker-1",
    ready: bool = True,
    unschedulable: bool = False,
    kubelet_version: str = "v1.29.0",
    roles: list[str] | None = None,
) -> MagicMock:
    """Build a minimal V1Node mock."""
    node = MagicMock()
    node.metadata.name = name
    node.metadata.labels = {
        f"node-role.kubernetes.io/{r}": "" for r in (roles or ["worker"])
    }
    node.metadata.owner_references = None
    node.spec.unschedulable = unschedulable

    cond = MagicMock()
    cond.type = "Ready"
    cond.status = "True" if ready else "False"
    node.status.conditions = [cond]
    node.status.node_info = MagicMock()
    node.status.node_info.kubelet_version = kubelet_version
    return node


def _make_pod(
    name: str = "mypod",
    namespace: str = "default",
    phase: str = "Running",
    owner_kind: str = "ReplicaSet",
    is_mirror: bool = False,
) -> MagicMock:
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.annotations = {"kubernetes.io/config.mirror": ""} if is_mirror else {}
    owner = MagicMock()
    owner.kind = owner_kind
    pod.metadata.owner_references = [owner]
    pod.status.phase = phase
    return pod


def _api_error(status: int, reason: str = "error") -> ApiException:
    exc = ApiException(status=status, reason=reason)
    exc.status = status
    exc.reason = reason
    return exc


# ── list_nodes ────────────────────────────────────────────────────────────────

def test_list_nodes_returns_node_list():
    kube = _make_kube()
    kube.list_node.return_value.items = [
        _make_node("node-1"),
        _make_node("node-2", ready=False),
    ]
    result = NodeService().list_nodes(cluster="test", kube=kube)

    assert isinstance(result, NodeListData)
    assert result.cluster == "test"
    assert len(result.nodes) == 2
    assert result.nodes[0].name == "node-1"
    assert result.nodes[0].status == "Ready"
    assert result.nodes[1].status == "NotReady"


def test_list_nodes_raises_kube_api_error_on_api_exception():
    kube = _make_kube()
    kube.list_node.side_effect = _api_error(500, "Internal error")

    with pytest.raises(KubeApiException):
        NodeService().list_nodes(cluster="test", kube=kube)


def test_list_nodes_unschedulable_flag():
    kube = _make_kube()
    kube.list_node.return_value.items = [
        _make_node("node-cordoned", unschedulable=True),
    ]
    result = NodeService().list_nodes(cluster="test", kube=kube)
    assert result.nodes[0].unschedulable is True


# ── cordon ────────────────────────────────────────────────────────────────────

def test_cordon_patches_node():
    kube = _make_kube()
    result = NodeService().cordon(cluster="test", node_name="worker-1", kube=kube)

    # First call: mark unschedulable; second call: apply labels.
    assert kube.patch_node.call_count == 2
    first_call_body = kube.patch_node.call_args_list[0][0][1]
    assert first_call_body == {"spec": {"unschedulable": True}}
    assert isinstance(result, NodeActionData)
    assert result.action == "cordon"
    assert result.node == "worker-1"
    assert result.cluster == "test"


def test_cordon_applies_ownership_labels():
    kube = _make_kube()
    NodeService().cordon(cluster="test", node_name="worker-1", kube=kube)

    # Second patch_node call must carry the two label assignments.
    label_call_body = kube.patch_node.call_args_list[1][0][1]
    labels = label_call_body["metadata"]["labels"]
    assert labels["cordon"] == "PM"
    assert labels["cordon_by"] == "cluster_service"


def test_cordon_raises_node_not_found_on_404():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(404)

    with pytest.raises(NodeNotFoundException):
        NodeService().cordon(cluster="test", node_name="missing", kube=kube)


def test_cordon_raises_kube_api_exception_on_other_error():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(500, "Server error")

    with pytest.raises(KubeApiException):
        NodeService().cordon(cluster="test", node_name="worker-1", kube=kube)


# ── uncordon ──────────────────────────────────────────────────────────────────

def test_uncordon_patches_node():
    kube = _make_kube()
    result = NodeService().uncordon(cluster="test", node_name="worker-1", kube=kube)

    kube.patch_node.assert_called_once_with(
        "worker-1", {"spec": {"unschedulable": False}}
    )
    assert result.action == "uncordon"


def test_uncordon_raises_node_not_found_on_404():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(404)

    with pytest.raises(NodeNotFoundException):
        NodeService().uncordon(cluster="test", node_name="missing", kube=kube)


# ── drain ─────────────────────────────────────────────────────────────────────

def test_drain_cordons_then_evicts_pods():
    """Drain must: cordon → list pods → evict each → wait."""
    kube = _make_kube()
    pod = _make_pod("app-pod", "default", owner_kind="ReplicaSet")

    # First call: pods still running; second call: pods gone (wait loop).
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[pod]),   # initial list
        MagicMock(items=[]),      # wait loop — pods gone
    ]

    opts = DrainOptions()
    result = NodeService().drain("test", "worker-1", kube, opts)

    kube.patch_node.assert_called_once_with(
        "worker-1", {"spec": {"unschedulable": True}}
    )
    kube.create_namespaced_pod_eviction.assert_called_once()
    assert result.action == "drain"


def test_drain_skips_mirror_pods():
    kube = _make_kube()
    mirror = _make_pod("static-pod", is_mirror=True)
    kube.list_pod_for_all_namespaces.return_value.items = [mirror]
    # Since no pods to evict, wait loop is also skipped.
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[mirror]),
        MagicMock(items=[]),
    ]

    NodeService().drain("test", "worker-1", kube, DrainOptions())

    kube.create_namespaced_pod_eviction.assert_not_called()
    kube.delete_namespaced_pod.assert_not_called()


def test_drain_skips_daemonset_pods_when_ignore_daemonsets_true():
    kube = _make_kube()
    ds_pod = _make_pod("ds-pod", owner_kind="DaemonSet")
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[ds_pod]),
        MagicMock(items=[]),
    ]

    opts = DrainOptions(ignore_daemonsets=True)
    NodeService().drain("test", "worker-1", kube, opts)

    kube.create_namespaced_pod_eviction.assert_not_called()
    kube.delete_namespaced_pod.assert_not_called()


def test_drain_skips_completed_pods():
    kube = _make_kube()
    done_pod = _make_pod("job-pod", phase="Succeeded")
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[done_pod]),
        MagicMock(items=[]),
    ]

    NodeService().drain("test", "worker-1", kube, DrainOptions())

    kube.create_namespaced_pod_eviction.assert_not_called()


def test_drain_uses_delete_when_disable_eviction_true():
    kube = _make_kube()
    pod = _make_pod("app-pod")
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[pod]),
        MagicMock(items=[]),
    ]

    opts = DrainOptions(disable_eviction=True)
    NodeService().drain("test", "worker-1", kube, opts)

    kube.delete_namespaced_pod.assert_called_once_with(
        name="app-pod",
        namespace="default",
        grace_period_seconds=None,
    )
    kube.create_namespaced_pod_eviction.assert_not_called()


def test_drain_raises_node_not_found_on_404_patch():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(404)

    with pytest.raises(NodeNotFoundException):
        NodeService().drain("test", "missing-node", kube, DrainOptions())


def test_drain_raises_kube_api_exception_on_pod_list_failure():
    kube = _make_kube()
    # patch_node succeeds, list_pod fails
    kube.patch_node.return_value = MagicMock()
    kube.list_pod_for_all_namespaces.side_effect = _api_error(500, "Server error")

    with pytest.raises(KubeApiException):
        NodeService().drain("test", "worker-1", kube, DrainOptions())
