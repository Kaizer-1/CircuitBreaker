#!/usr/bin/env python3
"""Sealed Secrets (ADR-0019): every Kubernetes Secret the system needs is in Git, encrypted.

    seal --env E [--rotate NAME ...]   seal E's secrets into deploy/envs/E/sealed/ (make seal)
    cert          save the controller's public certificate (infra/sealed-secrets/)
    backup        save the controller's private key(s) to private/ (make sealing-key-backup)
    restore       put the backed-up key in before the controller starts (make platform)
    check --env E every SealedSecret of E decrypted into its Secret
    ghcr-token    ask for the GHCR read token (a person types it) and seal it for both environments
    openrouter-key  ask for the SRE agent's OpenRouter key (a person types it), check it has a
                  credit limit, seal it for both environments
    github-app    check the SRE agent's GitHub App (only this repository, only contents and pull
                  requests, on no ruleset's bypass list) and seal its ID and key for both
                  environments

The plaintext values live in private/secrets/<env>.json (git-ignored, mode 600), the source the
SealedSecrets are made from: a missing value is generated at random, and only a secret whose value
changed is sealed again (each sealing makes different ciphertext, so re-sealing everything would
turn every file over in Git). Sealing needs only the PUBLIC certificate, committed in
infra/sealed-secrets/sealing-cert.pem, so it works without the cluster.

Scope is the default, `strict`: a SealedSecret decrypts only under its own name in its own
namespace, so a staging secret copied into prod (or renamed) cannot be decrypted.

Standard library only; needs kubectl and kubeseal.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import github_app  # scripts/github_app.py

ROOT = Path(__file__).resolve().parent.parent


def main_checkout() -> Path:
    """The repository's main checkout. private/ lives only there: run from a git worktree (a
    branch checked out under build/), the ciphertext lands on that branch, the plaintext stays in
    the one private/ folder."""
    common = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return Path(common).parent if common else ROOT


CONTEXT = "kind-circuitbreaker"
CONTROLLER_NS = "kube-system"
CONTROLLER = "sealed-secrets-controller"
KEY_LABEL = "sealedsecrets.bitnami.com/sealed-secrets-key"
CERT = ROOT / "infra" / "sealed-secrets" / "sealing-cert.pem"
PRIVATE = main_checkout() / "private"
KEY_BACKUP = PRIVATE / "sealed-secrets" / "sealing-key.json"
PLACEHOLDER = "PLACEHOLDER-filled-in-phase-7"
GHCR = "ghcr.io"
GHCR_SECRET = "ghcr-pull"  # noqa: S105 (the Secret's name, not a value)
OPENROUTER_SECRET = "openrouter-api-key"  # noqa: S105 (the Secret's name)
APP_SECRET = "sre-agent-github-app"  # noqa: S105 (the Secret's name)
APP_KEY = PRIVATE / "keys" / "sre-agent-github-app.pem"
OPENROUTER = "https://openrouter.ai/api/v1"
AGENT_MODEL = "anthropic/claude-sonnet-5.5"  # SRE_AGENT_MODEL's default (SRE agent design, 4)
ENVS = ("staging", "prod")


def random_value() -> str:
    return secrets.token_urlsafe(32)


def passcode() -> str:
    """Typed by the presenter on the laptop: four short hex groups, still 96 random bits."""
    return "-".join(secrets.token_hex(3) for _ in range(4))


# Every Secret the charts reference, per environment: name -> key -> how a new value is made. The
# names are the ones charts/circuitbreaker/values.yaml (secretEnv) and the docs use.
CATALOG: dict[str, dict[str, object]] = {
    "jwt-secret": {"value": random_value},
    "bot-secret": {"value": random_value},
    "admin-passcode": {"value": passcode},
    # Cassandra's superuser. The init job logs in with it to create each service's role, and gives a
    # cluster that still has the image's default password this one, once (ALTER ROLE; 2026-11-25).
    "cassandra-admin": {"password": random_value},
    "postgres-admin": {"password": random_value},
    "order-service-db": {"password": random_value},
    "matching-engine-db": {"password": random_value},
    "market-data-service-db": {"password": random_value},
    "sre-agent-db": {"password": random_value},
    "portfolio-service-db": {"password": random_value},
    # The SRE agent's LLM key and the GitHub App that opens revert PRs (ADR-0016). Placeholders
    # until a person runs make openrouter-key and make github-app, which put in real values.
    OPENROUTER_SECRET: {"value": lambda: PLACEHOLDER},
    APP_SECRET: {
        "app-id": lambda: PLACEHOLDER,
        "installation-id": lambda: PLACEHOLDER,
        "private-key": lambda: PLACEHOLDER,
    },
    # The kubelet's login to pull our images from GHCR: a person's classic token with only
    # read:packages and an expiry date. Never generated: `make ghcr-token` asks for it.
    GHCR_SECRET: {"username": None, "token": None, "expires": None},
}


def secret_body(name: str, data: dict[str, str]) -> tuple[str, dict[str, str]]:
    """The Secret's type and data. The registry login is a docker config, the rest are Opaque."""
    if name != GHCR_SECRET:
        return "Opaque", data
    auth = base64.b64encode(f"{data['username']}:{data['token']}".encode()).decode()
    config = {"auths": {GHCR: {"username": data["username"], "auth": auth}}}
    return "kubernetes.io/dockerconfigjson", {".dockerconfigjson": json.dumps(config)}


