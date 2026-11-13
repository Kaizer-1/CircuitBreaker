#!/usr/bin/env python3
"""The local Kubernetes platform: make k8s-up / k8s-down / k8s-status / k8s-images / k8s-reset.

    up [--env prod|staging]  cluster + registry + images + everything installed, then wait
    down                     stop the cluster's node container (data kept, RAM freed)
    status                   pods per namespace and the cluster's memory
    images [svc ...]         build changed images, push them to the local registry
    reset                    DELETE the cluster and all its data (asks; run by a person)

Standard library only, so it runs with the system python3. Phase 6 moves cluster creation into
Terraform (`make platform`) and deploys through Argo CD; until then this is the platform's entry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLUSTER = "circuitbreaker"
NODE = f"{CLUSTER}-control-plane"
KIND_CONFIG = ROOT / "infra" / "kind" / "cluster.yaml"
REGISTRY_NAME = "kind-registry"
REGISTRY_IMAGE = "registry:3.0.0"
REGISTRY_PORT = 5001
REGISTRY = f"localhost:{REGISTRY_PORT}"
CONTEXT = f"kind-{CLUSTER}"
MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
# Image tags chosen by `images`, read by `up` (git-ignored: build/).
TAGS_FILE = ROOT / "build" / "k8s" / "image-tags.json"

# What gets an image, and where it is built from.
IMAGES = {
    "api-gateway": "services/api-gateway",
    "order-service": "services/order-service",
    "matching-engine": "services/matching-engine",
    "market-data-service": "services/market-data-service",
    "portfolio-service": "services/portfolio-service",
    "bot-traders": "services/bot-traders",
    "sre-agent": "services/sre-agent",
    "chaos-service": "services/chaos-service",
    "web": "web",
}


# --------------------------------------------------------------------------- helpers


def run(
    cmd: list[str], *, check: bool = True, capture: bool = False, input_text: str | None = None
) -> str:
    result = subprocess.run(cmd, check=False, capture_output=capture, text=True, input=input_text)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        sys.exit(f"failed ({result.returncode}): {' '.join(cmd)}\n{detail}")
    return (result.stdout or "").strip() if capture else ""


def ok(cmd: list[str]) -> bool:
    return subprocess.run(cmd, capture_output=True, check=False).returncode == 0


def step(msg: str) -> None:
    print(f"\n== {msg}", flush=True)


def kubectl(*args: str) -> list[str]:
    return ["kubectl", "--context", CONTEXT, *args]


def node_state() -> str:
    """'running', 'exited' (stopped by k8s-down), or '' (no cluster)."""
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", NODE],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() if out.returncode == 0 else ""


# --------------------------------------------------------------------------- registry and cluster


def ensure_registry() -> None:
    state = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", REGISTRY_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    if state.returncode != 0:
        step(f"local image registry {REGISTRY} ({REGISTRY_IMAGE})")
        run(
            [
                "docker",
                "run",
                "-d",
                "--restart=always",
                "-p",
                f"127.0.0.1:{REGISTRY_PORT}:5000",
                "--network",
                "bridge",
                "--name",
                REGISTRY_NAME,
                REGISTRY_IMAGE,
            ]
        )
    elif state.stdout.strip() != "running":
        run(["docker", "start", REGISTRY_NAME], capture=True)


def connect_registry() -> None:
    """Put the registry on kind's network; tell the node's containerd where localhost:5001 is."""
    networks = run(
        ["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", REGISTRY_NAME],
        capture=True,
    )
    if "kind" not in json.loads(networks):
        run(["docker", "network", "connect", "kind", REGISTRY_NAME])
    certs = f"/etc/containerd/certs.d/{REGISTRY}"
    run(["docker", "exec", NODE, "mkdir", "-p", certs])
    run(
        ["docker", "exec", "-i", NODE, "cp", "/dev/stdin", f"{certs}/hosts.toml"],
        input_text=f'[host."http://{REGISTRY_NAME}:5000"]\n',
    )
    # The documented way to tell tools which registry this cluster uses (KEP-1755).
    run(
        kubectl("apply", "-f", "-"),
        input_text=f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "{REGISTRY}"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
""",
        capture=True,
    )


