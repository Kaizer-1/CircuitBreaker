"""The order-service Deployment through the Kubernetes API, with chaos-service's ServiceAccount.

Plain HTTPS to the API server with the pod's token (no client library): GET the Deployment, a
strategic merge PATCH for its pod template, and the `scale` subresource. The Role in Terraform's
environment module allows exactly these verbs on exactly `deployments/order-service` and
`deployments/order-service/scale` (resourceNames), in this namespace only (ADR-0020).

The bad release adds FAULT_PROFILE and FAULT_ERROR_RATE to the container's env and sets the
Deployment's change-cause to "release 1.4.1": a new rollout revision, like a developer's deploy.
Removing them restores the previous pod template exactly, so Kubernetes reuses the old ReplicaSet,
and puts back the change-cause Git gave it (the chart writes "release <version> (<tag>)"), so the
Argo CD Application is Synced again. That value is remembered in this process only: after a
chaos-service restart the reset removes the annotation, and Argo CD shows the Deployment
OutOfSync until the next sync (`make sync env=<env> apply=yes`).
"""

import asyncio
import logging
import ssl
from pathlib import Path
from typing import Any

import httpx

from chaos_service.ports.targets import TargetUnavailableError

ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
DEPLOYMENT = "order-service"
CONTAINER = "order-service"
FAULT_ENV = ("FAULT_PROFILE", "FAULT_ERROR_RATE")
CHANGE_CAUSE = "kubernetes.io/change-cause"
REVISION = "deployment.kubernetes.io/revision"
BAD_RELEASE = "release 1.4.1"

logger = logging.getLogger("kubernetes")


class KubernetesOrderService:
    def __init__(self, client: httpx.AsyncClient, api: str, namespace: str, token: str) -> None:
        self._client = client
        self._base = f"{api}/apis/apps/v1/namespaces/{namespace}/deployments/{DEPLOYMENT}"
        self._auth = {"Authorization": f"Bearer {token}"}
        self._release: str | None = None  # the change-cause before the bad release

    @classmethod
    def in_cluster(cls, client_factory: Any = httpx.AsyncClient) -> "KubernetesOrderService":
        """From the pod's mounted ServiceAccount (raises FileNotFoundError outside Kubernetes)."""
        token = (ACCOUNT / "token").read_text().strip()
        namespace = (ACCOUNT / "namespace").read_text().strip()
        tls = ssl.create_default_context(cafile=str(ACCOUNT / "ca.crt"))
        client = client_factory(verify=tls, timeout=10.0)
        return cls(client, "https://kubernetes.default.svc", namespace, token)

    async def _request(
        self, method: str, url: str, body: Any = None, content_type: str | None = None
    ) -> dict[str, Any]:
        headers = dict(self._auth)
        if content_type:
            headers["Content-Type"] = content_type
        try:
            response = await self._client.request(method, url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise TargetUnavailableError(f"Kubernetes API: {type(exc).__name__}") from exc
        if response.status_code >= 300:
            raise TargetUnavailableError(f"Kubernetes API {method} {response.status_code}")
        result: dict[str, Any] = response.json()
        return result

    async def _deployment(self) -> dict[str, Any]:
        return await self._request("GET", self._base)

    @staticmethod
    def _env(deployment: dict[str, Any]) -> list[dict[str, Any]]:
        for container in deployment["spec"]["template"]["spec"]["containers"]:
            if container["name"] == CONTAINER:
                env: list[dict[str, Any]] = container.get("env") or []
                return env
        return []

    async def bad_release_running(self) -> bool:
        return any(e["name"] == "FAULT_PROFILE" for e in self._env(await self._deployment()))

    async def deploy_bad_release(self, error_rate: float) -> int | None:
        before = await self._deployment()
        annotations = before["metadata"].get("annotations", {})
        revision = int(annotations.get(REVISION, "0"))
        self._release = annotations.get(CHANGE_CAUSE)
        patch = {
            "metadata": {"annotations": {CHANGE_CAUSE: BAD_RELEASE}},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": CONTAINER,
                                "env": [
                                    {"name": "FAULT_PROFILE", "value": "bad-release"},
                                    {"name": "FAULT_ERROR_RATE", "value": str(error_rate)},
                                ],
                            }
                        ]
                    }
                }
            },
        }
        await self._request("PATCH", self._base, patch, "application/strategic-merge-patch+json")
        for _ in range(20):  # the controller numbers the new revision within a moment
            after = await self._deployment()
            new = int(after["metadata"].get("annotations", {}).get(REVISION, "0"))
            if new > revision:
                return new
            await asyncio.sleep(0.25)
        return None

    async def remove_bad_release(self) -> bool:
        if not await self.bad_release_running():
            return False
        if self._release is None:
            logger.warning("the change-cause before the bad release is not known: removing it")
        patch = {
            "metadata": {"annotations": {CHANGE_CAUSE: self._release}},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": CONTAINER,
                                "env": [{"name": n, "$patch": "delete"} for n in FAULT_ENV],
                            }
                        ]
                    }
                }
            },
        }
        await self._request("PATCH", self._base, patch, "application/strategic-merge-patch+json")
        self._release = None
        return True

    async def replicas(self) -> int:
        scale = await self._request("GET", f"{self._base}/scale")
        return int(scale["spec"].get("replicas", 0))

    async def scale(self, replicas: int) -> None:
        body = {"spec": {"replicas": replicas}}
        await self._request("PATCH", f"{self._base}/scale", body, "application/merge-patch+json")


class NoKubernetes:
    """Outside Kubernetes (Docker Compose): the bad deploy and the replica reset are unavailable."""

    async def bad_release_running(self) -> bool:
        return False

    async def deploy_bad_release(self, error_rate: float) -> int | None:
        raise TargetUnavailableError("BAD_DEPLOY needs Kubernetes (a real rollout revision)")

    async def remove_bad_release(self) -> bool:
        return False

    async def replicas(self) -> int:
        raise TargetUnavailableError("replicas need Kubernetes")

    async def scale(self, replicas: int) -> None:
        raise TargetUnavailableError("replicas need Kubernetes")
