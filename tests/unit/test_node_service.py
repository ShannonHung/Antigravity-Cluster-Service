"""
tests/unit/test_node_service.py

Unit tests for NodeService.

CoreV1Api is fully mocked — no Kubernetes cluster required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app.core.exceptions import DrainTimeoutException, KubeApiException, NodeNotFoundException
from app.domain.kubernetes_models import DrainActionData, DrainOptions, NodeActionData, NodeListData, NodeTaintData, PodListData, TaintRemoveSpec, TaintSpec
from app.services.node_service import NodeService


# ── Helpers ───────────────────────────────────────────────────────────────────

def _svc() -> NodeService:
    return NodeService()


def _make_kube() -> MagicMock:
    return MagicMock()


def _make_node(
    name: str = "worker-1",
    ready: bool = True,
    unschedulable: bool = False,
    kubelet_version: str = "v1.29.0",
    roles: list[str] | None = None,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    taints: list | None = None,
) -> MagicMock:
    node = MagicMock()
    node.metadata.name = name
    node.metadata.labels = labels or {
        f"node-role.kubernetes.io/{r}": "" for r in (roles or ["worker"])
    }
    node.metadata.annotations = annotations or {}
    node.metadata.owner_references = None
    node.spec.unschedulable = unschedulable
    node.spec.taints = taints if taints is not None else []
    cond = MagicMock()
    cond.type = "Ready"
    cond.status = "True" if ready else "False"
    node.status.conditions = [cond]
    node.status.node_info = MagicMock()
    node.status.node_info.kubelet_version = kubelet_version
    return node


def _make_taint(key: str, effect: str, value: str | None = None) -> MagicMock:
    t = MagicMock()
    t.key = key
    t.value = value
    t.effect = effect
    return t


def _make_pod(
    name: str = "mypod",
    namespace: str = "default",
    phase: str = "Running",
    owner_kind: str = "ReplicaSet",
    is_mirror: bool = False,
    node_name: str = "worker-1",
) -> MagicMock:
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.annotations = {"kubernetes.io/config.mirror": ""} if is_mirror else {}
    owner = MagicMock()
    owner.kind = owner_kind
    pod.metadata.owner_references = [owner]
    pod.status.phase = phase
    pod.status.container_statuses = []
    pod.spec.node_name = node_name
    return pod


def _api_error(status: int, reason: str = "error") -> ApiException:
    exc = ApiException(status=status, reason=reason)
    exc.status = status
    exc.reason = reason
    return exc


# ── get_node ──────────────────────────────────────────────────────────────────

def test_get_node_returns_detail_without_pods():
    kube = _make_kube()
    kube.read_node.return_value = _make_node("worker-1", labels={"env": "prod"})
    result = _svc().get_node(cluster="test", node_name="worker-1", kube=kube)

    assert result.cluster == "test"
    assert result.name == "worker-1"
    assert result.labels == {"env": "prod"}
    # Node detail must NOT list pods anymore, and must not query pods.
    assert not hasattr(result, "pods")
    kube.list_pod_for_all_namespaces.assert_not_called()


# ── list_nodes ────────────────────────────────────────────────────────────────

def test_list_nodes_returns_node_list():
    kube = _make_kube()
    kube.list_node.return_value.items = [
        _make_node("node-1"),
        _make_node("node-2", ready=False),
    ]
    result = _svc().list_nodes(cluster="test", kube=kube)

    assert isinstance(result, NodeListData)
    assert result.cluster == "test"
    assert len(result.nodes) == 2
    assert result.nodes[0].status == "Ready"
    assert result.nodes[1].status == "NotReady"


def test_list_nodes_includes_labels():
    kube = _make_kube()
    kube.list_node.return_value.items = [
        _make_node("node-1", labels={"env": "prod", "team": "infra"}),
    ]
    result = _svc().list_nodes(cluster="test", kube=kube)
    assert result.nodes[0].labels == {"env": "prod", "team": "infra"}


def test_list_nodes_unschedulable_flag():
    kube = _make_kube()
    kube.list_node.return_value.items = [_make_node("n", unschedulable=True)]
    result = _svc().list_nodes(cluster="test", kube=kube)
    assert result.nodes[0].unschedulable is True


def test_list_nodes_raises_on_api_error():
    kube = _make_kube()
    kube.list_node.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().list_nodes(cluster="test", kube=kube)


# ── cordon ────────────────────────────────────────────────────────────────────

def test_cordon_patches_unschedulable_true():
    kube = _make_kube()
    result = _svc().cordon(cluster="test", node_name="worker-1", kube=kube)

    assert kube.patch_node.call_count == 1
    body = kube.patch_node.call_args_list[0][0][1]
    assert body == {"spec": {"unschedulable": True}}
    assert isinstance(result, NodeActionData)
    assert result.action == "cordon"


def test_cordon_raises_node_not_found_on_404():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(404)
    with pytest.raises(NodeNotFoundException):
        _svc().cordon(cluster="test", node_name="missing", kube=kube)


def test_cordon_raises_kube_api_exception_on_500():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().cordon(cluster="test", node_name="worker", kube=kube)


# ── uncordon ──────────────────────────────────────────────────────────────────

def test_uncordon_patches_unschedulable_false():
    kube = _make_kube()
    result = _svc().uncordon(cluster="test", node_name="worker-1", kube=kube)

    assert kube.patch_node.call_count == 1
    body = kube.patch_node.call_args_list[0][0][1]
    assert body == {"spec": {"unschedulable": False}}
    assert result.action == "uncordon"


def test_uncordon_raises_node_not_found_on_404():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(404)
    with pytest.raises(NodeNotFoundException):
        _svc().uncordon(cluster="test", node_name="missing", kube=kube)


# ── drain ─────────────────────────────────────────────────────────────────────

def test_drain_returns_drain_action_data_with_pod_list():
    kube = _make_kube()
    pod = _make_pod("app-pod", "default", owner_kind="ReplicaSet")
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[pod]),  # listing
        MagicMock(items=[]),     # wait loop
    ]

    result = _svc().drain("test", "worker-1", kube, DrainOptions())

    assert isinstance(result, DrainActionData)
    assert result.action == "drain"
    assert len(result.drained_pods) == 1
    assert result.drained_pods[0].name == "app-pod"
    assert result.drained_pods[0].namespace == "default"


def test_drain_always_skips_daemonset_pods():
    """DaemonSet pods must be skipped regardless of any option."""
    kube = _make_kube()
    ds_pod = _make_pod("ds-pod", owner_kind="DaemonSet")
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[ds_pod]),
        MagicMock(items=[]),
    ]

    result = _svc().drain("test", "worker-1", kube, DrainOptions())

    kube.create_namespaced_pod_eviction.assert_not_called()
    assert len(result.drained_pods) == 0


def test_drain_skips_mirror_pods():
    kube = _make_kube()
    mirror = _make_pod("static", is_mirror=True)
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[mirror]),
        MagicMock(items=[]),
    ]
    result = _svc().drain("test", "worker-1", kube, DrainOptions())
    kube.create_namespaced_pod_eviction.assert_not_called()
    assert len(result.drained_pods) == 0


def test_drain_skips_completed_pods():
    kube = _make_kube()
    done = _make_pod("job-pod", phase="Succeeded")
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[done]),
        MagicMock(items=[]),
    ]
    result = _svc().drain("test", "worker-1", kube, DrainOptions())
    kube.create_namespaced_pod_eviction.assert_not_called()
    assert len(result.drained_pods) == 0


def test_drain_uses_delete_when_disable_eviction():
    kube = _make_kube()
    pod = _make_pod("app-pod")
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[pod]),
        MagicMock(items=[]),
    ]
    _svc().drain("test", "worker-1", kube, DrainOptions(disable_eviction=True))
    kube.delete_namespaced_pod.assert_called_once()
    kube.create_namespaced_pod_eviction.assert_not_called()


def test_drain_raises_node_not_found_when_cordon_fails():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(404)
    with pytest.raises(NodeNotFoundException):
        _svc().drain("test", "missing-node", kube, DrainOptions())


def test_drain_raises_on_pod_list_failure():
    kube = _make_kube()
    kube.patch_node.return_value = MagicMock()
    kube.list_pod_for_all_namespaces.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().drain("test", "worker-1", kube, DrainOptions())


def _timeout_setup(monkeypatch, default: float) -> MagicMock:
    """Build a kube whose target pod never disappears and freeze the clock so the
    drain deadline (settings-driven) expires on the first wait-loop check."""
    kube = _make_kube()
    stuck = _make_pod("stuck-pod", "default", owner_kind="ReplicaSet")
    kube.list_pod_for_all_namespaces.return_value = MagicMock(items=[stuck])
    monkeypatch.setattr("app.services.node_service.time.sleep", lambda _s: None)
    times = iter([0.0, float(default) + 1.0])  # start, then past-deadline
    monkeypatch.setattr(
        "app.services.node_service.time.monotonic",
        lambda: next(times, 9999.0),
    )
    return kube


def test_drain_raises_drain_timeout_when_pods_never_terminate(monkeypatch):
    """When a targeted pod never disappears, drain must raise a structured
    DrainTimeoutException (504) that names the stuck pod and hints at retry —
    not a bare KubeApiException nor a proxy-level 500. The timeout budget comes
    from settings; the client can no longer set it."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    default = get_settings().DRAIN_DEFAULT_TIMEOUT_SECONDS
    kube = _timeout_setup(monkeypatch, default)

    with pytest.raises(DrainTimeoutException) as exc_info:
        _svc().drain("test", "worker-1", kube, DrainOptions())

    exc = exc_info.value
    assert exc.http_status == 504
    # Timeout budget is the server default, not client-controlled.
    assert exc.detail["timeout_seconds"] == default
    # The stuck pod is reported in the structured detail.
    assert "stuck-pod" in str(exc.detail)
    # The message tells the user drain can be safely retried.
    assert "retry" in exc.message.lower() or "again" in exc.message.lower()


