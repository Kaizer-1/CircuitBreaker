"""The Kubernetes adapter against a fake API server: the exact requests chaos-service may send."""

import json
from typing import Any

import httpx
import pytest

from chaos_service.adapters.kubernetes import KubernetesOrderService
from chaos_service.ports.targets import TargetUnavailableError

BASE = "/apis/apps/v1/namespaces/staging/deployments/order-service"


class FakeApiServer:
    def __init__(self) -> None:
        self.env: list[dict[str, Any]] = [{"name": "LOG_LEVEL", "value": "INFO"}]
        self.revision = 7
        self.replicas = 2
        self.requests: list[tuple[str, str, str | None, Any]] = []

    def deployment(self) -> dict[str, Any]:
        return {
            "metadata": {"annotations": {"deployment.kubernetes.io/revision": str(self.revision)}},
            "spec": {
                "template": {"spec": {"containers": [{"name": "order-service", "env": self.env}]}}
            },
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body: Any = json.loads(request.content) if request.content else None
        kind = request.headers.get("content-type")
        self.requests.append((request.method, request.url.path, kind, body))
        assert request.headers["authorization"] == "Bearer token-x"
        if request.url.path == f"{BASE}/scale":
            if request.method == "PATCH":
                self.replicas = body["spec"]["replicas"]
            return httpx.Response(200, json={"spec": {"replicas": self.replicas}})
        assert request.url.path == BASE  # never any other object
        if request.method == "PATCH":
            for change in body["spec"]["template"]["spec"]["containers"][0]["env"]:
                self.env = [e for e in self.env if e["name"] != change["name"]]
                if change.get("$patch") != "delete":
                    self.env.append(change)
            self.revision += 1
        return httpx.Response(200, json=self.deployment())


@pytest.fixture
def server() -> FakeApiServer:
    return FakeApiServer()


def adapter(server: FakeApiServer) -> KubernetesOrderService:
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return KubernetesOrderService(client, "https://kubernetes.default.svc", "staging", "token-x")


async def test_the_bad_release_is_a_strategic_merge_patch_and_a_new_revision(
    server: FakeApiServer,
) -> None:
    k8s = adapter(server)
    assert not await k8s.bad_release_running()
    assert await k8s.deploy_bad_release(0.4) == 8
    method, path, kind, body = server.requests[2]  # after reading the revision
    assert (method, path, kind) == ("PATCH", BASE, "application/strategic-merge-patch+json")
    assert body["metadata"]["annotations"] == {"kubernetes.io/change-cause": "release 1.4.1"}
    assert [e["name"] for e in server.env] == ["LOG_LEVEL", "FAULT_PROFILE", "FAULT_ERROR_RATE"]
    assert await k8s.bad_release_running()

    assert await k8s.remove_bad_release()
    patch = server.requests[-1][3]
    assert patch["metadata"]["annotations"] == {"kubernetes.io/change-cause": None}
    assert server.env == [{"name": "LOG_LEVEL", "value": "INFO"}]  # the template as it was
    assert not await k8s.remove_bad_release()  # nothing to remove: no PATCH sent
    assert server.requests[-1][0] == "GET"


async def test_replicas_go_through_the_scale_subresource(server: FakeApiServer) -> None:
    k8s = adapter(server)
    assert await k8s.replicas() == 2
    await k8s.scale(5)
    assert server.requests[-1][:3] == ("PATCH", f"{BASE}/scale", "application/merge-patch+json")
    assert await k8s.replicas() == 5


async def test_a_refusal_from_the_api_server_is_target_unavailable() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    k8s = KubernetesOrderService(client, "https://k", "prod", "t")
    with pytest.raises(TargetUnavailableError, match="403"):
        await k8s.deploy_bad_release(0.4)
