"""
app/services/node_service.py

NodeService — implements Kubernetes node operations.

Operations:
  - list_nodes  : list all nodes and their status
  - cordon      : mark a node as unschedulable
  - uncordon    : re-enable scheduling on a node
  - drain       : cordon + evict/delete all eligible pods

Drain design notes
──────────────────
The Python kubernetes client has no single ``drain()`` helper equivalent to
``kubectl drain``.  We implement it by hand:

  1. Cordon the node (patch spec.unschedulable = True).
  2. List all pods on the node.
  3. Filter out:
       - DaemonSet pods (unless ignore_daemonsets=False — rare)
       - Mirror / static pods (annotation: mirror.k8s.io/pod)
       - Completed / failed pods (phase in {Succeeded, Failed})
  4. For each remaining pod:
       - if disable_eviction=True  → DELETE the pod (bypasses PDB)
       - else                       → create an Eviction object (honours PDB)
  5. Wait until all targeted pods are gone (poll until timeout).

``dry_run`` is handled at the API layer: when dry_run=True the service
is never called; the route short-circuits and returns a success response
with dry_run=True in the body.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from kubernetes.client import CoreV1Api, V1Node
from kubernetes.client.exceptions import ApiException

from app.core.exceptions import KubeApiException, NodeNotFoundException
from app.domain.kubernetes_models import (
    DrainOptions,
    NodeActionData,
    NodeInfo,
    NodeListData,
)

_logger = logging.getLogger(__name__)

# Annotations that identify mirror / static pods — these are not evictable.
_MIRROR_POD_ANNOTATION = "kubernetes.io/config.mirror"


class NodeService:
    """Implements cordon / uncordon / drain / list operations.

    Accepts a ``CoreV1Api`` instance injected by the route handler, which
    makes the service trivially mockable in unit tests.
    """

    # ── Node listing ──────────────────────────────────────────────────────────

    def list_nodes(self, cluster: str, kube: CoreV1Api) -> NodeListData:
        """Fetch all nodes in the cluster and summarise their status.

        Raises:
            KubeApiException: On Kubernetes API failure.
        """
        try:
            node_list = kube.list_node()
        except ApiException as exc:
            raise KubeApiException(
                f"Failed to list nodes in cluster '{cluster}': {exc.reason}",
                kube_status=exc.status,
            ) from exc

        nodes = [self._node_to_info(n) for n in node_list.items]
        _logger.info("Listed %d node(s) | cluster=%s", len(nodes), cluster)
        return NodeListData(cluster=cluster, nodes=nodes)

    # ── Cordon ────────────────────────────────────────────────────────────────

    def cordon(self, cluster: str, node_name: str, kube: CoreV1Api) -> NodeActionData:
        """Mark *node_name* as unschedulable (cordon).

        Also stamps two labels on the node to record who performed the action:
          - ``cordon=PM``
          - ``cordon_by=cluster_service``

        Raises:
            NodeNotFoundException: If the node does not exist.
            KubeApiException: On Kubernetes API failure.
        """
        self._patch_unschedulable(cluster, node_name, kube, unschedulable=True)
        self._label_node_cordoned(cluster, node_name, kube)
        _logger.info("Cordoned node | cluster=%s | node=%s", cluster, node_name)
        return NodeActionData(cluster=cluster, node=node_name, action="cordon")

    # ── Uncordon ──────────────────────────────────────────────────────────────

    def uncordon(
        self, cluster: str, node_name: str, kube: CoreV1Api
    ) -> NodeActionData:
        """Re-enable scheduling on *node_name* (uncordon).

        Raises:
            NodeNotFoundException: If the node does not exist.
            KubeApiException: On Kubernetes API failure.
        """
        self._patch_unschedulable(cluster, node_name, kube, unschedulable=False)
        _logger.info("Uncordoned node | cluster=%s | node=%s", cluster, node_name)
        return NodeActionData(cluster=cluster, node=node_name, action="uncordon")

    # ── Drain ─────────────────────────────────────────────────────────────────

    def drain(
        self,
        cluster: str,
        node_name: str,
        kube: CoreV1Api,
        options: DrainOptions,
    ) -> NodeActionData:
        """Drain *node_name*: cordon, then evict/delete eligible pods.

        Steps:
          1. Cordon the node.
          2. Collect pods running on the node.
          3. Filter pods that should not be removed (mirror, daemonset, completed).
          4. Evict or delete each eligible pod.
          5. Poll until all pods are gone or *timeout_seconds* elapses.

        Raises:
            NodeNotFoundException: Node does not exist.
            KubeApiException: On Kubernetes API failure.
        """
        # Step 1 — cordon first so no new pods land during drain.
        self._patch_unschedulable(cluster, node_name, kube, unschedulable=True)
        _logger.info(
            "Draining node | cluster=%s | node=%s | options=%s",
            cluster,
            node_name,
            options.model_dump(),
        )

        # Step 2 — collect pods assigned to this node.
        try:
            pod_list = kube.list_pod_for_all_namespaces(
                field_selector=f"spec.nodeName={node_name}"
            )
        except ApiException as exc:
            raise KubeApiException(
                f"Failed to list pods on node '{node_name}': {exc.reason}",
                kube_status=exc.status,
            ) from exc

        # Step 3 — filter: skip mirror pods, completed pods, and optionally daemonsets.
        pods_to_evict = []
        for pod in pod_list.items:
            annotations = pod.metadata.annotations or {}
            owner_kinds = [
                ref.kind for ref in (pod.metadata.owner_references or [])
            ]

            if _MIRROR_POD_ANNOTATION in annotations:
                _logger.debug("Skipping mirror pod | pod=%s", pod.metadata.name)
                continue

            phase = (pod.status.phase or "").lower()
            if phase in ("succeeded", "failed"):
                _logger.debug(
                    "Skipping completed pod | pod=%s | phase=%s",
                    pod.metadata.name,
                    phase,
                )
                continue

            if "DaemonSet" in owner_kinds and options.ignore_daemonsets:
                _logger.debug(
                    "Skipping DaemonSet pod | pod=%s", pod.metadata.name
                )
                continue

            pods_to_evict.append(pod)

        _logger.info(
            "Pods to evict | cluster=%s | node=%s | count=%d",
            cluster,
            node_name,
            len(pods_to_evict),
        )

        # Step 4 — evict or delete each pod.
        for pod in pods_to_evict:
            ns = pod.metadata.namespace
            name = pod.metadata.name
            self._evict_or_delete(
                kube=kube,
                name=name,
                namespace=ns,
                options=options,
            )

        # Step 5 — wait for pods to terminate.
        self._wait_for_pods_gone(
            kube=kube,
            node_name=node_name,
            pod_names={(p.metadata.namespace, p.metadata.name) for p in pods_to_evict},
            timeout_seconds=options.timeout_seconds,
        )

        _logger.info(
            "Drain complete | cluster=%s | node=%s", cluster, node_name
        )
        return NodeActionData(cluster=cluster, node=node_name, action="drain")

    # ── Private helpers ───────────────────────────────────────────────────────

    def _patch_unschedulable(
        self,
        cluster: str,
        node_name: str,
        kube: CoreV1Api,
        *,
        unschedulable: bool,
    ) -> None:
        """Patch spec.unschedulable on the node."""
        try:
            kube.patch_node(
                node_name,
                {"spec": {"unschedulable": unschedulable}},
            )
        except ApiException as exc:
            if exc.status == 404:
                raise NodeNotFoundException(
                    f"Node '{node_name}' not found in cluster '{cluster}'.",
                ) from exc
            raise KubeApiException(
                f"Failed to patch node '{node_name}': {exc.reason}",
                kube_status=exc.status,
            ) from exc

    def _label_node_cordoned(
        self,
        cluster: str,
        node_name: str,
        kube: CoreV1Api,
    ) -> None:
        """Stamp cordon-ownership labels onto the node.

        Labels applied:
          - ``cordon=PM``              — marks that the cordon was requested
                                         via the cluster service's PM workflow
          - ``cordon_by=cluster_service`` — records the actor that performed it
        """
        try:
            kube.patch_node(
                node_name,
                {
                    "metadata": {
                        "labels": {
                            "cordon": "PM",
                            "cordon_by": "cluster_service",
                        }
                    }
                },
            )
            _logger.debug(
                "Applied cordon labels | cluster=%s | node=%s",
                cluster,
                node_name,
            )
        except ApiException as exc:
            raise KubeApiException(
                f"Failed to label node '{node_name}' after cordon: {exc.reason}",
                kube_status=exc.status,
            ) from exc


    def _evict_or_delete(
        self,
        kube: CoreV1Api,
        name: str,
        namespace: str,
        options: DrainOptions,
    ) -> None:
        """Evict (honour PDB) or delete (bypass PDB) a single pod."""
        grace = options.grace_period_seconds

        if options.disable_eviction:
            _logger.debug("Deleting pod | ns=%s | pod=%s", namespace, name)
            try:
                kube.delete_namespaced_pod(
                    name=name,
                    namespace=namespace,
                    grace_period_seconds=grace,
                )
            except ApiException as exc:
                if exc.status == 404:
                    return  # already gone
                raise KubeApiException(
                    f"Failed to delete pod '{namespace}/{name}': {exc.reason}",
                    kube_status=exc.status,
                ) from exc
        else:
            from kubernetes.client.models import (
                V1DeleteOptions,
                V1Eviction,
                V1ObjectMeta,
            )

            _logger.debug("Evicting pod | ns=%s | pod=%s", namespace, name)
            eviction = V1Eviction(
                metadata=V1ObjectMeta(name=name, namespace=namespace),
                delete_options=V1DeleteOptions(
                    grace_period_seconds=grace,
                ),
            )
            try:
                kube.create_namespaced_pod_eviction(
                    name=name,
                    namespace=namespace,
                    body=eviction,
                )
            except ApiException as exc:
                if exc.status == 404:
                    return  # pod already gone
                if exc.status == 429:
                    # PDB prevents eviction — raise a meaningful error.
                    raise KubeApiException(
                        f"Pod '{namespace}/{name}' cannot be evicted due to "
                        "a PodDisruptionBudget. Use disable_eviction=true to "
                        "bypass (will ignore PDB).",
                        kube_status=409,
                    ) from exc
                raise KubeApiException(
                    f"Failed to evict pod '{namespace}/{name}': {exc.reason}",
                    kube_status=exc.status,
                ) from exc

    def _wait_for_pods_gone(
        self,
        kube: CoreV1Api,
        node_name: str,
        pod_names: set[tuple[str, str]],  # {(namespace, name)}
        timeout_seconds: int,
    ) -> None:
        """Poll until all targeted pods have disappeared or timeout expires."""
        if not pod_names:
            return

        deadline = time.monotonic() + timeout_seconds
        poll_interval = 2  # seconds

        while time.monotonic() < deadline:
            try:
                remaining = kube.list_pod_for_all_namespaces(
                    field_selector=f"spec.nodeName={node_name}"
                )
            except ApiException as exc:
                raise KubeApiException(
                    f"Error while waiting for pods to drain: {exc.reason}",
                    kube_status=exc.status,
                ) from exc

            still_present = {
                (p.metadata.namespace, p.metadata.name)
                for p in remaining.items
                if (p.metadata.namespace, p.metadata.name) in pod_names
            }
            if not still_present:
                _logger.debug("All targeted pods are gone | node=%s", node_name)
                return

            _logger.debug(
                "Waiting for %d pod(s) to terminate | node=%s",
                len(still_present),
                node_name,
            )
            time.sleep(poll_interval)

        # Timeout expired — raise to signal incomplete drain.
        raise KubeApiException(
            f"Drain timed out after {timeout_seconds}s: some pods are still "
            f"running on '{node_name}'.",
            kube_status=504,
        )

    @staticmethod
    def _node_to_info(node: V1Node) -> NodeInfo:
        """Convert a V1Node object to a NodeInfo response model."""
        # Determine ready status from conditions
        status = "Unknown"
        for cond in (node.status.conditions or []):
            if cond.type == "Ready":
                status = "Ready" if cond.status == "True" else "NotReady"
                break

        # Parse roles from labels: node-role.kubernetes.io/<role>
        labels = node.metadata.labels or {}
        roles = [
            key.split("/")[-1]
            for key in labels
            if key.startswith("node-role.kubernetes.io/")
        ] or ["<none>"]

        version = (
            node.status.node_info.kubelet_version
            if node.status.node_info
            else ""
        )

        return NodeInfo(
            name=node.metadata.name,
            status=status,
            roles=roles,
            version=version,
            unschedulable=bool(node.spec.unschedulable),
        )
