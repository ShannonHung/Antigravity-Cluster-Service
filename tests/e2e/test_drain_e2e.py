"""
tests/e2e/test_drain_e2e.py

Drain option semantics against a real Kubernetes cluster.

These tests exist to check the assumptions the unit tests cannot: that the
Eviction API really returns 429 under a PDB, that emptyDir volumes appear where
we look for them, and that a pod ignoring SIGTERM really does outlive the wait
budget. The unit suite proves the code follows its own logic; this one proves
the logic matches Kubernetes.

Run with: make test-e2e
"""

from __future__ import annotations

import pytest

from app.core.exceptions import DrainBlockedException
from app.domain.kubernetes_models import DrainOptions
from app.services.node_service import NodeService

from .conftest import E2E_CLUSTER, E2E_DRAIN_NODE, NAMESPACE

pytestmark = pytest.mark.e2e


def _drain(kube, **opts):
    return NodeService().drain(
        cluster=E2E_CLUSTER,
        node_name=E2E_DRAIN_NODE,
        kube=kube,
        options=DrainOptions(**opts),
    )


def _names(pods) -> list[str]:
    return sorted(p.name for p in pods)


def _in_ns(pods, namespace: str = NAMESPACE) -> list:
    """Keep only the scenario namespace — the node also runs system workloads."""
    return [p for p in pods if p.namespace == namespace]


# ── Blocking: the caller learns everything before anything is destroyed ───────

def test_plain_drain_is_blocked_by_both_guards(kube, scenarios):
    """A plain drain must refuse: the namespace holds an unmanaged pod and an
    emptyDir pod, so both flags are required, reported in one response."""
    with pytest.raises(DrainBlockedException) as exc_info:
        _drain(kube)

    exc = exc_info.value
    assert exc.http_status == 400
    assert exc.detail["required_options"] == {
        "force": True,
        "delete_emptydir_data": True,
    }

    blocked = {p["name"]: p["reasons"] for p in exc.detail["blocked_pods"]}
    assert blocked["bare-pod"] == ["unmanaged"]
    assert blocked["bare-emptydir"] == ["unmanaged", "emptydir"]
    assert any(n.startswith("emptydir-cache") for n in blocked)
    # Skipped categories must never appear as blockers.
    assert not any(n.startswith("drain-daemon") for n in blocked)
    assert "completed-job" not in blocked


def test_blocked_drain_destroys_nothing(kube, scenarios):
    """The refusal is pre-flight: every pod is still there afterwards."""
    before = {p.metadata.name for p in kube.list_namespaced_pod(NAMESPACE).items}

    with pytest.raises(DrainBlockedException):
        _drain(kube)

    after = {p.metadata.name for p in kube.list_namespaced_pod(NAMESPACE).items}
    assert before == after


def test_blocked_drain_still_cordons_the_node(kube, scenarios):
    with pytest.raises(DrainBlockedException):
        _drain(kube)

    node = kube.read_node(E2E_DRAIN_NODE)
    assert node.spec.unschedulable is True


def test_force_alone_still_blocked_by_emptydir(kube, scenarios):
    """Lifting one guard must not lift the other."""
    with pytest.raises(DrainBlockedException) as exc_info:
        _drain(kube, force=True)

    assert exc_info.value.detail["required_options"] == {"delete_emptydir_data": True}


def test_emptydir_alone_still_blocked_by_unmanaged(kube, scenarios):
    with pytest.raises(DrainBlockedException) as exc_info:
        _drain(kube, delete_emptydir_data=True)

    assert exc_info.value.detail["required_options"] == {"force": True}


# ── PDB: only disable_eviction gets past a budget that forbids all disruption ─

def test_pdb_blocks_eviction_without_disable_eviction(kube, scenarios):
    """minAvailable == replicas means the Eviction API always answers 429.

    This is the assumption unit tests cannot verify: we map that 429 to a 409
    telling the caller to use disable_eviction.
    """
    from app.core.exceptions import KubeApiException

    with pytest.raises(KubeApiException) as exc_info:
        _drain(kube, force=True, delete_emptydir_data=True)

    exc = exc_info.value
    assert exc.kube_status == 409
    assert "pdb-guarded" in str(exc)
    assert "disable_eviction" in str(exc)


def test_disable_eviction_bypasses_pdb(kube, scenarios):
    """A raw DELETE ignores the budget entirely, so the drain completes."""
    result = _drain(
        kube, force=True, delete_emptydir_data=True,
        disable_eviction=True, grace_period_seconds=0,
    )

    drained = _names(_in_ns(result.drained_pods))
    assert any(n.startswith("pdb-guarded") for n in drained)
    assert "bare-pod" in drained
    assert "bare-emptydir" in drained
    assert any(n.startswith("emptydir-cache") for n in drained)
    # Never touched, regardless of flags.
    assert not any(n.startswith("drain-daemon") for n in drained)
    assert "completed-job" not in drained


# ── grace period / still_terminating ─────────────────────────────────────────

def test_sigterm_ignoring_pod_is_reported_still_terminating(kube, scenarios):
    """A pod that traps SIGTERM with a 120s grace period outlives the wait
    budget. That is a 200 naming it, not an error — the eviction was accepted.
    """
    result = _drain(
        kube, force=True, delete_emptydir_data=True, disable_eviction=True,
    )

    assert result.node_emptied is False
    still = _names(_in_ns(result.still_terminating))
    assert any(n.startswith("slow-terminator") for n in still)
    # It was targeted, so it is in both lists.
    assert any(n.startswith("slow-terminator") for n in _names(_in_ns(result.drained_pods)))
    assert result.forced_deletion is False


def test_grace_zero_kills_sigterm_ignoring_pod_immediately(kube, scenarios):
    """grace=0 skips the graceful path the pod is abusing, so nothing lingers."""
    result = _drain(
        kube, force=True, delete_emptydir_data=True,
        disable_eviction=True, grace_period_seconds=0,
    )

    assert result.forced_deletion is True
    assert _in_ns(result.still_terminating) == []
    assert result.node_emptied is True


# ── Always-skipped categories survive the most forceful drain possible ───────

def test_daemonset_and_completed_pods_survive_maximum_force(kube, scenarios):
    """Every flag on is still not permission to touch a DaemonSet pod."""
    _drain(
        kube, force=True, delete_emptydir_data=True,
        disable_eviction=True, grace_period_seconds=0,
    )

    remaining = {
        p.metadata.name: p.status.phase
        for p in kube.list_namespaced_pod(NAMESPACE).items
        if p.metadata.deletion_timestamp is None
    }
    assert any(n.startswith("drain-daemon") for n in remaining)
    assert remaining.get("completed-job") == "Succeeded"