def wait_api(timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ok(kubectl("get", "--raw", "/readyz")):
            return
        time.sleep(2)
    sys.exit("the Kubernetes API did not become ready")


def ensure_cluster() -> None:
    state = node_state()
    if state == "":
        step(f"kind cluster '{CLUSTER}' (1 node, {KIND_CONFIG.relative_to(ROOT)})")
        run(["kind", "create", "cluster", "--config", str(KIND_CONFIG), "--wait", "120s"])
    elif state != "running":
        step(f"starting the stopped cluster '{CLUSTER}' (data kept)")
        run(["docker", "start", NODE], capture=True)
    wait_api()
    connect_registry()


# --------------------------------------------------------------------------- images


def image_tag(path: str) -> str:
    """The git SHA of HEAD; with uncommitted changes under `path`, plus a hash of those changes.

    Build once per change: an unchanged service keeps its tag, so it is neither rebuilt nor rolled.
    """
    sha = run(["git", "-C", str(ROOT), "rev-parse", "--short=12", "HEAD"], capture=True)
    dirty = run(["git", "-C", str(ROOT), "status", "--porcelain", "--", path], capture=True)
    if not dirty:
        return sha
    h = hashlib.sha256(
        run(["git", "-C", str(ROOT), "diff", "HEAD", "--", path], capture=True).encode()
    )
    untracked = run(
        ["git", "-C", str(ROOT), "ls-files", "--others", "--exclude-standard", "--", path],
        capture=True,
    )
    for name in sorted(untracked.splitlines()):
        h.update(name.encode())
        h.update((ROOT / name).read_bytes())
    return f"{sha}-dirty-{h.hexdigest()[:8]}"


def in_registry(repo: str, tag: str) -> bool:
    req = urllib.request.Request(
        f"http://{REGISTRY}/v2/{repo}/manifests/{tag}",
        method="HEAD",
        headers={"Accept": MANIFEST_TYPES},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 (local http:// registry)
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def build_images(names: list[str]) -> dict[str, str]:
    ensure_registry()
    tags = json.loads(TAGS_FILE.read_text()) if TAGS_FILE.exists() else {}
    for name in names:
        path = IMAGES[name]
        tag = image_tag(path)
        repo = f"circuitbreaker/{name}"
        ref = f"{REGISTRY}/{repo}:{tag}"
        if in_registry(repo, tag):
            print(f"   {name:<20} {tag}  (already in the registry)")
        else:
            step(f"build {name}:{tag}")
            run(["docker", "build", "-q", "-t", ref, str(ROOT / path)])
            run(["docker", "push", "-q", ref])
        tags[name] = tag
    TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TAGS_FILE.write_text(json.dumps(tags, indent=2) + "\n")
    return tags


# --------------------------------------------------------------------------- commands


def cmd_up(args: argparse.Namespace) -> None:
    for tool in ("docker", "kind", "kubectl", "helm"):
        if not shutil.which(tool):
            sys.exit(f"{tool} is not installed: run make doctor")
    ensure_registry()
    ensure_cluster()
    if not args.skip_images:
        step("images")
        build_images(list(IMAGES))
    print(f"\ncluster '{CLUSTER}' is up (kubectl context {CONTEXT})")


def cmd_down(_: argparse.Namespace) -> None:
    if node_state() == "running":
        step("stopping the cluster's node (data kept; `make k8s-up` starts it again)")
        run(["docker", "stop", NODE], capture=True)
    else:
        print("the cluster is not running")


def cmd_status(_: argparse.Namespace) -> None:
    state = node_state()
    print(f"cluster '{CLUSTER}': {state or 'not created'}")
    if state != "running":
        return
    subprocess.run(
        kubectl("get", "pods", "-A", "-o", "wide", "--sort-by=.metadata.namespace"), check=False
    )
    stats = run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.Name}} {{.MemUsage}}",
            NODE,
            REGISTRY_NAME,
        ],
        capture=True,
        check=False,
    )
    print("\nmemory (Docker):\n" + stats)


def cmd_images(args: argparse.Namespace) -> None:
    names = args.services or list(IMAGES)
    unknown = [n for n in names if n not in IMAGES]
    if unknown:
        sys.exit(f"unknown image(s): {', '.join(unknown)}; choose from {', '.join(IMAGES)}")
    build_images(names)


def cmd_reset(args: argparse.Namespace) -> None:
    if os.environ.get("CONFIRM") != "yes":
        answer = input(
            f"This DELETES the kind cluster '{CLUSTER}' and ALL its data. Type 'yes' to continue: "
        )
        if answer != "yes":
            sys.exit("Aborted.")
    run(["kind", "delete", "cluster", "--name", CLUSTER])
    print("cluster deleted; the image registry is kept (its images are rebuilt from git anyway)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up")
    up.add_argument("--env", choices=["prod", "staging"], default="prod")
    up.add_argument("--skip-images", action="store_true", help="deploy the tags built last time")
    up.set_defaults(func=cmd_up)
    sub.add_parser("down").set_defaults(func=cmd_down)
    sub.add_parser("status").set_defaults(func=cmd_status)
    images = sub.add_parser("images")
    images.add_argument("services", nargs="*")
    images.set_defaults(func=cmd_images)
    sub.add_parser("reset").set_defaults(func=cmd_reset)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
