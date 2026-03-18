"""
app/repositories/json_cluster_repository.py

JsonClusterRepository — loads cluster credentials from a JSON file.

File layout (one file per cluster):
    <KUBECONFIG_BASE_PATH>/
        <cluster-name>.json
        …

Expected JSON schema:
    {
        "cluster_name": "prod",
        "server":       "https://k8s.example.com:6443",
        "ca":           "<base64-encoded PEM certificate>",
        "token":        "<bearer token>"
    }

This avoids distributing a full kubeconfig when only a service-account
token and CA cert are available (e.g. CI injected credentials or Vault).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.exceptions import ClusterNotFoundException, ValidationException
from app.domain.kubernetes_models import ClusterInfo, KubeClientConfig
from app.repositories.cluster_repository import ClusterRepository

_logger = logging.getLogger(__name__)

_REQUIRED_KEYS = {"server", "ca", "token"}


class JsonClusterRepository(ClusterRepository):
    """Filesystem-backed repository that resolves cluster names to JSON credentials.

    Args:
        base_path: Directory containing ``<cluster>.json`` credential files.
                   Sourced from ``Settings.KUBECONFIG_BASE_PATH``.
    """

    def __init__(self, base_path: str) -> None:
        self._base = Path(base_path)

    # ── ClusterRepository interface ───────────────────────────────────────────

    def get_kube_client_config(self, cluster: str) -> KubeClientConfig:
        """Resolve *cluster* to a KubeClientConfig backed by token auth.

        Raises:
            ClusterNotFoundException: If no ``<cluster>.json`` exists.
            ValidationException: If the JSON file is missing required keys.
        """
        path = self._base / f"{cluster}.json"
        if not path.exists():
            _logger.info(
                "JSON credential file not found | cluster=%s | path=%s",
                cluster,
                path,
            )
            raise ClusterNotFoundException(
                f"Cluster '{cluster}' not found. "
                f"No credential file at '{path}'.",
            )

        try:
            data: dict = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationException(
                f"Invalid JSON in credential file '{path}': {exc}",
            ) from exc

        missing = _REQUIRED_KEYS - set(data.keys())
        if missing:
            raise ValidationException(
                f"Credential file '{path}' is missing required keys: {sorted(missing)}. "
                f"Expected: server, ca, token.",
            )

        _logger.debug("Resolved JSON credential | cluster=%s | server=%s", cluster, data["server"])
        return KubeClientConfig(
            cluster_name=cluster,
            source="json",
            server=data["server"],
            ca_data=data["ca"],
            token=data["token"],
        )

    def list_clusters(self) -> list[ClusterInfo]:
        """Scan the base directory for ``*.json`` files and return cluster metadata."""
        if not self._base.exists():
            _logger.warning(
                "Credential base directory does not exist | path=%s", self._base
            )
            return []

        clusters = [
            ClusterInfo(name=p.stem, source="json")
            for p in sorted(self._base.glob("*.json"))
        ]
        _logger.debug("JSON repo listed %d cluster(s) from %s", len(clusters), self._base)
        return clusters