def test_drain_timeout_suggests_stronger_options(monkeypatch):
    """On timeout with a plain drain, the response suggests the stronger flags
    the caller hasn't enabled yet (force / disable_eviction / grace 0 / emptydir)."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    default = get_settings().DRAIN_DEFAULT_TIMEOUT_SECONDS
    kube = _timeout_setup(monkeypatch, default)

    with pytest.raises(DrainTimeoutException) as exc_info:
        _svc().drain("test", "worker-1", kube, DrainOptions())

    suggested = exc_info.value.detail["suggested_options"]
    # A plain drain hasn't enabled any of the escalations, so all are suggested.
    assert suggested["force"] is True
    assert suggested["disable_eviction"] is True
    assert suggested["grace_period_seconds"] == 0
    assert suggested["delete_emptydir_data"] is True


def test_drain_timeout_omits_already_enabled_options(monkeypatch):
    """Flags the caller already set are not re-suggested — only what would add force."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    default = get_settings().DRAIN_DEFAULT_TIMEOUT_SECONDS
    kube = _timeout_setup(monkeypatch, default)

    with pytest.raises(DrainTimeoutException) as exc_info:
        _svc().drain(
            "test", "worker-1", kube,
            DrainOptions(force=True, disable_eviction=True),
        )

    suggested = exc_info.value.detail["suggested_options"]
    # force / disable_eviction already on → not repeated; the rest still offered.
    assert "force" not in suggested
    assert "disable_eviction" not in suggested
    assert suggested["grace_period_seconds"] == 0
    assert suggested["delete_emptydir_data"] is True


