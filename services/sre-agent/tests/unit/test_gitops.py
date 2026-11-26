"""The rollback's Git step (ADR-0016) against a fake Argo CD and GitHub.

A bad release came from Git only if an Argo CD sync STARTED just before its pod template first
ran, and that sync changed the service's values file: then a revert pull request, after G11's
checks. Otherwise nothing to do; anything doubtful is FAILED, with why."""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from sre_agent.adapters.gitops import ArgoGitOps, GitHubReverter
from sre_agent.ports.gitops import Sync, SyncState

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
).decode()
PATH = "deploy/envs/staging/order-service.yaml"
BAD, PARENT, OLDER = "b" * 40, "a" * 40, "f" * 40
SYNCED, BEFORE = "c" * 40, "e" * 40  # the sync that applied the bad commit, and the one before
T = datetime(2026, 11, 26, 14, 0, tzinfo=UTC)
HISTORY = (Sync(BEFORE, T - timedelta(hours=3)), Sync(SYNCED, T - timedelta(seconds=4)))
FIRST_RAN = T  # the bad ReplicaSet was made 4 s after that sync started


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


class GitHub:
    def __init__(
        self,
        files: list[str] | None = None,
        now_blob: str = "blob-bad",
        open_prs: list[Any] | None = None,
        in_sync: list[str] | None = None,
    ) -> None:
        self.files = files if files is not None else [PATH]
        self.now_blob = now_blob
        self.open_prs = open_prs or []
        self.in_sync = in_sync if in_sync is not None else [BAD]  # what the sync brought
        self.writes: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        query = dict(request.url.params)
        if path.startswith("/api/v1/applications/"):
            assert request.headers["authorization"] == "Bearer argo-token"
            assert query == {"refresh": "normal"}
            history = [
                {"revision": h.revision, "deployStartedAt": iso(h.started_at)} for h in HISTORY
            ]
            status = {"sync": {"status": "Synced", "revision": SYNCED}, "history": history}
            return httpx.Response(
                200, json={"spec": {"source": {"targetRevision": "main"}}, "status": status}
            )
        if path == "/app/installations/77/access_tokens":
            token = request.headers["authorization"][7:]
            claims = jwt.decode(token, KEY.public_key(), algorithms=["RS256"])
            assert claims["iss"] == "5935158"
            return httpx.Response(201, json={"token": "inst-token"})
        assert request.headers["authorization"] == "Bearer inst-token"
        repo = "/repos/Kaizer-1/CircuitBreaker"
        if method == "GET" and path == f"{repo}/pulls":
            return httpx.Response(200, json=self.open_prs)
        if method == "GET" and path == f"{repo}/commits":
            assert query["path"] == PATH and query["sha"] == SYNCED
            return httpx.Response(200, json=[{"sha": BAD}, {"sha": OLDER}])
        if method == "GET" and path == f"{repo}/compare/{BEFORE}...{SYNCED}":
            return httpx.Response(200, json={"commits": [{"sha": c} for c in self.in_sync]})
        if method == "GET" and path == f"{repo}/commits/{BAD}":
            message = "chore(staging): order-service a1-bad\n\nbody"
            return httpx.Response(
                200,
                json={
                    "files": [{"filename": f} for f in self.files],
                    "parents": [{"sha": PARENT}],
                    "commit": {"message": message},
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
            url = "https://github.com/Kaizer-1/CircuitBreaker/pull/99"
            return httpx.Response(201, json={"html_url": url})
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
    assert before.history == HISTORY
    out = await g.reconcile("order-service", before, "INC-0007", FIRST_RAN)
    assert (out["status"], out["reverted_commit"]) == ("PR_OPENED", BAD)
    assert out["pull_request_url"].endswith("/pull/99")
    assert [m for m, _, _ in server.writes] == ["POST", "PUT", "POST"]  # branch, file, PR
    ref, put, pr = (w[2] for w in server.writes)
    assert ref["ref"] == "refs/heads/sre-agent/revert-staging-order-service-INC-0007"
    assert base64.b64decode(put["content"]).decode() == "tag: good\n"
    assert put["sha"] == "blob-bad"
    assert put["message"].startswith(
        'Revert "chore(staging): order-service a1-bad"\n\nThis reverts commit ' + BAD
    )
    assert (pr["base"], pr["head"]) == ("main", ref["ref"].removeprefix("refs/heads/"))
    assert not any("merge" in p for _, p, _ in server.writes)


async def test_drift_from_outside_git_needs_no_pull_request() -> None:
    server = GitHub()
    drift = SyncState(False, None, "OutOfSync at x", "main", HISTORY)
    out = await gitops(server).reconcile("order-service", drift, "INC-1", T)
    assert out["status"] == "NOT_NEEDED" and "outside Git" in out["reason"]
    assert server.writes == []


async def test_a_change_from_outside_git_that_argo_cd_re_synced_after_is_not_reverted() -> None:
    """Staging, 2026-11-26: chaos-service's bad release ran at 13:55:47; Argo CD's automated sync
    started 5 s LATER (a newer revision was waiting), re-applied Git and reported Synced while the
    injected setting kept running. The first version then reverted an unrelated image bump."""
    server = GitHub()
    later = (Sync(BEFORE, T - timedelta(hours=3)), Sync(SYNCED, T + timedelta(seconds=5)))
    resynced = SyncState(True, SYNCED, "Synced at c", "main", later)
    out = await gitops(server).reconcile("order-service", resynced, "INC-0002", T)
    assert out["status"] == "NOT_NEEDED" and "outside Git" in out["reason"]
    long_ago = SyncState(True, SYNCED, "Synced", "main", (Sync(SYNCED, T - timedelta(hours=1)),))
    out = await gitops(server).reconcile("order-service", long_ago, "INC-0003", T)
    assert out["status"] == "NOT_NEEDED"
    assert server.writes == []


@pytest.mark.parametrize(
    ("server", "reason"),
    [
        (GitHub(files=[PATH, "deploy/envs/prod/order-service.yaml"]), "changed 2 files"),
        (GitHub(now_blob="blob-later"), "changed after commit"),
        (
            GitHub(
                open_prs=[
                    {
                        "head": {"ref": "sre-agent/revert-staging-order-service-INC-3"},
                        "html_url": "u",
                    }
                ]
            ),
            "already open",
        ),
        (GitHub(in_sync=["9" * 40]), "did not change"),  # the sync brought a chart change
        (lambda r: httpx.Response(503), "GitHub answered 503"),
    ],
)
async def test_g11_and_github_failures_keep_the_rollback_and_say_why(
    server: Any, reason: str
) -> None:
    before = SyncState(True, SYNCED, "Synced at c", "main", HISTORY)
    out = await gitops(server).reconcile("order-service", before, "INC-0008", FIRST_RAN)
    assert out["status"] == "FAILED" and reason in out["reason"]
    assert out["reason"].startswith("Manual git revert needed")


async def test_no_answer_before_no_first_run_time_or_no_app_is_failed() -> None:
    g = gitops(GitHub(), github=False)
    assert (await g.reconcile("order-service", None, "INC-1", T))["status"] == "FAILED"
    synced = SyncState(True, SYNCED, "Synced", "main", HISTORY)
    assert (await g.reconcile("order-service", synced, "INC-1", None))["status"] == "FAILED"
    out = await g.reconcile("order-service", synced, "INC-1", FIRST_RAN)
    assert "No GitHub App" in out["reason"]


async def test_a_sync_still_running_counts_as_the_one_before_the_bad_release() -> None:
    """Argo CD records a sync in its history only when it finishes; a release that never gets
    healthy keeps its sync running, so the pre-check must read the operation in progress."""
    started = iso(T - timedelta(seconds=3))

    def argo(request: httpx.Request) -> httpx.Response:
        status = {
            "sync": {"status": "Synced", "revision": SYNCED},
            "history": [{"revision": BEFORE, "deployStartedAt": iso(T - timedelta(hours=3))}],
            "operationState": {
                "phase": "Running",
                "startedAt": started,
                "operation": {"sync": {"revision": SYNCED}},
            },
        }
        return httpx.Response(
            200, json={"spec": {"source": {"targetRevision": "main"}}, "status": status}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(argo))
    before = await ArgoGitOps(client, "http://argo", "argo-token", "staging", None).sync_state(
        "order-service"
    )
    assert before is not None
    assert [h.revision for h in before.history] == [BEFORE, SYNCED]
