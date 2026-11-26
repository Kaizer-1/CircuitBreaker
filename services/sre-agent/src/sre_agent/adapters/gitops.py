"""The rollback's Git step (ADR-0016), deterministic code inside rollback_deployment:

1. **Pre-check** (before the rollback): read the service's Argo CD Application with a forced
   refresh, using the read-only account `sre-agent` (it may `get` Applications, nothing else).
   Synced means the bad release came from Git; OutOfSync means live drift (a chaos BAD_DEPLOY).
2. **Reconcile** (after it): only if it was Synced, the GitHub App opens a `git revert` pull
   request for the one values file, after guardrail G11's checks. It never merges: GitHub's
   ruleset refuses it (tested by make github-rules-check), and there is no code here that tries.

G11: only `deploy/envs/<env>/<service>.yaml` of the same service and environment; only a commit
that changed that one file; the file unchanged since (otherwise a revert would undo more than the
bad release); at most one open revert pull request per service; a branch under `sre-agent/`.
Any check failing, or GitHub not answering, is FAILED with the reason: the rollback stays, and a
person does the revert.
"""

import base64
import contextlib
import logging
import time
from datetime import datetime
from typing import Any

import httpx
import jwt

from sre_agent.ports.gitops import Sync, SyncState, sync_behind

logger = logging.getLogger("gitops")
GITHUB = "https://api.github.com"


def result(
    status: str, reason: str, commit: str | None = None, url: str | None = None
) -> dict[str, Any]:
    return {"status": status, "reverted_commit": commit, "pull_request_url": url, "reason": reason}


class GitStepError(Exception):
    """A G11 check failed or GitHub refused: the reason goes on the feed."""