# ── label_node ────────────────────────────────────────────────────────────────

def test_label_node_calls_patch_with_labels():
    kube = _make_kube()
    # read_node returns current state after patch
    kube.read_node.return_value = _make_node(
        labels={"env": "prod"}, annotations={"note": "hi"}
    )
    result = _svc().label_node("test", "n", kube, set_labels={"env": "prod"})

    kube.patch_node.assert_called_once_with("n", {"metadata": {"labels": {"env": "prod"}}})
    assert result.action == "label"
    assert result.labels == {"env": "prod"}
    assert not hasattr(result, "annotations")


def test_label_node_removes_labels_with_null():
    kube = _make_kube()
    kube.read_node.return_value = _make_node(labels={}, annotations={})
    _svc().label_node("test", "n", kube, remove_labels=["old-key"])

    body = kube.patch_node.call_args[0][1]
    assert body["metadata"]["labels"]["old-key"] is None


def test_label_node_set_and_remove_together():
    kube = _make_kube()
    kube.read_node.return_value = _make_node(labels={"new": "val"}, annotations={})
    _svc().label_node("test", "n", kube, set_labels={"new": "val"}, remove_labels=["old"])

    body = kube.patch_node.call_args[0][1]
    labels = body["metadata"]["labels"]
    assert labels["new"] == "val"
    assert labels["old"] is None


