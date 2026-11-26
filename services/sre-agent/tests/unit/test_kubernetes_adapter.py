"""The Kubernetes adapter's requests: only its namespace, only Deployments/ReplicaSets/Pods, and
a rollback that is `kubectl rollout undo` (template without its hash label, plus change-cause)."""

import json
from typing import Any

import httpx
import pytest

from sre_agent.adapters.kubernetes import KubernetesPlatform
from sre_agent.ports.platform import PlatformError

DEPLOYMENT = {
    "metadata": {
        "name": "order-service",
        "annotations": {
            "deployment.kubernetes.io/revision": "9",
            "kubernetes.io/change-cause": "release 1.4.1",
        },
    },
    "spec": {
        "replicas": 2,
        "selector": {"matchLabels": {"app.kubernetes.io/name": "order-service"}},
    },
    "status": {"readyReplicas": 2, "updatedReplicas": 2},
}


def rs(revision: int, image: str, cause: str, owner: str = "order-service") -> dict[str, Any]:
    return {
        "metadata": {
            "name": f"order-service-h{revision}",
            "creationTimestamp": "2026-11-26T09:00:00Z",
            "annotations": {
                "deployment.kubernetes.io/revision": str(revision),
                "kubernetes.io/change-cause": cause,
            },
            "ownerReferences": [{"kind": "Deployment", "name": owner}],
        },
        "spec": {
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "order-service",
                        "pod-template-hash": f"h{revision}",
                    }
                },
                "spec": {"containers": [{"name": "order-service", "image": image}]},
            }
        },
    }


class Api:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        assert path.startswith(("/apis/apps/v1/namespaces/staging/", "/api/v1/namespaces/staging/"))
        assert request.headers["authorization"] == "Bearer t0ken"
        if path.endswith("/replicasets"):
            items = [
                rs(8, "os:good", "release 0.5.0"),
                rs(9, "os:bad", "release 1.4.1"),
                rs(3, "other", "x", owner="order-service-old"),
            ]
            return httpx.Response(200, json={"items": items})
        if path.endswith("/pods"):
            pod = {
                "metadata": {"name": "order-service-abc", "labels": {"pod-template-hash": "h8"}},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "ready": True,
                            "restartCount": 2,
                            "lastState": {"terminated": {"reason": "OOMKilled"}},
                        }
                    ],
                },
            }
            return httpx.Response(200, json={"items": [pod]})
        if path.endswith("/scale"):
            return httpx.Response(200, json={"spec": {"replicas": 2}})
        return httpx.Response(200, json=DEPLOYMENT)


def platform(api: Any) -> KubernetesPlatform:
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    return KubernetesPlatform(client, "https://k8s", "staging", "t0ken")


async def test_state_reads_its_own_replicasets_and_pods() -> None:
    state = await platform(Api()).deployment("order-service")
    assert (state.desired, state.ready, state.rolled_out) == (2, 2, True)
    assert sorted(r.revision for r in state.replicasets) == [8, 9]  # another owner's is ignored
    (pod,) = state.pods
    assert (pod.ready, pod.restarts, pod.last_termination) == (True, 2, "OOMKilled")
    assert pod.revision == 8  # its pod-template-hash names revision 8's ReplicaSet


async def test_rollback_is_rollout_undo_with_the_change_cause() -> None:
    api = Api()
    details = await platform(api).rollback("order-service", 8)
    patch = next(r for r in api.requests if r.method == "PATCH")
    assert patch.headers["content-type"] == "application/json-patch+json"
    ops = json.loads(patch.content)
    template = ops[0]["value"]
    assert ops[0]["op"] == "replace" and ops[0]["path"] == "/spec/template"
    assert template["spec"]["containers"][0]["image"] == "os:good"
    assert "pod-template-hash" not in template["metadata"]["labels"]
    assert ops[1] == {
        "op": "add",
        "path": "/metadata/annotations/kubernetes.io~1change-cause",
        "value": "release 0.5.0",
    }
    assert "from revision 9 (release 1.4.1) to revision 8 (release 0.5.0)" in details
    with pytest.raises(PlatformError, match="no revision 4"):
        await platform(api).rollback("order-service", 4)


async def test_scale_and_restart_patch_only_what_they_must() -> None:
    api = Api()
    assert "from 2 to 5" in await platform(api).scale("order-service", 5)
    scale = [r for r in api.requests if r.method == "PATCH"][-1]
    assert scale.url.path.endswith("/deployments/order-service/scale")
    assert json.loads(scale.content) == {"spec": {"replicas": 5}}
    await platform(api).restart("market-data-service")
    restart = [r for r in api.requests if r.method == "PATCH"][-1]
    assert restart.headers["content-type"] == "application/strategic-merge-patch+json"
    body = json.loads(restart.content)
    assert list(body["spec"]["template"]["metadata"]["annotations"]) == [
        "kubectl.kubernetes.io/restartedAt"
    ]
    assert all(r.method in ("GET", "PATCH") for r in api.requests)  # never DELETE or POST


async def test_a_refusal_is_a_platform_error_with_the_apis_reason() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "statefulsets is forbidden"})

    with pytest.raises(PlatformError, match="403 statefulsets is forbidden"):
        await platform(forbidden).scale("order-service", 3)