# --------------------------------------------------------------------------- helpers


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, input=input_text, check=False)  # noqa: S603
    if check and result.returncode != 0:
        sys.exit(f"failed ({result.returncode}): {' '.join(cmd)}\n{result.stderr.strip()}")
    return result.stdout


def kubectl(*args: str) -> list[str]:
    return ["kubectl", "--context", CONTEXT, *args]


def write_private(path: Path, text: str) -> None:
    """A file only this user can read (mode 600 from the start, never briefly world-readable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def shown(path: Path) -> str:
    """A path as a person reads it: relative to the checkout it is in."""
    for base in (ROOT, PRIVATE.parent):
        if path.is_relative_to(base):
            return str(path.relative_to(base))
    return str(path)


def fingerprint(pem: str) -> str:
    return hashlib.sha256(pem.strip().encode()).hexdigest()[:16]


def controller_keys() -> list[dict]:
    out = run(kubectl("get", "secret", "-n", CONTROLLER_NS, "-l", KEY_LABEL, "-o", "json"))
    return json.loads(out)["items"]


# --------------------------------------------------------------------------- seal


def plaintext_file(env: str) -> Path:
    return PRIVATE / "secrets" / f"{env}.json"


def seal_one(env: str, name: str, data: dict[str, str]) -> str:
    kind, body = secret_body(name, data)
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": env,
            "labels": {"app.kubernetes.io/part-of": "circuitbreaker"},
        },
        "type": kind,
        "stringData": body,
    }
    sealed = run(
        ["kubeseal", "--cert", str(CERT), "--format", "yaml", "--scope", "strict"],
        input_text=json.dumps(secret),
    )
    header = (
        f"# {name} in {env}: a SealedSecret (ADR-0019), encrypted to the cluster's sealing key.\n"
        f"# Only the controller can decrypt it, and only as {env}/{name}. make seal env={env}\n"
    )
    return header + sealed.removeprefix("---\n")


def cmd_seal(args: argparse.Namespace) -> None:
    if not CERT.exists():
        sys.exit(f"no {CERT.relative_to(ROOT)}: run `make sealing-cert` once the controller runs")
    source = plaintext_file(args.env)
    store: dict[str, dict] = json.loads(source.read_text()) if source.exists() else {}
    out_dir = ROOT / "deploy" / "envs" / args.env / "sealed"
    out_dir.mkdir(parents=True, exist_ok=True)
    unknown = set(args.rotate) - set(CATALOG)
    if unknown:
        sys.exit(f"unknown secret(s): {', '.join(sorted(unknown))}")
    cert = fingerprint(CERT.read_text())
    changed, waiting = [], []
    for name, keys in CATALOG.items():
        entry = store.setdefault(name, {"data": {}})
        data = entry["data"]
        if any(make is None and key not in data for key, make in keys.items()):
            waiting.append(name)  # a value only a person can give
            continue
        for key, make in keys.items():
            if make is not None and (key not in data or name in args.rotate):
                data[key] = make()  # type: ignore[operator]
        # Seal again only when the value or the certificate changed (or the file is missing).
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        target = out_dir / f"{name}.yaml"
        if entry.get("sealed") == {"sha256": digest, "cert": cert} and target.exists():
            continue
        target.write_text(seal_one(args.env, name, data))
        entry["sealed"] = {"sha256": digest, "cert": cert}
        changed.append(name)
    write_private(source, json.dumps(store, indent=2, sort_keys=True) + "\n")
    print(f"{args.env}: sealed {', '.join(changed) if changed else 'nothing (no value changed)'}")
    if GHCR_SECRET in waiting:
        print(f"   {GHCR_SECRET}: no token yet (a person runs make ghcr-token)")
    print(f"   plaintext: {shown(source)} (private, mode 600); ciphertext: {shown(out_dir)}/")
    if changed:
        print("   commit them; Argo CD syncs them; restart the services that read a rotated secret")


# --------------------------------------------------------------------------- the controller's key


def cmd_cert(_: argparse.Namespace) -> None:
    pem = run(
        [
            "kubeseal",
            "--context",
            CONTEXT,
            "--controller-namespace",
            CONTROLLER_NS,
            "--controller-name",
            CONTROLLER,
            "--fetch-cert",
        ]
    )
    CERT.parent.mkdir(parents=True, exist_ok=True)
    CERT.write_text(pem)
    print(f"public certificate saved: {CERT.relative_to(ROOT)} (fingerprint {fingerprint(pem)})")


def cmd_backup(_: argparse.Namespace) -> None:
    """Save every sealing key (private + public) to private/: without it, no new cluster can
    decrypt the secrets in Git."""
    keys = controller_keys()
    if not keys:
        sys.exit("the controller has no key yet (is it running?)")
    kept = []
    for item in keys:
        meta = item["metadata"]
        kept.append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": item.get("type", "kubernetes.io/tls"),
                "metadata": {
                    "name": meta["name"],
                    "namespace": CONTROLLER_NS,
                    "labels": {KEY_LABEL: meta.get("labels", {}).get(KEY_LABEL, "active")},
                },
                "data": item["data"],
            }
        )
    write_private(
        KEY_BACKUP, json.dumps({"apiVersion": "v1", "kind": "List", "items": kept}, indent=2)
    )
    print(f"{len(kept)} sealing key(s) saved to {shown(KEY_BACKUP)} (mode 600, git-ignored)")
    print(
        "   copy it OFF this laptop too (an encrypted USB stick, a password manager): see the"
        " deployment doc,"
    )
    print("   'Backing up the sealing key'. Losing the laptop must not lose the key.")


def cmd_restore(_: argparse.Namespace) -> None:
    """Called by Terraform before the controller is installed. No backup: nothing to do (the
    controller makes a new key)."""
    if not KEY_BACKUP.exists():
        print(
            "sealing key: no backup in private/ yet: the controller makes a new key"
            " (back it up afterwards: make sealing-key-backup)"
        )
        return
    if controller_keys():
        print("sealing key: the cluster already has one: left as it is")
        return
    run(kubectl("apply", "-f", "-"), input_text=KEY_BACKUP.read_text())
    print(f"sealing key restored from {shown(KEY_BACKUP)}")


# --------------------------------------------------------------------------- the GHCR read token


def token_facts(token: str) -> tuple[str, str, str]:
    """Ask GitHub whose token it is, its scopes and expiry (the token goes only to GitHub)."""
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (fixed https URL)
            login = json.load(resp)["login"]
            return (
                login,
                resp.headers.get("X-OAuth-Scopes", ""),
                resp.headers.get("GitHub-Authentication-Token-Expiration", ""),
            )
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub refused the token ({e.code}): mistyped, expired or revoked?")


def cmd_ghcr_token(_: argparse.Namespace) -> None:
    """A person pastes the token (never echoed, never an argument, so not in shell history).
    Refused unless it is a classic token with exactly read:packages and an expiry date."""
    token = getpass.getpass("GHCR token (classic, only read:packages, with an expiry date): ")
    token = token.strip()
    if not token.startswith("ghp_"):
        sys.exit("not a classic personal access token (they start with ghp_)")
    login, scopes, expires = token_facts(token)
    if [s.strip() for s in scopes.split(",") if s.strip()] != ["read:packages"]:
        sys.exit(f"the token's scopes are {scopes or 'none'}: it must have ONLY read:packages")
    if not expires:
        sys.exit("the token never expires: make one with an expiry date")
    expiry = dt.date.fromisoformat(expires[:10]).isoformat()  # "YYYY-MM-DD hh:mm:ss <zone>"
    store_and_seal(GHCR_SECRET, {"username": login, "token": token, "expires": expiry})
    print(f"GHCR token of {login}: read:packages only, expires {expiry} (renew before then:")
    print("   deployment doc section 5, 'GHCR pull token')")


def store_and_seal(name: str, data: dict[str, str], checked: dict | None = None) -> None:
    """Put a value a person gave into both environments' plaintext stores and seal it. `checked`
    records what was verified and when (kept in the store, never sealed)."""
    for env in ENVS:
        source = plaintext_file(env)
        store = json.loads(source.read_text()) if source.exists() else {}
        store[name] = {"data": data}
        if checked:
            store[name]["checked"] = checked
        write_private(source, json.dumps(store, indent=2, sort_keys=True) + "\n")
        cmd_seal(argparse.Namespace(env=env, rotate=[]))


# ----------------------------------------------------------------- the SRE agent's LLM key


def openrouter(path: str, key: str | None = None) -> dict:
    """One OpenRouter call. Only the key goes with it: nothing about the person or project."""
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"{OPENROUTER}{path}", headers=headers)  # noqa: S310
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (fixed https URL)
        return json.load(resp)


def key_facts(key: str) -> dict:
    """OpenRouter's facts about the key: its label, credit limit and use so far."""
    for path in ("/key", "/auth/key"):  # the current endpoint, then its older name
        try:
            return openrouter(path, key)["data"]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            sys.exit(f"OpenRouter refused the key ({e.code}): mistyped, disabled or deleted?")
    sys.exit("OpenRouter has no key-information endpoint any more: check the key by hand")