def test_label_node_no_op_when_nothing_provided():
    kube = _make_kube()
    _svc().label_node("test", "n", kube)
    kube.patch_node.assert_not_called()
    kube.read_node.assert_not_called()


# ── annotate_node ─────────────────────────────────────────────────────────────

def test_annotate_node_calls_patch_with_annotations():
    kube = _make_kube()
    kube.read_node.return_value = _make_node(
        labels={"env": "prod"}, annotations={"note": "hello"}
    )
    result = _svc().annotate_node("test", "n", kube, set_annotations={"note": "hello"})

    kube.patch_node.assert_called_once_with(
        "n", {"metadata": {"annotations": {"note": "hello"}}}
    )
    assert result.action == "annotate"
    assert not hasattr(result, "labels")
    assert result.annotations == {"note": "hello"}


def test_annotate_node_removes_with_null():
    kube = _make_kube()
    kube.read_node.return_value = _make_node(labels={}, annotations={})
    _svc().annotate_node("test", "n", kube, remove_annotations=["old"])

    body = kube.patch_node.call_args[0][1]
    assert body["metadata"]["annotations"]["old"] is None


# ── list_pods ─────────────────────────────────────────────────────────────────

def test_list_pods_no_filters_returns_all_in_namespace():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("web-1", node_name="n1"),
        _make_pod("api-1", node_name="n2"),
    ]
    result = _svc().list_pods(cluster="test", namespace="default", kube=kube)

    assert isinstance(result, PodListData)
    assert result.cluster == "test"
    assert result.namespace == "default"
    assert {p.name for p in result.pods} == {"web-1", "api-1"}
    kube.list_namespaced_pod.assert_called_once_with("default")
    kube.list_pod_for_all_namespaces.assert_not_called()


def test_list_pods_wildcard_lists_all_namespaces():
    kube = _make_kube()
    kube.list_pod_for_all_namespaces.return_value.items = [
        _make_pod("web-1", namespace="default", node_name="n1"),
        _make_pod("kube-dns", namespace="kube-system", node_name="n2"),
    ]
    result = _svc().list_pods(cluster="test", namespace="*", kube=kube)

    assert result.namespace == "*"
    assert {p.name for p in result.pods} == {"web-1", "kube-dns"}
    kube.list_pod_for_all_namespaces.assert_called_once_with()
    kube.list_namespaced_pod.assert_not_called()


def test_list_pods_wildcard_still_applies_filters():
    kube = _make_kube()
    kube.list_pod_for_all_namespaces.return_value.items = [
        _make_pod("web-1", namespace="default", node_name="n1", phase="Running"),
        _make_pod("web-2", namespace="kube-system", node_name="n2", phase="Pending"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="*", kube=kube, statuses=["Running"]
    )
    assert {p.name for p in result.pods} == {"web-1"}


def test_list_pods_filters_by_node():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("web-1", node_name="n1"),
        _make_pod("web-2", node_name="n2"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, nodes=["n1"]
    )
    assert {p.name for p in result.pods} == {"web-1"}


def test_list_pods_filters_by_multiple_nodes_or():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("a", node_name="n1"),
        _make_pod("b", node_name="n2"),
        _make_pod("c", node_name="n3"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, nodes=["n1", "n2"]
    )
    assert {p.name for p in result.pods} == {"a", "b"}


def test_list_pods_filters_by_status_case_insensitive():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("running-pod", phase="Running"),
        _make_pod("pending-pod", phase="Pending"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, statuses=["running"]
    )
    assert {p.name for p in result.pods} == {"running-pod"}


def test_list_pods_filters_by_name_prefix_or():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("web-7d9f"),
        _make_pod("api-xyz"),
        _make_pod("db-1"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, name_prefixes=["web-", "api-"]
    )
    assert {p.name for p in result.pods} == {"web-7d9f", "api-xyz"}


def test_list_pods_filters_are_anded():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("web-1", node_name="n1", phase="Running"),
        _make_pod("web-2", node_name="n2", phase="Running"),
        _make_pod("web-3", node_name="n1", phase="Pending"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube,
        nodes=["n1"], statuses=["Running"], name_prefixes=["web-"],
    )
    assert {p.name for p in result.pods} == {"web-1"}


def test_list_pods_empty_when_no_match():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [_make_pod("web-1", node_name="n1")]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, nodes=["nonexistent"]
    )
    assert result.pods == []


