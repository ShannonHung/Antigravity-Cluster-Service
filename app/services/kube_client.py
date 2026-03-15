"""
app/services/kube_client.py

KubeClientFactory — creates isolated Kubernetes API client objects.

Key design decision:
  Each call creates a *fresh* ApiClient backed by a *new* Configuration
  instance loaded from the given kubeconfig file.  This guarantees that
  concurrent requests targeting different clusters never share global state
  (which ``config.load_kube_config()`` would pollute).
"""

from __future__ import annotations

import logging
from pathlib import Path

from kubernetes import client, config as kube_config
from kubernetes.client import ApiClient, CoreV1Api, Configuration

from app.core.exceptions import KubeApiException

_logger = logging.getLogger(__name__)


class KubeClientFactory:
    """Produces scope-isolated Kubernetes API clients.

    Usage::

        factory = KubeClientFactory()
        core = factory.get_core_v1(kubeconfig_path)
        nodes = core.list_node()
    """

    # ── Public helpers ────────────────────────────────────────────────────────

    def get_core_v1(self, kubeconfig_path: Path) -> CoreV1Api:
        """Return a CoreV1Api client scoped to *kubeconfig_path*.

        Args:
            kubeconfig_path: Absolute path to the cluster's kubeconfig file.

        Raises:
            KubeApiException: If the kubeconfig cannot be loaded.
        """
        return CoreV1Api(api_client=self._make_api_client(kubeconfig_path))

    def get_api_client(self, kubeconfig_path: Path) -> ApiClient:
        """Return a raw ApiClient for use with helpers that need one directly.

        For example the drain utility needs the API client object.
        """
        return self._make_api_client(kubeconfig_path)

    # ── Private ───────────────────────────────────────────────────────────────

    def _make_api_client(self, kubeconfig_path: Path) -> ApiClient:
        """Load kubeconfig into an isolated Configuration and return ApiClient."""
        cfg = Configuration()
        try:
            kube_config.load_kube_config(
                config_file=str(kubeconfig_path),
                client_configuration=cfg,
            )
        except Exception as exc:
            _logger.error(
                "Failed to load kubeconfig | path=%s | error=%s",
                kubeconfig_path,
                exc,
            )
            raise KubeApiException(
                f"Failed to load kubeconfig for '{kubeconfig_path.stem}': {exc}",
            ) from exc

        _logger.debug(
            "Loaded kubeconfig | cluster=%s | host=%s",
            kubeconfig_path.stem,
            cfg.host,
        )
        return ApiClient(configuration=cfg)
