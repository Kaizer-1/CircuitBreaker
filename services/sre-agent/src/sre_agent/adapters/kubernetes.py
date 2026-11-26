"""The agent's Deployments through the Kubernetes API, with its own ServiceAccount (A-17).

Plain HTTPS to the API server with the pod's token, like chaos-service: no client library. Its
Role (Terraform's environment module) allows get/list/patch on Deployments, get/patch on their
scale, get/list on ReplicaSets and Pods, in its own namespace only. Nothing here can delete, exec,
read a Secret or touch a StatefulSet, and the URLs only ever name the agent's own namespace.

The rollback is what `kubectl rollout undo --to-revision` does: the target ReplicaSet's pod
template (without its pod-template-hash label) and its change-cause go back on the Deployment.
Copying the change-cause matters: the chart writes "release <version> (<tag>)" there, and without
it Argo CD would show the Application OutOfSync after a rollback to the Git version (found in 7a).
"""

import contextlib
import copy
import ssl
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from sre_agent.domain.revisions import RESTARTED_AT, TEMPLATE_HASH, ReplicaSetInfo
from sre_agent.ports.platform import DeploymentState, PlatformError, PodInfo

ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
REVISION = "deployment.kubernetes.io/revision"
CHANGE_CAUSE = "kubernetes.io/change-cause"