def test_list_pods_raises_on_api_error():
    kube = _make_kube()
    kube.list_namespaced_pod.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().list_pods(cluster="test", namespace="default", kube=kube)


def test_list_pods_wildcard_raises_on_api_error():
    kube = _make_kube()
    kube.list_pod_for_all_namespaces.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().list_pods(cluster="test", namespace="*", kube=kube)


# ── taint_node ────────────────────────────────────────────────────────────────

def test_taint_node_adds_new_taint():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[]),
        _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "true")]),
    ]
    result = _svc().taint_node(
        "test", "n", kube,
        set_taints=[TaintSpec(key="gpu", value="true", effect="NoSchedule")],
        remove_taints=[],
    )
    body = kube.patch_node.call_args[0][1]
    taints = body["spec"]["taints"]
    assert {(t["key"], t["effect"], t.get("value")) for t in taints} == {("gpu", "NoSchedule", "true")}
    assert result.action == "taint"
    assert any(t.key == "gpu" and t.effect == "NoSchedule" for t in result.taints)


def test_taint_node_removes_by_key_and_effect():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "true")]),
        _make_node("n", taints=[]),
    ]
    result = _svc().taint_node(
        "test", "n", kube,
        set_taints=[],
        remove_taints=[TaintRemoveSpec(key="gpu", effect="NoSchedule")],
    )
    body = kube.patch_node.call_args[0][1]
    assert body["spec"]["taints"] == []
    assert result.taints == []


def test_taint_node_same_key_effect_overwrites_value():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "old")]),
        _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "new")]),
    ]
    _svc().taint_node(
        "test", "n", kube,
        set_taints=[TaintSpec(key="gpu", value="new", effect="NoSchedule")],
        remove_taints=[],
    )
    body = kube.patch_node.call_args[0][1]
    taints = body["spec"]["taints"]
    matching = [t for t in taints if t["key"] == "gpu" and t["effect"] == "NoSchedule"]
    assert len(matching) == 1
    assert matching[0]["value"] == "new"


def test_taint_node_set_and_remove_together():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[_make_taint("old", "NoSchedule", None)]),
        _make_node("n", taints=[_make_taint("new", "NoExecute", "1")]),
    ]
    _svc().taint_node(
        "test", "n", kube,
        set_taints=[TaintSpec(key="new", value="1", effect="NoExecute")],
        remove_taints=[TaintRemoveSpec(key="old", effect="NoSchedule")],
    )
    body = kube.patch_node.call_args[0][1]
    keys = {(t["key"], t["effect"]) for t in body["spec"]["taints"]}
    assert keys == {("new", "NoExecute")}


def test_taint_node_remove_nonexistent_is_noop_not_error():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[_make_taint("keep", "NoSchedule", None)]),
        _make_node("n", taints=[_make_taint("keep", "NoSchedule", None)]),
    ]
    result = _svc().taint_node(
        "test", "n", kube,
        set_taints=[],
        remove_taints=[TaintRemoveSpec(key="ghost", effect="NoExecute")],
    )
    body = kube.patch_node.call_args[0][1]
    keys = {(t["key"], t["effect"]) for t in body["spec"]["taints"]}
    assert keys == {("keep", "NoSchedule")}
    assert any(t.key == "keep" for t in result.taints)


def test_taint_node_no_op_when_nothing_provided():
    kube = _make_kube()
    kube.read_node.return_value = _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "true")])
    result = _svc().taint_node("test", "n", kube, set_taints=[], remove_taints=[])
    kube.patch_node.assert_not_called()
    assert any(t.key == "gpu" for t in result.taints)