class GitHubReverter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        repository: str,
        app_id: str,
        installation_id: str,
        private_key: str,
        api: str = GITHUB,
    ) -> None:
        self._client = client
        self._repo = repository
        self._app_id = app_id
        self._installation = installation_id
        self._key = private_key
        self._api = api.rstrip("/")
        self._token: tuple[str, float] | None = None  # (token, monotonic expiry)

    def app_jwt(self) -> str:
        now = int(time.time())
        claims = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return jwt.encode(claims, self._key, algorithm="RS256")

    async def _call(
        self, method: str, path: str, token: str, body: dict[str, Any] | None = None
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                f"{self._api}{path}",
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            raise GitStepError(f"GitHub did not answer ({type(exc).__name__})") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 300:
            message = ""
            with contextlib.suppress(ValueError):
                message = response.json().get("message", "")
            raise GitStepError(f"GitHub answered {response.status_code} {message}".strip())
        return response.json() if response.content else {}

    async def installation_token(self) -> str:
        if self._token and self._token[1] > time.monotonic():
            return self._token[0]
        body = await self._call(
            "POST", f"/app/installations/{self._installation}/access_tokens", self.app_jwt()
        )
        if not body or "token" not in body:
            raise GitStepError("GitHub gave the App no installation token")
        self._token = (body["token"], time.monotonic() + 45 * 60)  # they last an hour
        return str(body["token"])

    async def open_revert(
        self,
        service: str,
        environment: str,
        before: SyncState,
        incident_id: str,
        sync: Sync,
        previous: Sync | None,
    ) -> dict[str, Any]:
        path = f"deploy/envs/{environment}/{service}.yaml"
        base = before.branch or "main"
        repo = f"/repos/{self._repo}"
        t = await self.installation_token()

        prefix = f"sre-agent/revert-{environment}-{service}-"
        for pr in await self._call("GET", f"{repo}/pulls?state=open&per_page=100", t) or []:
            if str(pr.get("head", {}).get("ref", "")).startswith(prefix):
                raise GitStepError(
                    f"a revert pull request for {service} is already open ({pr.get('html_url')})"
                )
        # The commit to revert is the newest one that changed the values file IN the sync that put
        # the bad release in place (between the sync before it and that sync).
        touched = await self._call(
            "GET", f"{repo}/commits?sha={sync.revision}&path={path}&per_page=20", t
        )
        if not touched:
            raise GitStepError(f"no commit changed {path}")
        in_sync = [str(c["sha"]) for c in touched]
        if previous is not None:
            compared = await self._call(
                "GET", f"{repo}/compare/{previous.revision}...{sync.revision}", t
            )
            new = {str(c["sha"]) for c in (compared or {}).get("commits", [])}
            in_sync = [sha for sha in in_sync if sha in new]
        if not in_sync:
            raise GitStepError(
                f"the sync at {sync.started_at:%H:%M:%S} did not change {path}: the bad release"
                " came from another file (the shared chart?), which the agent never reverts (G11)"
            )
        sha = in_sync[0]
        commit = await self._call("GET", f"{repo}/commits/{sha}", t)
        files = [f["filename"] for f in commit.get("files", [])]
        if files != [path]:
            raise GitStepError(
                f"commit {sha[:12]} changed {len(files)} files, not only {path}: the agent reverts"
                " only a commit of that one file (G11)"
            )
        if not commit.get("parents"):
            raise GitStepError(f"commit {sha[:12]} has no parent to go back to")
        parent = commit["parents"][0]["sha"]
        now = await self._call("GET", f"{repo}/contents/{path}?ref={base}", t)
        bad = await self._call("GET", f"{repo}/contents/{path}?ref={sha}", t)
        if not now or not bad or now["sha"] != bad["sha"]:
            raise GitStepError(
                f"{path} changed after commit {sha[:12]}: a revert would undo more than the bad"
                " release (G11)"
            )
        old = await self._call("GET", f"{repo}/contents/{path}?ref={parent}", t)
        if not old:
            raise GitStepError(f"{path} did not exist before commit {sha[:12]}")
        head = await self._call("GET", f"{repo}/git/ref/heads/{base}", t)
        branch = f"{prefix}{incident_id}"
        await self._call(
            "POST",
            f"{repo}/git/refs",
            t,
            {"ref": f"refs/heads/{branch}", "sha": head["object"]["sha"]},
        )
        subject = str(commit["commit"]["message"]).splitlines()[0]
        content = base64.b64decode(old["content"]).decode()
        message = (
            f'Revert "{subject}"\n\nThis reverts commit {sha}.\n\n'
            f"Opened by the SRE agent for {incident_id}: it rolled {service} back in {environment}"
            " because this release failed. Merging makes Git match the cluster again."
        )
        await self._call(
            "PUT",
            f"{repo}/contents/{path}",
            t,
            {
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "sha": now["sha"],
                "branch": branch,
            },
        )
        pr = await self._call(
            "POST",
            f"{repo}/pulls",
            t,
            {
                "title": f'Revert "{subject}"',
                "head": branch,
                "base": base,
                "body": (
                    f"The SRE agent rolled **{service}** back in **{environment}** ({incident_id}):"
                    f" the release from {sha[:12]} failed. The cluster already runs the version"
                    " before it. Merging this makes Git say the same, and Argo CD then syncs"
                    " with no new rollout (the pod template is identical).\n\nThe agent cannot"
                    " merge this:"
                    " GitHub's ruleset on the base branch refuses its App (ADR-0016)."
                ),
            },
        )
        return result(
            "PR_OPENED",
            f"The bad release came from Git ({sha[:12]}): revert pull request opened.",
            sha,
            pr["html_url"],
        )


class ArgoGitOps:
    def __init__(
        self,
        client: httpx.AsyncClient,
        argocd_url: str,
        token: str | None,
        environment: str,
        github: GitHubReverter | None,
    ) -> None:
        self._client = client
        self._url = argocd_url.rstrip("/")
        self._token = token
        self._env = environment
        self._github = github

    async def sync_state(self, service: str) -> SyncState | None:
        if not self._token:
            return None
        try:
            response = await self._client.get(
                f"{self._url}/api/v1/applications/{service}-{self._env}",
                params={"refresh": "normal"},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=10.0,
            )
            response.raise_for_status()
            app = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Argo CD pre-check failed", extra={"error": str(exc)})
            return None
        sync = app.get("status", {}).get("sync", {})
        status = sync.get("status", "Unknown")
        revision = sync.get("revision")
        history = []
        for h in app.get("status", {}).get("history", []):
            started = h.get("deployStartedAt") or h.get("deployedAt")
            if h.get("revision") and started:
                at = datetime.fromisoformat(started.replace("Z", "+00:00"))
                history.append(Sync(h["revision"], at))
        return SyncState(
            synced=status == "Synced",
            revision=revision,
            detail=f"{status} at {str(revision)[:12]}",
            branch=app.get("spec", {}).get("source", {}).get("targetRevision"),
            history=tuple(sorted(history, key=lambda x: x.started_at)),
        )

    async def reconcile(
        self,
        service: str,
        before: SyncState | None,
        incident_id: str,
        first_ran: datetime | None,
    ) -> dict[str, Any]:
        if before is None:
            return result(
                "FAILED",
                "Argo CD's pre-check got no answer, so it is not known whether the bad release came"
                f" from Git: a person checks deploy/envs/{self._env}/{service}.yaml.",
            )
        if not before.synced:
            return result(
                "NOT_NEEDED",
                f"The Application was {before.detail.split(' at ')[0]} before the rollback: the bad"
                " revision was a change made outside Git, and the rollback brought the cluster back"
                " to what Git says.",
            )
        if first_ran is None:
            return result(
                "FAILED", "When the bad release first ran is not known: a person checks Git."
            )
        sync, previous = sync_behind(before.history, first_ran)
        if sync is None:
            return result(
                "NOT_NEEDED",
                f"The bad release first ran at {first_ran:%H:%M:%S} UTC and no Argo CD sync had"
                " started just before it: it was a change made outside Git. The rollback brought"
                " the cluster back to what Git says.",
            )
        if self._github is None:
            return result("FAILED", "No GitHub App is configured: a manual git revert is needed.")
        try:
            return await self._github.open_revert(
                service, self._env, before, incident_id, sync, previous
            )
        except GitStepError as exc:
            logger.warning("git revert step failed", extra={"reason": str(exc)})
            return result("FAILED", f"Manual git revert needed: {exc}.")