class KubernetesPlatform:
    def __init__(self, client: httpx.AsyncClient, api: str, namespace: str, token: str) -> None:
        self._client = client
        self._apps = f"{api}/apis/apps/v1/namespaces/{namespace}"
        self._core = f"{api}/api/v1/namespaces/{namespace}"
        self._auth = {"Authorization": f"Bearer {token}"}

    @classmethod
    def in_cluster(cls) -> "KubernetesPlatform":
        token = (ACCOUNT / "token").read_text().strip()
        namespace = (ACCOUNT / "namespace").read_text().strip()
        tls = ssl.create_default_context(cafile=str(ACCOUNT / "ca.crt"))
        client = httpx.AsyncClient(verify=tls, timeout=10.0)
        return cls(client, "https://kubernetes.default.svc", namespace, token)

    async def _request(
        self,
        method: str,
        url: str,
        body: Any = None,
        content_type: str | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = dict(self._auth)
        if content_type:
            headers["Content-Type"] = content_type
        try:
            response = await self._client.request(
                method, url, json=body, headers=headers, params=params
            )
        except httpx.HTTPError as exc:
            raise PlatformError(f"Kubernetes API: {type(exc).__name__}") from exc
        if response.status_code >= 300:
            reason = ""
            with contextlib.suppress(ValueError):
                reason = response.json().get("message", "")
            raise PlatformError(f"Kubernetes API {method} {response.status_code} {reason}".strip())
        result: dict[str, Any] = response.json()
        return result

    async def _deployment(self, service: str) -> dict[str, Any]:
        return await self._request("GET", f"{self._apps}/deployments/{service}")

    async def _replicasets(self, deployment: dict[str, Any]) -> list[ReplicaSetInfo]:
        labels = deployment["spec"]["selector"].get("matchLabels", {})
        selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        listing = await self._request(
            "GET", f"{self._apps}/replicasets", params={"labelSelector": selector}
        )
        name = deployment["metadata"]["name"]
        out = []
        for rs in listing.get("items", []):
            owners = rs["metadata"].get("ownerReferences", [])
            if not any(o.get("kind") == "Deployment" and o.get("name") == name for o in owners):
                continue
            annotations = rs["metadata"].get("annotations", {})
            if REVISION not in annotations:
                continue
            out.append(
                ReplicaSetInfo(
                    revision=int(annotations[REVISION]),
                    created_at=_time(rs["metadata"]["creationTimestamp"]),
                    template=rs["spec"]["template"],
                    change_cause=annotations.get(CHANGE_CAUSE),
                    name=rs["metadata"].get("name", ""),
                )
            )
        return out

    async def _pods(self, deployment: dict[str, Any]) -> list[PodInfo]:
        labels = deployment["spec"]["selector"].get("matchLabels", {})
        selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        listing = await self._request(
            "GET", f"{self._core}/pods", params={"labelSelector": selector}
        )
        pods = []
        for pod in listing.get("items", []):
            statuses = pod.get("status", {}).get("containerStatuses", [])
            last = next(
                (
                    s["lastState"]["terminated"].get("reason")
                    for s in statuses
                    if s.get("lastState", {}).get("terminated")
                ),
                None,
            )
            pods.append(
                PodInfo(
                    name=pod["metadata"]["name"],
                    phase=pod.get("status", {}).get("phase", "Unknown"),
                    ready=bool(statuses) and all(s.get("ready") for s in statuses),
                    restarts=sum(int(s.get("restartCount", 0)) for s in statuses),
                    last_termination=last,
                )
            )
        return pods

    async def deployment(self, service: str) -> DeploymentState:
        d = await self._deployment(service)
        status = d.get("status", {})
        return DeploymentState(
            name=service,
            desired=int(d["spec"].get("replicas", 0)),
            ready=int(status.get("readyReplicas", 0)),
            updated=int(status.get("updatedReplicas", 0)),
            replicasets=await self._replicasets(d),
            pods=await self._pods(d),
        )

    async def rollback(self, service: str, to_revision: int) -> str:
        d = await self._deployment(service)
        sets = await self._replicasets(d)
        target = next((rs for rs in sets if rs.revision == to_revision), None)
        if target is None:
            raise PlatformError(f"{service} has no revision {to_revision} any more")
        current = max(sets, key=lambda rs: rs.revision) if sets else None
        template = copy.deepcopy(target.template)
        template.get("metadata", {}).get("labels", {}).pop(TEMPLATE_HASH, None)
        patch: list[dict[str, Any]] = [
            {"op": "replace", "path": "/spec/template", "value": template}
        ]
        annotation = "/metadata/annotations/" + CHANGE_CAUSE.replace("/", "~1")
        if target.change_cause is not None:
            patch.append({"op": "add", "path": annotation, "value": target.change_cause})
        elif CHANGE_CAUSE in d["metadata"].get("annotations", {}):
            patch.append({"op": "remove", "path": annotation})
        await self._request(
            "PATCH", f"{self._apps}/deployments/{service}", patch, "application/json-patch+json"
        )
        before = f"revision {current.revision} ({current.change_cause})" if current else "?"
        return (
            f"Rolled {service} back from {before} to revision {to_revision}"
            f" ({target.change_cause or 'no release name'}); the rollout has started."
        )

    async def scale(self, service: str, replicas: int) -> str:
        url = f"{self._apps}/deployments/{service}/scale"
        before = await self._request("GET", url)
        body = {"spec": {"replicas": replicas}}
        await self._request("PATCH", url, body, "application/merge-patch+json")
        was = before.get("spec", {}).get("replicas", "?")
        return f"Scaled {service} from {was} to {replicas} replicas."

    async def restart(self, service: str) -> str:
        now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        body = {"spec": {"template": {"metadata": {"annotations": {RESTARTED_AT: now}}}}}
        await self._request(
            "PATCH",
            f"{self._apps}/deployments/{service}",
            body,
            "application/strategic-merge-patch+json",
        )
        return f"Restarted {service}: new pods with the same release are rolling out."


class DryRunPlatform:
    """Docker Compose has no Kubernetes API: the agent says what it would do (design section 9)."""

    async def deployment(self, service: str) -> DeploymentState:
        raise PlatformError("no Kubernetes API here (Docker Compose)")

    async def rollback(self, service: str, to_revision: int) -> str:
        return f"Dry run: would roll {service} back to revision {to_revision}."

    async def scale(self, service: str, replicas: int) -> str:
        return f"Dry run: would scale {service} to {replicas} replicas."

    async def restart(self, service: str) -> str:
        return f"Dry run: would restart {service}."


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
