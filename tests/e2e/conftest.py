"""
tests/e2e/conftest.py

Fixtures for drain e2e tests against a real Kubernetes cluster.

These tests are marked ``e2e`` and excluded from ``make test``. They need a
reachable cluster whose kubeconfig lives under KUBECONFIG_BASE_PATH, and they
drain a real node — see E2E_DRAIN_NODE below.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.domain.kubernetes_models import KubeClientConfig
from app.services.kube_client import KubeClientFactory

# The cluster and node the suite is allowed to disturb. A drain empties the
# node, so this must never point at a node running anything irreplaceable.
E2E_CLUSTER = os.getenv("E2E_CLUSTER", "k3d-mycluster")
E2E_DRAIN_NODE = os.getenv("E2E_DRAIN_NODE", "k3d-mycluster-agent-1")
NAMESPACE = "test-drain"

_MANIFEST = Path(__file__).parent / "manifests" / "drain-scenarios.yaml"


def _kubectl(*args: str, check: bool = True, stdin: str | None = None) -> str:
    """Run kubectl against the e2e cluster and return stdout."""
    proc = subprocess.run(
        ["kubectl", "--context", E2E_CLUSTER, *args],
        capture_output=True, text=True, input=stdin, check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


@pytest.fixture(scope="session")
def kube():
    """A live CoreV1Api, or skip the whole suite when no cluster is reachable.

    Skipping rather than failing keeps `make test-e2e` honest on a laptop with
    no cluster up: one clear skip line instead of a wall of connection errors.
    """
    settings = get_settings()
    kubeconfig = Path(settings.KUBECONFIG_BASE_PATH) / f"{E2E_CLUSTER}.yaml"
    if not kubeconfig.exists():
        pytest.skip(f"no kubeconfig at {kubeconfig}")

    cfg = KubeClientConfig(
        cluster_name=E2E_CLUSTER, source="yaml", kubeconfig_path=kubeconfig
    )
    api = KubeClientFactory().get_core_v1(cfg)
    try:
        api.list_node()
    except Exception as exc:  # noqa: BLE001 — any failure means "no cluster"
        pytest.skip(f"cluster {E2E_CLUSTER} unreachable: {exc}")
    return api


@pytest.fixture(scope="session", autouse=True)
def _guard_node(kube):
    """Refuse to run if the target node is missing, and always uncordon after.

    The suite cordons the node many times over; leaving it cordoned would
    silently shrink the developer's cluster after the tests pass.
    """
    names = [n.metadata.name for n in kube.list_node().items]
    if E2E_DRAIN_NODE not in names:
        pytest.skip(f"node {E2E_DRAIN_NODE} not in cluster (have: {names})")
    yield
    _kubectl("uncordon", E2E_DRAIN_NODE, check=False)
    _kubectl(
        "delete", "pod", "--all", "-n", NAMESPACE,
        "--grace-period=0", "--force", "--wait=false", check=False,
    )
    _kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found",
             "--wait=false", check=False)


@pytest.fixture
def scenarios(kube):
    """Apply the scenario manifests fresh for each test, then tear them down.

    Per-test rather than per-session: a drain deletes these pods, so a shared
    fixture would leave later tests with an empty namespace and silently
    vacuous assertions.
    """
    _reset_namespace(kube)
    manifest = _MANIFEST.read_text().replace("NODE_NAME", E2E_DRAIN_NODE)
    _kubectl("apply", "-f", "-", stdin=manifest)
    _kubectl("uncordon", E2E_DRAIN_NODE, check=False)

    _wait_for_scenarios(kube)
    yield
    _reset_namespace(kube)
    _kubectl("uncordon", E2E_DRAIN_NODE, check=False)


def _reset_namespace(kube, timeout: int = 90) -> None:
    """Delete the scenario namespace and wait until it is really gone.

    ``slow-terminator`` traps SIGTERM and has a 120s grace period, so an
    ordinary delete leaves the namespace stuck in Terminating far longer than a
    test can wait. Applying into a Terminating namespace appears to succeed and
    then has its resources garbage-collected, which would hang the next test
    waiting for pods that can never appear — so the grace period is forced to
    zero and we block until the namespace is actually absent.
    """
    _kubectl(
        "delete", "pod", "--all", "-n", NAMESPACE,
        "--grace-period=0", "--force", "--wait=false", check=False,
    )
    _kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found",
             "--wait=false", check=False)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        existing = _kubectl(
            "get", "namespace", NAMESPACE,
            "--ignore-not-found", "-o", "name", check=False,
        ).strip()
        if not existing:
            return
        # Pods created between the delete and now keep the namespace alive.
        _kubectl(
            "delete", "pod", "--all", "-n", NAMESPACE,
            "--grace-period=0", "--force", "--wait=false", check=False,
        )
        time.sleep(2)

    raise RuntimeError(f"namespace {NAMESPACE} still terminating after {timeout}s")


def _wait_for_scenarios(kube, timeout: int = 120) -> None:
    """Block until every scenario pod has reached the state its test needs.

    Running pods must actually be Running (a Pending pod is not yet a drain
    target) and completed-job must have finished (a still-Running one would be
    treated as an evictable bare pod and change what the test observes).
    """
    expected_running = {
        "bare-pod", "bare-emptydir",
        "clean-web", "emptydir-cache", "pdb-guarded", "slow-terminator",
    }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pods = kube.list_namespaced_pod(NAMESPACE).items
        by_name = {p.metadata.name: p for p in pods}

        running = {
            name for name, p in by_name.items() if p.status.phase == "Running"
        }
        # Deployment pods carry a generated suffix; match on prefix.
        satisfied = {
            want for want in expected_running
            if any(r.startswith(want) for r in running)
        }
        completed = any(
            p.metadata.name == "completed-job" and p.status.phase == "Succeeded"
            for p in pods
        )
        if satisfied == expected_running and completed:
            return
        time.sleep(2)

    raise RuntimeError(
        f"scenario pods not ready within {timeout}s; "
        f"current: {[(p.metadata.name, p.status.phase) for p in pods]}"
    )