def test_taint_node_raises_node_not_found_on_404():
    kube = _make_kube()
    kube.read_node.side_effect = _api_error(404)
    with pytest.raises(NodeNotFoundException):
        _svc().taint_node(
            "test", "missing", kube,
            set_taints=[TaintSpec(key="gpu", effect="NoSchedule")],
            remove_taints=[],
        )


def test_taint_node_raises_kube_api_exception_on_read_500():
    kube = _make_kube()
    kube.read_node.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().taint_node(
            "test", "n", kube,
            set_taints=[TaintSpec(key="gpu", effect="NoSchedule")],
            remove_taints=[],
        )


def test_taint_node_raises_kube_api_exception_on_patch_500():
    kube = _make_kube()
    kube.read_node.return_value = _make_node("n", taints=[])
    kube.patch_node.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().taint_node(
            "test", "n", kube,
            set_taints=[TaintSpec(key="gpu", effect="NoSchedule")],
            remove_taints=[],
        )


# ── cordon_many / uncordon_many ───────────────────────────────────────────────

def test_cordon_many_all_succeed():
    kube = _make_kube()
    result = _svc().cordon_many(cluster="test", node_names=["n1", "n2"], kube=kube)

    assert result.cluster == "test"
    assert result.action == "cordon"
    assert result.summary.total == 2
    assert result.summary.succeeded == 2
    assert result.summary.failed == 0
    assert [r.node for r in result.results] == ["n1", "n2"]
    assert all(r.status == "success" for r in result.results)


def test_cordon_many_partial_failure_keeps_going():
    kube = _make_kube()
    kube.patch_node.side_effect = [None, _api_error(404, "Not Found"), None]

    result = _svc().cordon_many(cluster="test", node_names=["n1", "n2", "n3"], kube=kube)

    assert result.summary.total == 3
    assert result.summary.succeeded == 2
    assert result.summary.failed == 1

    failed = [r for r in result.results if r.status == "failed"]
    assert len(failed) == 1
    assert failed[0].node == "n2"
    assert failed[0].error_code == "NODE_NOT_FOUND"
    assert failed[0].kube_status == 404
    assert "n2" in failed[0].message

    # The node after the failure was still attempted.
    assert [r.node for r in result.results] == ["n1", "n2", "n3"]
    assert result.results[2].status == "success"


def test_cordon_many_api_error_carries_underlying_status():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(503, "Service Unavailable")

    result = _svc().cordon_many(cluster="test", node_names=["n1"], kube=kube)

    assert result.summary.failed == 1
    assert result.results[0].error_code == "KUBE_API_ERROR"
    assert result.results[0].kube_status == 503


def test_cordon_many_deduplicates_node_names():
    kube = _make_kube()

    result = _svc().cordon_many(cluster="test", node_names=["n1", "n2", "n1"], kube=kube)

    assert result.summary.total == 2
    assert [r.node for r in result.results] == ["n1", "n2"]
    assert kube.patch_node.call_count == 2


def test_uncordon_many_patches_unschedulable_false():
    kube = _make_kube()

    result = _svc().uncordon_many(cluster="test", node_names=["n1", "n2"], kube=kube)

    assert result.action == "uncordon"
    assert result.summary.succeeded == 2
    kube.patch_node.assert_has_calls([
        call("n1", {"spec": {"unschedulable": False}}),
        call("n2", {"spec": {"unschedulable": False}}),
    ])


def test_cordon_many_patches_unschedulable_true():
    kube = _make_kube()

    _svc().cordon_many(cluster="test", node_names=["n1"], kube=kube)

    kube.patch_node.assert_called_once_with("n1", {"spec": {"unschedulable": True}})


def test_cordon_many_propagates_cluster_connection_failure():
    """A cluster-level failure must not be downgraded to N per-node failures."""
    kube = _make_kube()
    kube.patch_node.side_effect = Urllib3HTTPError("connection refused")

    with pytest.raises(KubeApiException):
        _svc().cordon_many(cluster="test", node_names=["n1", "n2"], kube=kube)


@pytest.mark.parametrize("status", [401, 403])
def test_cordon_many_propagates_auth_failure(status):
    """Bad credentials are a cluster-level failure, not N broken nodes."""
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(status, "Unauthorized")

    with pytest.raises(KubeApiException):
        _svc().cordon_many(cluster="test", node_names=["n1", "n2"], kube=kube)