def model_facts(model: str) -> dict | None:
    """The agent's default model in OpenRouter's public list (no key needed)."""
    for entry in openrouter("/models")["data"]:
        if entry["id"] == model:
            return entry
    return None


def cmd_openrouter_key(_: argparse.Namespace) -> None:
    """A person pastes the key (never echoed, never an argument). Refused unless OpenRouter says it
    has a credit limit: that limit is the agent's spend control (SRE agent design, section 4)."""
    key = getpass.getpass("OpenRouter API key (one with a credit limit): ").strip()
    if not key.startswith("sk-or-"):
        sys.exit("not an OpenRouter API key (they start with sk-or-)")
    facts = key_facts(key)
    limit = facts.get("limit")
    if limit is None:
        sys.exit("the key has no credit limit: set one on openrouter.ai (Settings, API Keys)")
    remaining = facts.get("limit_remaining")
    print(
        f"OpenRouter key {facts.get('label', '?')!r}: credit limit ${limit},"
        f" used ${facts.get('usage', 0):.2f}"
        + (f", ${remaining:.2f} left" if isinstance(remaining, int | float) else "")
    )
    if facts.get("is_free_tier"):
        print("   WARN the account is on the free tier: paid models answer only after credits")
    model = model_facts(AGENT_MODEL)
    if model is None:
        print(
            f"   WARN {AGENT_MODEL} is not in OpenRouter's model list (SRE_AGENT_MODEL can differ)"
        )
    else:
        params = model.get("supported_parameters", [])
        print(
            f"   {AGENT_MODEL}: tool calling {'yes' if 'tools' in params else 'NO'},"
            f" temperature {'yes' if 'temperature' in params else 'not offered'}"
        )
    checked = {"on": dt.date.today().isoformat(), "credit_limit": limit}
    store_and_seal(OPENROUTER_SECRET, {"value": key}, checked)


# -------------------------------------------------------------- the SRE agent's GitHub App


def cmd_github_app(args: argparse.Namespace) -> None:
    """Seal the App's ID, installation and private key, after GitHub confirms ADR-0016's limits:
    installed on this repository only, exactly contents + pull requests (write) and metadata
    (read), and on no ruleset's bypass list (so GitHub, not only our code, stops it merging)."""
    pem_path = Path(args.pem)
    if not pem_path.exists():
        sys.exit(f"no private key at {pem_path}: save the .pem GitHub downloaded there")
    if pem_path.stat().st_mode & 0o077:
        sys.exit(f"{pem_path} can be read by other users: chmod 600 {pem_path}")
    pem = pem_path.read_text()
    if "PRIVATE KEY-----" not in pem:
        sys.exit(f"{pem_path} is not a PEM private key")
    app_id = (
        args.app_id or input("GitHub App ID (a number, on the App's settings page): ")
    ).strip()
    if not app_id.isdigit():
        sys.exit(f"{app_id!r} is not an App ID (a number; the Client ID starts with Iv)")
    repo = github_app.repository()
    facts = github_app.inspect(app_id, pem, repo)
    found = github_app.problems(facts, repo)
    found += [
        f"ruleset {n!r} lets the App bypass it" for n in github_app.bypassing_rulesets(app_id, repo)
    ]
    print(
        f"GitHub App {facts['slug']} (ID {app_id}), installation {facts['installation_id']}:"
        f" reaches {', '.join(facts['repositories'])};"
        f" permissions {facts['installation_permissions']}"
    )
    if facts["events"]:
        print(f"   WARN it subscribes to webhook events {facts['events']}: it needs none")
    if found:
        for problem in found:
            print(f"   FAIL {problem}")
        sys.exit("not sealed: change the App's settings on GitHub, then run this again")
    data = {
        "app-id": app_id,
        "installation-id": str(facts["installation_id"]),
        "private-key": pem,
    }
    checked = {"on": dt.date.today().isoformat(), "slug": facts["slug"], "repository": repo}
    store_and_seal(APP_SECRET, data, checked)
    print("   next: make github-rules-check (GitHub must refuse the App's merge, push and tags)")


def ghcr_expiry_warning(env: str) -> str:
    source = plaintext_file(env)
    if not source.exists():
        return ""
    expires = json.loads(source.read_text()).get(GHCR_SECRET, {}).get("data", {}).get("expires")
    if not expires:
        return ""
    days = (dt.date.fromisoformat(expires) - dt.date.today()).days
    if days < 14:
        return f"GHCR pull token expires {expires} ({days} days): make ghcr-token with a new one"
    return ""


# --------------------------------------------------------------------------- check


def cmd_check(args: argparse.Namespace) -> None:
    sealed = json.loads(run(kubectl("get", "sealedsecrets", "-n", args.env, "-o", "json")))["items"]
    expected = set(CATALOG)
    found = {s["metadata"]["name"] for s in sealed}
    problems = [f"missing SealedSecret {name}" for name in sorted(expected - found)]
    for s in sealed:
        name = s["metadata"]["name"]
        cond = next(
            (c for c in s.get("status", {}).get("conditions", []) if c["type"] == "Synced"), {}
        )
        if cond.get("status") != "True":
            problems.append(f"{name}: not decrypted ({cond.get('message', 'no status yet')})")
        elif not run(kubectl("get", "secret", "-n", args.env, name, "-o", "name"), check=False):
            problems.append(f"{name}: no Secret")
    print(f"{args.env}: {len(sealed)} SealedSecrets, {len(problems)} problems")
    warning = ghcr_expiry_warning(args.env)
    if warning:
        print(f"   WARN {warning}")
    for p in problems:
        print(f"   FAIL {p}")
    sys.exit(1 if problems else 0)


def wait_for(condition, what: str, timeout: float = 60) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            sys.exit(f"timed out waiting for {what}")
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    seal = sub.add_parser("seal")
    seal.add_argument("--env", choices=["staging", "prod"], required=True)
    seal.add_argument(
        "--rotate", nargs="*", default=[], help="give these secrets new random values"
    )
    seal.set_defaults(func=cmd_seal)
    sub.add_parser("cert").set_defaults(func=cmd_cert)
    sub.add_parser("backup").set_defaults(func=cmd_backup)
    sub.add_parser("restore").set_defaults(func=cmd_restore)
    check = sub.add_parser("check")
    check.add_argument("--env", choices=["staging", "prod"], default="prod")
    check.set_defaults(func=cmd_check)
    sub.add_parser("ghcr-token").set_defaults(func=cmd_ghcr_token)
    sub.add_parser("openrouter-key").set_defaults(func=cmd_openrouter_key)
    app = sub.add_parser("github-app")
    app.add_argument("--app-id", default="", help="the App's ID (asked for if not given)")
    app.add_argument("--pem", default=str(APP_KEY), help="the App's private key (.pem)")
    app.set_defaults(func=cmd_github_app)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
