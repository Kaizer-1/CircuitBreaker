#!/usr/bin/env python3
"""GitHub itself stops the SRE agent's App doing more than ADR-0016 allows, and keeps release tags
fixed. Run by a person after `make github-app`:

    python3 scripts/github_rules_check.py [--env prod]        (make github-rules-check)

The App logs in with the credentials sealed for --env (private/secrets/<env>.json), then:

- **allowed**, its normal path: make a commit, push a branch, open a pull request;
- **refused by a ruleset**, everything else: merge that pull request, move `main` (API and git
  push), create a `v*` tag, move or delete one; and it cannot edit a ruleset (no permission);
- the **owner** (admin) is refused a `v*` tag delete or move too: only creating one is allowed.

Tag tests use only the canary tag `v0.0.0-tag-rules-canary` (made by the owner once, after the tag
rulesets: making it is the admin-may-create test), never a real release tag. Every change the test
makes is harmless even if GitHub wrongly allowed it: the test commit has main's exact files.
GitHub Actions skips it ("[skip ci]"). The pull request and branch are closed and deleted at the
end, and `main` and every tag must be exactly as before. Results:
build/github/rules-check-<time>.json.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import github_app
from github_app import GitHubError, call
from sealed_secrets import APP_SECRET, PLACEHOLDER, plaintext_file

ROOT = Path(__file__).resolve().parent.parent
CANARY = "v0.0.0-tag-rules-canary"
STAMP = time.strftime("%Y%m%d-%H%M%S")
BRANCH = f"sre-agent/rules-check-{STAMP}"
TEST_TAG = f"v0.0.0-app-check-{STAMP}"
MESSAGE = (
    "test: the SRE agent's App may not merge this (rules check) [skip ci]\n\n"
    "Made by scripts/github_rules_check.py: same files as main. The pull request is closed and the"
    " branch deleted by the same run."
)

ALLOWED, RULE, NO_PERMISSION = "allowed", "refused by a rule", "refused: no permission"


@dataclass
class Result:
    name: str
    who: str
    expected: str
    outcome: str
    detail: str
    passed: bool


results: list[Result] = []


def record(name: str, who: str, expected: str, outcome: str, detail: str) -> Result:
    result = Result(name, who, expected, outcome, detail[:240], outcome == expected)
    results.append(result)
    mark = "PASS" if result.passed else "FAIL"
    print(f"  {mark}  {who:5}  {name}: {outcome} ({result.detail.splitlines()[0][:110]})")
    return result


def classify(error: GitHubError) -> str:
    text = error.message.lower()
    if "rule" in text:  # "Repository rule violations found"
        return RULE
    if error.status in (403, 404) and ("not accessible" in text or error.status == 404):
        return NO_PERMISSION
    return f"refused: other ({error.status})"


def api(
    name: str,
    expected: str,
    method: str,
    path: str,
    token: str,
    body: dict | None = None,
    *,
    control: bool = False,
):
    """One API attempt by the App, recorded. Returns the response body when GitHub allowed it.

    `control`: the same request for a ref no ruleset covers was just allowed. GitHub's API refuses
    a ruleset-blocked tag creation with only "Reference update failed" (git push names the rule,
    GH013), so once that control passed, this answer can only be the ruleset's."""
    try:
        status, response = call(method, path, token, body)
    except GitHubError as e:
        outcome, detail = classify(e), str(e)
        if control and e.status == 422 and "reference update failed" in e.message.lower():
            outcome = RULE
            detail += " (the same request outside v* was allowed: the ruleset refused it)"
        record(name, "App", expected, outcome, detail)
        return None
    record(name, "App", expected, ALLOWED, f"{status}")
    return response


def git(name: str, who: str, expected: str, env: dict[str, str], *args: str) -> None:
    """One git push attempt, recorded. GitHub names the rule that refused it (GH013)."""
    result = subprocess.run(
        ["git", "-C", str(ROOT), "push", *args], capture_output=True, text=True, env=env
    )
    output = (result.stderr + result.stdout).strip()
    if result.returncode == 0:
        outcome = ALLOWED
    elif "GH013" in output or "rule violations" in output.lower():
        outcome = RULE
    else:
        outcome = "refused: other (see detail)"
    lines = [line for line in output.splitlines() if "remote:" in line or "!" in line]
    record(name, who, expected, outcome, " | ".join(lines) or output)


def app_git_env(token: str) -> dict[str, str]:
    """git as the App: only its installation token, never the owner's saved login."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",  # an empty helper clears the list (the keychain's included)
        "GIT_CONFIG_KEY_1": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_1": f"AUTHORIZATION: basic {basic}",
    }


def remote_refs(repo: str) -> dict[str, str]:
    out = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{repo}.git", "refs/heads/main", "refs/tags/*"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {ref: sha for sha, ref in (line.split("\t") for line in out.splitlines())}


# --------------------------------------------------------------------------- the rulesets, as set


def check_rulesets(repo: str, app_id: str) -> int | None:
    """The three rulesets as designed (deployment doc section 4). Returns main's ruleset ID."""
    rulesets = [
        json.loads(github_app.gh("api", f"repos/{repo}/rulesets/{s['id']}"))
        for s in json.loads(github_app.gh("api", f"repos/{repo}/rulesets"))
    ]

    def rules(r: dict) -> set[str]:
        return {rule["type"] for rule in r.get("rules", [])}

    def actors(r: dict) -> list[tuple]:
        return sorted(
            (a.get("actor_type"), a.get("actor_id"), a.get("bypass_mode"))
            for a in r.get("bypass_actors", [])
        )

    def find(pred: Callable[[dict], bool]) -> dict | None:
        return next((r for r in rulesets if r.get("enforcement") == "active" and pred(r)), None)

    main = find(lambda r: r["target"] == "branch" and "update" in rules(r))
    fixed = find(
        lambda r: r["target"] == "tag" and {"deletion", "update", "non_fast_forward"} <= rules(r)
    )
    create = find(lambda r: r["target"] == "tag" and "creation" in rules(r))
    expected_main = [("DeployKey", None, "always"), ("RepositoryRole", 5, "pull_request")]
    checks = [
        (
            "main: restrict updates; bypass deploy keys + admin by pull request only",
            main,
            lambda r: actors(r) == expected_main,
        ),
        (
            "v* tags: no one deletes, moves or force-pushes (no bypass at all)",
            fixed,
            lambda r: r["conditions"]["ref_name"]["include"] == ["refs/tags/v*"] and not actors(r),
        ),
        (
            "v* tags: only the admin role creates them",
            create,
            lambda r: (
                r["conditions"]["ref_name"]["include"] == ["refs/tags/v*"]
                and actors(r) == [("RepositoryRole", 5, "always")]
            ),
        ),
    ]
    for name, ruleset, ok in checks:
        if ruleset is None:
            record(name, "-", "as designed", "missing", "no active ruleset with these rules")
        else:
            detail = f"{ruleset['name']!r} #{ruleset['id']}: rules {sorted(rules(ruleset))}"
            record(
                name,
                "-",
                "as designed",
                "as designed" if ok(ruleset) else "different",
                f"{detail}, bypass {actors(ruleset)}",
            )
    bypass = [
        r["name"]
        for r in rulesets
        if ("Integration", int(app_id), "always") in actors(r)
        or ("Integration", int(app_id), "pull_request") in actors(r)
    ]
    record(
        "the App is on no ruleset's bypass list",
        "-",
        "as designed",
        "as designed" if not bypass else "different",
        f"bypassed by it: {bypass}",
    )
    return main["id"] if main else None


# --------------------------------------------------------------------------- the attempts


def attempts(repo: str, token: str, main_sha: str, main_ruleset: int | None) -> tuple:
    """Every attempt. Returns (pull request number, branches made) for the cleanup."""
    branches: list[str] = []
    _, base = call("GET", f"/repos/{repo}/git/commits/{main_sha}", token)
    made = api(
        "App makes a commit (main's files)",
        ALLOWED,
        "POST",
        f"/repos/{repo}/git/commits",
        token,
        {"message": MESSAGE, "tree": base["tree"]["sha"], "parents": [main_sha]},
    )
    if made is None:
        return None, branches
    test_sha = made["sha"]
    if (
        api(
            f"App pushes its own branch {BRANCH}",
            ALLOWED,
            "POST",
            f"/repos/{repo}/git/refs",
            token,
            {"ref": f"refs/heads/{BRANCH}", "sha": test_sha},
        )
        is not None
    ):
        branches.append(BRANCH)
    pull = api(
        "App opens a pull request",
        ALLOWED,
        "POST",
        f"/repos/{repo}/pulls",
        token,
        {
            "title": "test: the SRE agent's App may not merge this (rules check)",
            "head": BRANCH,
            "base": "main",
            "body": MESSAGE.split("\n\n", 1)[1],
        },
    )
    number = pull["number"] if pull else None
    if number:
        for _ in range(20):  # GitHub works out mergeability in the background
            _, state = call("GET", f"/repos/{repo}/pulls/{number}", token)
            if state.get("mergeable") is not None:
                break
            time.sleep(1)
        api(
            "App merges its pull request",
            RULE,
            "PUT",
            f"/repos/{repo}/pulls/{number}/merge",
            token,
            {"merge_method": "merge"},
        )
    api(
        "App moves main (API)",
        RULE,
        "PATCH",
        f"/repos/{repo}/git/refs/heads/main",
        token,
        {"sha": test_sha, "force": False},
    )
    control = f"rules-check-control-{STAMP}"  # a tag name no ruleset covers
    made_control = api(
        f"App creates tag {control} (control: no ruleset covers it)",
        ALLOWED,
        "POST",
        f"/repos/{repo}/git/refs",
        token,
        {"ref": f"refs/tags/{control}", "sha": test_sha},
    )
    if made_control is not None:
        api(
            f"App deletes tag {control}",
            ALLOWED,
            "DELETE",
            f"/repos/{repo}/git/refs/tags/{control}",
            token,
        )
    api(
        f"App creates tag {TEST_TAG} (API)",
        RULE,
        "POST",
        f"/repos/{repo}/git/refs",
        token,
        {"ref": f"refs/tags/{TEST_TAG}", "sha": test_sha},
        control=made_control is not None,
    )
    api(
        f"App moves {CANARY} (API)",
        RULE,
        "PATCH",
        f"/repos/{repo}/git/refs/tags/{CANARY}",
        token,
        {"sha": test_sha, "force": True},
    )
    api(
        f"App deletes {CANARY} (API)",
        RULE,
        "DELETE",
        f"/repos/{repo}/git/refs/tags/{CANARY}",
        token,
    )
    if main_ruleset is not None:
        api(
            "App edits the main ruleset (no change)",
            NO_PERMISSION,
            "PUT",
            f"/repos/{repo}/rulesets/{main_ruleset}",
            token,
            {"enforcement": "active"},
        )

    # The same through git push, the way people (and most tools) change refs.
    tree = base["tree"]["sha"]
    subprocess.run(["git", "-C", str(ROOT), "fetch", "--quiet", "origin", "main"], check=True)
    local = subprocess.run(
        ["git", "-C", str(ROOT), "commit-tree", tree, "-p", main_sha, "-m", MESSAGE],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    url = f"https://github.com/{repo}.git"
    as_app = app_git_env(token)
    git(
        f"App git-pushes its own branch {BRANCH}-git",
        "App",
        ALLOWED,
        as_app,
        url,
        f"{local}:refs/heads/{BRANCH}-git",
    )
    if results[-1].outcome == ALLOWED:
        branches.append(f"{BRANCH}-git")
    git("App git-pushes to main", "App", RULE, as_app, url, f"{local}:refs/heads/main")
    git(
        f"App git-pushes a new tag {TEST_TAG}-git",
        "App",
        RULE,
        as_app,
        url,
        f"{local}:refs/tags/{TEST_TAG}-git",
    )
    git(
        f"App git-force-pushes {CANARY}",
        "App",
        RULE,
        as_app,
        url,
        "--force",
        f"{local}:refs/tags/{CANARY}",
    )
    git(f"App git-deletes {CANARY}", "App", RULE, as_app, url, f":refs/tags/{CANARY}")
    as_owner = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # the owner's saved login (admin)
    git(
        f"owner git-force-pushes {CANARY}",
        "owner",
        RULE,
        as_owner,
        "origin",
        "--force",
        f"{local}:refs/tags/{CANARY}",
    )
    git(f"owner git-deletes {CANARY}", "owner", RULE, as_owner, "origin", f":refs/tags/{CANARY}")
    return number, branches


def cleanup(repo: str, token: str, number: int | None, branches: list[str]) -> None:
    if number:
        api(
            "App closes its pull request",
            ALLOWED,
            "PATCH",
            f"/repos/{repo}/pulls/{number}",
            token,
            {"state": "closed"},
        )
    for branch in branches:
        api(
            f"App deletes its branch {branch}",
            ALLOWED,
            "DELETE",
            f"/repos/{repo}/git/refs/heads/{branch}",
            token,
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env", choices=["staging", "prod"], default="prod")
    args = parser.parse_args()
    store = json.loads(plaintext_file(args.env).read_text())
    data = store.get(APP_SECRET, {}).get("data", {})
    if not data or PLACEHOLDER in data.values():
        sys.exit("the App's credentials are still placeholders: run make github-app first")
    app_id, installation, pem = data["app-id"], int(data["installation-id"]), data["private-key"]
    repo = github_app.repository()
    print(f"GitHub rules check on {repo}, App {app_id} (credentials sealed for {args.env})")

    main_ruleset = check_rulesets(repo, app_id)
    before = remote_refs(repo)
    if f"refs/tags/{CANARY}" not in before:
        sys.exit(
            f"no tag {CANARY} on GitHub yet. Make it once, after the tag rulesets"
            f" (it is the admin-may-create test):\n"
            f"  git tag -a {CANARY} -m 'Tag rules canary: GitHub must refuse moving or deleting"
            f" it' origin/main && git push origin {CANARY}"
        )
    token = github_app.installation_token(app_id, pem, installation)
    number, branches = None, []
    try:
        number, branches = attempts(repo, token, before["refs/heads/main"], main_ruleset)
    finally:
        cleanup(repo, token, number, branches)
        github_app.revoke(token)
    after = remote_refs(repo)
    changed = sorted(
        ref for ref in before.keys() | after.keys() if before.get(ref) != after.get(ref)
    )
    record(
        "main and every tag are exactly as before",
        "-",
        "as designed",
        "as designed" if not changed else "different",
        f"changed: {changed}",
    )

    out = ROOT / "build" / "github" / f"rules-check-{STAMP}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([asdict(r) for r in results], indent=2) + "\n")
    failed = [r for r in results if not r.passed]
    print(f"{len(results) - len(failed)}/{len(results)} passed; results: {out.relative_to(ROOT)}")
    if failed and any(r.outcome == ALLOWED and r.expected == RULE for r in failed):
        print("   GitHub ALLOWED something it must refuse: see the deployment doc, section 4")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
