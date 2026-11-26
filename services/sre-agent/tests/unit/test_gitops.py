"""The rollback's Git step (ADR-0016) against a fake Argo CD and GitHub: Synced -> a revert pull
request after G11's checks; OutOfSync -> nothing to do; anything doubtful -> FAILED, with why."""

import base64
import json
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from sre_agent.adapters.gitops import ArgoGitOps, GitHubReverter
from sre_agent.ports.gitops import SyncState

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
).decode()
PATH = "deploy/envs/staging/order-service.yaml"
BAD, PARENT = "b" * 40, "a" * 40


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class GitHub:
    def __init__(
        self,
        files: list[str] | None = None,
        now_blob: str = "blob-bad",
        open_prs: list[Any] | None = None,
    ):
        self.files = files if files is not None else [PATH]
        self.now_blob = now_blob
        self.open_prs = open_prs or []
        self.writes: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        query = dict(request.url.params)
        if path.startswith("/api/v1/applications/"):
            assert request.headers["authorization"] == "Bearer argo-token"
            assert query == {"refresh": "normal"}
            return httpx.Response(
                200,
                json={
                    "spec": {"source": {"targetRevision": "main"}},
                    "status": {"sync": {"status": "Synced", "revision": "c" * 40}},
                },
            )
        if path == "/app/installations/77/access_tokens":
            claims = jwt.decode(
                request.headers["authorization"][7:], KEY.public_key(), algorithms=["RS256"]
            )
            assert claims["iss"] == "5935158"
            return httpx.Response(201, json={"token": "inst-token"})
        assert request.headers["authorization"] == "Bearer inst-token"
        repo = "/repos/Kaizer-1/CircuitBreaker"
        if method == "GET" and path == f"{repo}/pulls":
            return httpx.Response(200, json=self.open_prs)
        if method == "GET" and path == f"{repo}/commits":
            assert query["path"] == PATH and query["sha"] == "c" * 40
            return httpx.Response(200, json=[{"sha": BAD}])
        if method == "GET" and path == f"{repo}/commits/{BAD}":
            return httpx.Response(
                200,
                json={
                    "files": [{"filename": f} for f in self.files],
                    "parents": [{"sha": PARENT}],
                    "commit": {"message": "chore(staging): order-service a1-bad\n\nbody"},
                },
            )
        if method == "GET" and path == f"{repo}/contents/{PATH}":
            ref = query["ref"]
            blob = {"main": self.now_blob, BAD: "blob-bad", PARENT: "blob-good"}[ref]
            content = "tag: good\n" if ref == PARENT else "tag: bad\n"
            return httpx.Response(200, json={"sha": blob, "content": b64(content)})
        if method == "GET" and path == f"{repo}/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "d" * 40}})
        body = json.loads(request.content) if request.content else {}
        self.writes.append((method, path, body))
        if method == "POST" and path.endswith("/pulls"):
            return httpx.Response(
                201, json={"html_url": "https://github.com/Kaizer-1/CircuitBreaker/pull/99"}
            )
        return httpx.Response(201, json={})


def gitops(server: Any, *, github: bool = True) -> ArgoGitOps:
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    reverter = (
        GitHubReverter(client, "Kaizer-1/CircuitBreaker", "5935158", "77", PEM, api="https://gh")
        if github
        else None
    )
    return ArgoGitOps(client, "http://argo", "argo-token", "staging", reverter)


async def test_a_bad_release_from_git_gets_a_revert_pull_request_never_a_merge() -> None:
    server = GitHub()
    g = gitops(server)
    before = await g.sync_state("order-service")
    assert before is not None and before.synced and before.branch == "main"
    out = await g.reconcile("order-service", before, "INC-0007")
    assert (out["status"], out["reverted_commit"]) == ("PR_OPENED", BAD)
    assert out["pull_request_url"].endswith("/pull/99")
    methods = [(m, p.rsplit("/", 2)[-2:]) for m, p, _ in server.writes]
    assert [m for m, _ in methods] == ["POST", "PUT", "POST"]  # branch, file, pull request
    ref, put, pr = (w[2] for w in server.writes)
    assert ref["ref"] == "refs/heads/sre-agent/revert-staging-order-service-INC-0007"
    assert base64.b64decode(put["content"]).decode() == "tag: good\n" and put["sha"] == "blob-bad"
    assert put["message"].startswith(
        'Revert "chore(staging): order-service a1-bad"\n\nThis reverts commit ' + BAD
    )
    assert (pr["base"], pr["head"]) == ("main", ref["ref"].removeprefix("refs/heads/"))
    assert not any("merge" in p for _, p, _ in server.writes)


async def test_drift_from_outside_git_needs_no_pull_request() -> None:
    g = gitops(GitHub())
    out = await g.reconcile("order-service", SyncState(False, None, "OutOfSync at x"), "INC-1")
    assert out["status"] == "NOT_NEEDED" and "outside Git" in out["reason"]


@pytest.mark.parametrize(
    ("server", "reason"),
    [
        (GitHub(files=[PATH, "deploy/envs/prod/order-service.yaml"]), "changed 2 files"),
        (GitHub(now_blob="blob-later"), "changed after commit"),
        (
            GitHub(
                open_prs=[
                    {
                        "head": {"ref": "sre-agent/revert-staging-order-service-INC-0003"},
                        "html_url": "u",
                    }
                ]
            ),
            "already open",
        ),
        (lambda r: httpx.Response(503), "GitHub answered 503"),
    ],
)
async def test_g11_and_github_failures_keep_the_rollback_and_say_why(
    server: Any, reason: str
) -> None:
    before = SyncState(True, "c" * 40, "Synced at c", "main")
    out = await gitops(server).reconcile("order-service", before, "INC-0008")
    assert out["status"] == "FAILED" and reason in out["reason"]
    assert out["reason"].startswith("Manual git revert needed")


async def test_no_answer_before_or_no_app_is_failed() -> None:
    g = gitops(GitHub(), github=False)
    assert (await g.reconcile("order-service", None, "INC-1"))["status"] == "FAILED"
    synced = SyncState(True, "c" * 40, "Synced", "main")
    assert "No GitHub App" in (await g.reconcile("order-service", synced, "INC-1"))["reason"]
