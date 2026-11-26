"""The SRE agent's GitHub App, as the scripts see it (ADR-0016): log in as the App, ask GitHub what
it may do, and get a short-lived installation token.

A GitHub App logs in in two steps. It signs a JWT (a token signed with its private key, valid for
at most 10 minutes) to prove it is the App; with that it asks for an **installation token** (one
hour) for the one account it is installed on, and only that token can touch the repository.

Standard library only: the RS256 signature is made by the `openssl` command, so these scripts need
no crypto package. The agent itself (services/sre-agent) does the same with PyJWT.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parent.parent

# What ADR-0016 allows the App, exactly: push its revert branch, open the pull request. Nothing else
# (no workflows, no administration, so it cannot change the rulesets that bind it).
PERMISSIONS = {"contents": "write", "metadata": "read", "pull_requests": "write"}


def repository() -> str:
    """owner/name of this checkout's GitHub repository, from the origin remote's URL."""
    url = subprocess.run(
        ["git", "-C", str(ROOT), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    slug = url.removesuffix(".git").split("github.com")[-1].lstrip(":/")
    if slug.count("/") != 1:
        sys.exit(f"origin is not a GitHub repository URL: {url}")
    return slug


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@contextlib.contextmanager
def key_file(pem: str) -> Iterator[str]:
    """The private key in a file only this user can read, for openssl; removed afterwards."""
    folder = tempfile.mkdtemp(prefix="cb-app-")  # mode 700
    path = os.path.join(folder, "key.pem")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(pem)
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        os.rmdir(folder)


def app_jwt(app_id: str, pem: str) -> str:
    """The App's own token: RS256, issued a minute in the past (clock skew), valid 9 minutes."""
    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iat": now - 60, "exp": now + 540, "iss": app_id}
    payload = b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    with key_file(pem) as path:
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", path],
            input=signing_input,
            capture_output=True,
            check=False,
        )
    if result.returncode != 0:
        sys.exit(f"openssl could not sign with the App's key: {result.stderr.decode().strip()}")
    return f"{header}.{payload}.{b64url(result.stdout)}"


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status} {message}")
        self.status = status
        self.message = message


def call(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    """One GitHub REST call. Returns (status, JSON body); an HTTP error raises GitHubError with
    GitHub's message (what refused it and why)."""
    req = urllib.request.Request(  # noqa: S310 (API is a fixed https URL)
        f"{API}{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed https URL)
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            detail = json.loads(raw)
        except ValueError:
            detail = {"message": raw.decode(errors="replace")}
        message = detail.get("message", "")
        errors = detail.get("errors")
        if errors:
            message = f"{message} {json.dumps(errors)}"
        raise GitHubError(e.code, message) from None


def inspect(app_id: str, pem: str, repo: str) -> dict:
    """Everything GitHub says about the App and its installation, checked by the callers."""
    jwt = app_jwt(app_id, pem)
    try:
        _, app = call("GET", "/app", jwt)
    except GitHubError as e:
        sys.exit(f"GitHub refused the App's login ({e}): wrong App ID, or not this App's key?")
    _, installations = call("GET", "/app/installations", jwt)
    try:
        _, installation = call("GET", f"/repos/{repo}/installation", jwt)
    except GitHubError as e:
        sys.exit(f"the App is not installed on {repo} ({e})")
    token = installation_token(app_id, pem, installation["id"])
    try:
        _, repos = call("GET", "/installation/repositories?per_page=100", token)
    finally:
        revoke(token)
    return {
        "slug": app["slug"],
        "app_permissions": app.get("permissions", {}),
        "events": app.get("events", []),
        "installations": [i["account"]["login"] for i in installations],
        "installation_id": installation["id"],
        "repository_selection": installation.get("repository_selection"),
        "installation_permissions": installation.get("permissions", {}),
        "repositories": sorted(r["full_name"] for r in repos.get("repositories", [])),
    }


def problems(facts: dict, repo: str) -> list[str]:
    """What would let the App do more than ADR-0016 allows (an empty list = as designed)."""
    found = []
    for which in ("app_permissions", "installation_permissions"):
        if facts[which] != PERMISSIONS:
            found.append(
                f"{which.replace('_', ' ')} are {facts[which]}: they must be exactly {PERMISSIONS}"
            )
    if facts["installations"] != [repo.split("/")[0]]:
        found.append(f"installed on {facts['installations']}: it must be on {repo} only")
    if facts["repository_selection"] != "selected" or facts["repositories"] != [repo]:
        found.append(
            f"it can reach {facts['repositories']} ({facts['repository_selection']}):"
            f" it must reach only {repo} ('Only select repositories')"
        )
    return found


def bypassing_rulesets(app_id: str, repo: str) -> list[str]:
    """Rulesets on the repository that list this App as a bypass actor (asked with the owner's gh
    login: reading a ruleset's bypass list needs admin)."""
    ids = json.loads(gh("api", f"repos/{repo}/rulesets"))
    names = []
    for summary in ids:
        ruleset = json.loads(gh("api", f"repos/{repo}/rulesets/{summary['id']}"))
        for actor in ruleset.get("bypass_actors", []):
            if actor.get("actor_type") == "Integration" and str(actor.get("actor_id")) == app_id:
                names.append(ruleset["name"])
    return names


def installation_token(app_id: str, pem: str, installation_id: int) -> str:
    _, body = call(
        "POST", f"/app/installations/{installation_id}/access_tokens", app_jwt(app_id, pem)
    )
    return body["token"]


def revoke(token: str) -> None:
    with contextlib.suppress(GitHubError):
        call("DELETE", "/installation/token", token)


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.exit(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout
