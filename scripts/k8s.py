#!/usr/bin/env python3
"""The local Kubernetes platform: make k8s-up / k8s-down / k8s-status / k8s-images / k8s-reset.

    up [--env prod|staging]  developing: images + the umbrella chart into a namespace Argo CD does
                             not manage (no cluster yet: Terraform makes one, without Argo CD). In a
                             namespace Argo CD manages it deploys nothing: it starts a stopped
                             cluster, applies PUBLIC_BASE_URL and waits until Argo CD reports it
                             Synced and Healthy (releases come from Git, ADR-0014)
    down                     stop the cluster's node container (data kept, RAM freed)
    status                   pods per namespace, Argo CD's Applications, memory, the QR code's URL
    set-url URL [--env]      change only the URL in the join QR code (PUBLIC_BASE_URL), data kept
    bots [--stop] [--env]    turn the 20 bots on or off
    sync [--env] [svc]       ask Argo CD to look at Git now instead of within 30 s (make sync)
    secret NAME [--env]      one value of a Secret, e.g. admin-passcode (make k8s-passcode)
    argocd                   Argo CD's web UI on http://localhost:8443 and its admin password
    images [svc ...]         build changed images, push them to the local registry
    charts                   download the pinned third-party charts once (make platform needs them)
    registry                 start the local registry and connect it to the cluster (Terraform)
    reset                    DELETE the cluster and all its data (asks; run by a person)

The platform itself (cluster, namespaces, Traefik, monitoring, Sealed Secrets, Argo CD) is
Terraform's: make platform (infra/envs/local). Standard library only, so it runs with the system
python3.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
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


TERRAFORM_DIR = ROOT / "infra" / "envs" / "local"


def terraform_apply(*variables: str) -> None:
    """infra/envs/local: the whole platform (ADR-0015). make platform plans first and asks; make
    k8s-up calls this only to create a missing cluster for development (no Argo CD)."""
    if not shutil.which("terraform"):
        sys.exit("terraform is not installed: run make doctor")
    cache_charts()
    run(["terraform", f"-chdir={TERRAFORM_DIR}", "init", "-input=false"], capture=True)
    args = [arg for var in variables for arg in ("-var", var)]
    run(["terraform", f"-chdir={TERRAFORM_DIR}", "apply", "-input=false", "-auto-approve", *args])


def ensure_cluster() -> None:
    state = node_state()
    if state == "":
        step(f"no cluster: Terraform creates '{CLUSTER}' for development, without Argo CD")
        terraform_apply("gitops_environments=[]")
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


# ------------------------------------------------------------------- the system, per environment

CHART = ROOT / "charts" / "circuitbreaker"
RELEASE = "circuitbreaker"
# Host port of each environment's ingress (infra/kind/cluster.yaml).
ENV_PORTS = {"prod": 80, "staging": 8090}
# The local-development placeholders from docker-compose.yml (never real secrets); an environment
# variable overrides each. Phase 6: Terraform's environment module creates these Secrets instead.
DEV_SECRETS = {
    "jwt-secret": {"value": ("JWT_SECRET", "cb-dev-jwt-secret-for-local-compose-only")},
    "bot-secret": {"value": ("BOT_SECRET", "cb-dev-bot-secret")},
    "admin-passcode": {"value": ("ADMIN_PASSCODE", "cb-dev-admin-passcode")},
    # Not the image's default (cassandra): on Kubernetes the init job replaces that (ALTER ROLE).
    "cassandra-admin": {"password": ("CASSANDRA_ADMIN_PASSWORD", "cb_dev_cassandra_admin")},
    "postgres-admin": {"password": ("POSTGRES_ADMIN_PASSWORD", "postgres")},
    "order-service-db": {"password": ("ORDER_SERVICE_DB_PASSWORD", "cb_dev_order_service")},
    "matching-engine-db": {"password": ("MATCHING_ENGINE_DB_PASSWORD", "cb_dev_matching_engine")},
    "market-data-service-db": {"password": ("MARKET_DATA_DB_PASSWORD", "cb_dev_market_data")},
    "sre-agent-db": {"password": ("SRE_AGENT_DB_PASSWORD", "cb_dev_sre_agent")},
    "portfolio-service-db": {"password": ("PORTFOLIO_DB_PASSWORD", "cb_dev_portfolio")},
    # The agent reads the placeholder as "no key": the rule-based fallback decides (ADR-0021).
    # export OPENROUTER_API_KEY=... before make k8s-up to try the model while developing.
    "openrouter-api-key": {"value": ("OPENROUTER_API_KEY", "PLACEHOLDER-filled-in-phase-7")},
}


# Third-party charts, pinned (ADR-0018, ADR-0019): (chart, repo, version). Terraform installs them
# (infra/modules/*) from the packages downloaded here.
CHARTS = [
    ("traefik", "oci://ghcr.io/traefik/helm", "42.3.1"),
    ("kube-prometheus-stack", "oci://ghcr.io/prometheus-community/charts", "92.22.0"),
    ("loki", "oci://ghcr.io/grafana-community/helm-charts", "20.4.1"),
    # Not published as OCI artifacts (or, for Argo CD, as one we checked), and their repositories'
    # indexes are large for a slow connection: the release packages themselves, checked against the
    # indexes' sha256 (CHART_DIGESTS).
    (
        "alloy",
        "https://github.com/grafana/helm-charts/releases/download/alloy-1.15.0/alloy-1.15.0.tgz",
        "1.15.0",
    ),
    (
        "argo-cd",
        "https://github.com/argoproj/argo-helm/releases/download/argo-cd-10.18.2/argo-cd-10.18.2.tgz",
        "10.18.2",
    ),
    (
        "sealed-secrets",
        "https://github.com/bitnami/sealed-secrets/releases/download/helm-v2.22.0/sealed-secrets-2.22.0.tgz",
        "2.22.0",
    ),
]
# Namespaces of the platform's pods (waited for, shown by status).
PLATFORM_NAMESPACES = ["argocd", "observability", "traefik"]


CHART_CACHE = ROOT / "build" / "k8s" / "charts"
# sha256 of chart packages downloaded by URL (from the chart repository's index.yaml).
CHART_DIGESTS = {
    "alloy-1.15.0.tgz": "2ecc96c05004cc01a45ab23cb390194ad5259ec9d02b42bf148d4468d013aaa3",  # gitleaks:allow  # noqa: E501
    "argo-cd-10.18.2.tgz": "6900d104854a18c733616a56853012eb9a136f2256a9d35de73cda03689a59d1",  # gitleaks:allow  # noqa: E501
    "sealed-secrets-2.22.0.tgz": "1ed23bb224652aa6c8477979d4bddccd207205b27b12b120f62f360890c80606",  # gitleaks:allow  # noqa: E501
}


def cached_chart(chart: str, repo: str, version: str) -> str:
    """The pinned chart's package, downloaded once: later runs (and demo day) need no internet.

    The repositories' index files are large (prometheus-community's is many MB) and timed out on a
    slow connection, so charts come from OCI registries where they are published, and only once.
    """
    package = CHART_CACHE / f"{chart}-{version}.tgz"
    if not package.exists():
        CHART_CACHE.mkdir(parents=True, exist_ok=True)
        # A package URL, or an OCI registry (neither needs the repository's index).
        source = [repo] if repo.endswith(".tgz") else [f"{repo}/{chart}", "--version", version]
        for attempt in (1, 2, 3):
            pulled = subprocess.run(  # noqa: S603 (fixed command)
                ["helm", "pull", *source, "-d", str(CHART_CACHE)],
                capture_output=True,
                text=True,
                check=False,
            )
            if pulled.returncode == 0:
                expected = CHART_DIGESTS.get(package.name)
                actual = hashlib.sha256(package.read_bytes()).hexdigest()
                if expected and actual != expected:
                    package.unlink()
                    sys.exit(f"{package.name}: sha256 {actual} is not the published {expected}")
                break
            print(f"   helm pull {chart} {version} failed (try {attempt}): {pulled.stderr.strip()}")
            time.sleep(5)
        else:
            sys.exit(f"could not download chart {chart} {version} from {repo}")
    return str(package)


def cache_charts() -> None:
    for chart, repo, version in CHARTS:
        cached_chart(chart, repo, version)


def install_observability() -> None:
    """Our dashboards and alert rules while developing (on a GitOps cluster Argo CD does it)."""
    if argo_manages("observability"):
        return
    step("our dashboards and alert rules (charts/observability)")
    run(
        [
            "helm",
            "upgrade",
            "--install",
            "circuitbreaker-observability",
            str(ROOT / "charts" / "observability"),
            "--kube-context",
            CONTEXT,
            "--namespace",
            "observability",
        ],
        capture=True,
    )


# --------------------------------------------------------------------------- Argo CD (GitOps)


def argo_applications(env: str | None = None) -> list[dict]:
    """Argo CD's Applications (all, or one environment's); [] when Argo CD is not installed."""
    selector = ["-l", f"circuitbreaker.io/env={env}"] if env else []
    out = subprocess.run(  # noqa: S603 (fixed command)
        kubectl("get", "applications.argoproj.io", "-n", "argocd", *selector, "-o", "json"),
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return []
    return list(json.loads(out.stdout)["items"])


def argo_manages(namespace: str) -> bool:
    if namespace == "observability":
        return any(a["metadata"]["name"] == "observability" for a in argo_applications())
    return bool(argo_applications(namespace))


def argo_not_settled(env: str) -> list[str]:
    """Applications of env that are not Synced and Healthy yet, or whose sync is still running:
    an Application is Synced and Healthy while its PostSync smoke test runs (CI, 2026-11-24).
    None at all counts too: right after Terraform installs Argo CD, the ApplicationSet has not
    generated them yet."""
    apps = argo_applications(env)
    if not apps:
        return ["no Applications yet"]
    waiting = []
    for app in apps:
        status = app.get("status", {})
        sync = status.get("sync", {}).get("status", "?")
        health = status.get("health", {}).get("status", "?")
        if status.get("operationState", {}).get("phase") == "Running":
            waiting.append(f"{app['metadata']['name']} (sync running)")
        elif sync != "Synced" or health != "Healthy":
            waiting.append(f"{app['metadata']['name']} ({sync}/{health})")
    return waiting


def wait_argo(env: str, timeout: float = 900) -> float:
    """Until every Application of env has been settled for 10 s (one look could catch the
    ApplicationSet halfway through generating them)."""
    start = time.monotonic()
    last = ""
    settled_since: float | None = None
    while True:
        now = time.monotonic()
        if waiting := argo_not_settled(env):
            settled_since = None
            if now - start > timeout:
                sys.exit(f"Argo CD: not settled after {timeout:.0f} s: {', '.join(waiting)}")
            line = f"   Argo CD: {len(waiting)} not settled: {', '.join(sorted(waiting))}"
            if line != last:
                print(line, flush=True)
                last = line
        elif settled_since is None:
            settled_since = now
        elif now - settled_since >= 10:
            return settled_since - start
        time.sleep(5)


def local_url(env: str) -> str:
    """The environment's entry from this laptop (Traefik's host port)."""
    port = ENV_PORTS[env]
    return "http://localhost" if port == 80 else f"http://localhost:{port}"


OPERATOR_SETTINGS = "operator-settings"


def operator_settings(env: str) -> dict[str, str]:
    """The ConfigMap of tonight's settings (the QR code's URL, the bots on or off). Git and Argo CD
    never own it, so no release and no sync resets it; the chart reads it with optional refs."""
    out = subprocess.run(  # noqa: S603 (fixed command)
        kubectl("get", "configmap", OPERATOR_SETTINGS, "-n", env, "-o", "json"),
        capture_output=True,
        text=True,
        check=False,
    )
    return dict(json.loads(out.stdout).get("data") or {}) if out.returncode == 0 else {}


def set_operator_settings(env: str, changes: dict[str, str]) -> bool:
    """Merge changes into the ConfigMap; True when something changed."""
    data = operator_settings(env)
    if all(data.get(k) == v for k, v in changes.items()):
        return False
    data.update(changes)
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": OPERATOR_SETTINGS,
            "namespace": env,
            "labels": {"app.kubernetes.io/part-of": "circuitbreaker"},
        },
        "data": data,
    }
    run(kubectl("apply", "-f", "-"), input_text=json.dumps(manifest), capture=True)
    return True


def restart(env: str, deployment: str) -> None:
    """A rolling restart: the pods read their settings again (Argo CD ignores the annotation)."""
    run(kubectl("rollout", "restart", "-n", env, f"deployment/{deployment}"), capture=True)
    run(kubectl("rollout", "status", "-n", env, f"deployment/{deployment}", "--timeout=300s"))


def public_base_url(env: str) -> str:
    """Where phones reach this environment (the join QR code): PUBLIC_BASE_URL when given, always;
    otherwise the URL the environment already has (kept in operator-settings), so a restart, an
    upgrade or k8s-bots never puts the QR code back to localhost; localhost only at first."""
    given = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if given:
        return checked_url(given)
    return operator_settings(env).get("PUBLIC_BASE_URL") or local_url(env)


def apply_public_url(env: str) -> None:
    """PUBLIC_BASE_URL (if given) into operator-settings; restarts web when it changed."""
    if os.environ.get("PUBLIC_BASE_URL", "").strip():
        url = public_base_url(env)
        if set_operator_settings(env, {"PUBLIC_BASE_URL": url}) and deployment_exists(env, "web"):
            step(f"{env}: the join QR code now points at {url}")
            restart(env, "web")


def deployment_exists(env: str, name: str) -> bool:
    return ok(kubectl("get", "deployment", name, "-n", env))


def checked_url(url: str) -> str:
    """http(s)://host[:port] and nothing else: the web container writes it into /config.js."""
    parsed = urllib.parse.urlsplit(url.strip().rstrip("/"))
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path:
        sys.exit(f"not a base URL: {url!r} (expected e.g. http://172.20.10.3)")
    return f"{parsed.scheme}://{parsed.netloc}"


def served_url(env: str) -> str | None:
    """The URL the running web pod puts in the QR code (its /config.js), or None if unreachable."""
    try:
        with urllib.request.urlopen(f"{local_url(env)}/config.js", timeout=5) as response:  # noqa: S310
            text = response.read().decode()
    except (urllib.error.URLError, OSError):
        return None
    found = re.search(r'publicBaseUrl:\s*"([^"]*)"', text)
    return found.group(1) if found else None


def lan_address() -> str | None:
    """This laptop's address on its current network (the hotspot), if it has one."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 80))  # no packet is sent; it only picks the route
            address = str(sock.getsockname()[0])
        except OSError:
            return None
    return None if address.startswith("127.") else address


def print_public_url(env: str) -> None:
    url = served_url(env)
    print(f"\nQR code / phones ({env}): {url or 'web not reachable'}")
    lan = lan_address()
    host = urllib.parse.urlsplit(url).hostname if url else None
    if url and host in ("localhost", "127.0.0.1"):
        print(f"   phones cannot reach localhost: make k8s-set-url url=http://{lan or '<LAN IP>'}")
    if url and lan and host not in (lan, "localhost", "127.0.0.1"):
        print(f"   but this laptop is now {lan} on its network: make k8s-set-url url=http://{lan}")


def ensure_namespace(name: str) -> None:
    manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}
    run(kubectl("apply", "-f", "-"), input_text=json.dumps(manifest), capture=True)


def apply_secrets(env: str) -> None:
    items = []
    for name, keys in DEV_SECRETS.items():
        data = {key: os.environ.get(var, default) for key, (var, default) in keys.items()}
        items.append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": name,
                    "namespace": env,
                    "labels": {"circuitbreaker.io/dev": "true"},
                },
                "type": "Opaque",
                "stringData": data,
            }
        )
    manifest = {"apiVersion": "v1", "kind": "List", "items": items}
    run(kubectl("apply", "-f", "-"), input_text=json.dumps(manifest), capture=True)


def values_files(env: str, overrides: dict[str, dict[str, object]]) -> list[str]:
    """deploy/envs/<env>/global.yaml, each service's file, then this run's overrides (last wins)."""
    env_dir = ROOT / "deploy" / "envs" / env
    files = [
        env_dir / "global.yaml",
        *sorted(f for f in env_dir.glob("*.yaml") if f.name != "global.yaml"),
    ]
    generated = ROOT / "build" / "k8s" / f"values-{env}.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text(json.dumps(overrides, indent=2) + "\n")  # JSON is YAML
    args: list[str] = []
    for f in [*files, generated]:
        args += ["-f", str(f)]
    return args


def deploy(env: str, extra: dict[str, dict[str, object]] | None = None) -> None:
    tags = json.loads(TAGS_FILE.read_text()) if TAGS_FILE.exists() else {}
    if not tags:
        sys.exit("no images built yet: run make k8s-images")
    # This build's images, from the local registry (deploy/envs/ names GHCR for CI's images).
    overrides: dict[str, dict[str, object]] = {
        # No PostSync smoke Job: without Argo CD it would be an ordinary Job, run once.
        "global": {"environment": env, "image": {"pullSecrets": []}, "smoke": {"mode": ""}},
        **{name: {"image": {"registry": REGISTRY, "tag": tag}} for name, tag in tags.items()},
    }
    for key, value in (extra or {}).items():
        overrides.setdefault(key, {}).update(value)
    step(f"namespace {env}: Secrets (local placeholders) and the umbrella chart")
    ensure_namespace(env)
    apply_secrets(env)
    run(
        [
            "helm",
            "upgrade",
            "--install",
            RELEASE,
            str(CHART),
            "--kube-context",
            CONTEXT,
            "--namespace",
            env,
            *values_files(env, overrides),
        ],
        capture=True,
    )


def pods_not_ready(namespace: str) -> list[str]:
    out = json.loads(run(kubectl("get", "pods", "-n", namespace, "-o", "json"), capture=True))
    waiting = []
    for pod in out["items"]:
        name, phase = pod["metadata"]["name"], pod["status"].get("phase")
        if phase == "Succeeded":
            continue
        ready = any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in pod["status"].get("conditions", [])
        )
        if phase != "Running" or not ready:
            statuses = pod["status"].get("containerStatuses") or []
            reason = next(
                (s["state"]["waiting"]["reason"] for s in statuses if "waiting" in s["state"]),
                phase,
            )
            waiting.append(f"{name} ({reason})")
    # A rollout is done only when every replica runs the new template (old pods may still be ready).
    workloads = json.loads(
        run(kubectl("get", "deployments,statefulsets", "-n", namespace, "-o", "json"), capture=True)
    )
    for w in workloads["items"]:
        spec, status = w["spec"], w.get("status", {})
        want = spec.get("replicas", 1)
        done = (
            status.get("observedGeneration", 0) >= w["metadata"]["generation"]
            and status.get("updatedReplicas", 0) == want
            and status.get("readyReplicas", 0) == want
            and status.get("replicas", 0) == want
        )
        if not done:
            waiting.append(f"{w['kind'].lower()}/{w['metadata']['name']} (rolling out)")
    return waiting


def wait_ready(namespace: str, timeout: float = 600) -> float:
    start = time.monotonic()
    last = ""
    while True:
        waiting = pods_not_ready(namespace)
        if not waiting:
            return time.monotonic() - start
        if time.monotonic() - start > timeout:
            sys.exit(f"not ready after {timeout:.0f} s in {namespace}: {', '.join(waiting)}")
        line = f"   waiting for {len(waiting)}: {', '.join(sorted(waiting))}"
        if line != last:
            print(line, flush=True)
            last = line
        time.sleep(3)


# --------------------------------------------------------------------------- commands


def cmd_up(args: argparse.Namespace) -> None:
    for tool in ("docker", "kind", "kubectl", "helm"):
        if not shutil.which(tool):
            sys.exit(f"{tool} is not installed: run make doctor")
    ensure_registry()
    ensure_cluster()
    if argo_manages(args.env):
        # GitOps (ADR-0014): what runs in this namespace comes from Git through Argo CD, never from
        # here. Only tonight's settings (the QR code's URL) are the operator's.
        step(f"{args.env} is deployed by Argo CD from Git: nothing is built or deployed from here")
        print(
            f"   to release: commit to deploy/envs/{args.env}/ (CI does it for staging; make"
            " promote for prod), then make sync to look at once"
        )
        apply_public_url(args.env)
        step(f"waiting until Argo CD reports {args.env} Synced and Healthy, and every pod is ready")
        took = wait_argo(args.env)
        took += sum(wait_ready(ns) for ns in [*PLATFORM_NAMESPACES, args.env])
    else:
        if not args.skip_images:
            step("images")
            build_images(list(IMAGES))
        install_observability()
        if os.environ.get("PUBLIC_BASE_URL", "").strip():
            set_operator_settings(args.env, {"PUBLIC_BASE_URL": public_base_url(args.env)})
        deploy(args.env)
        step(f"waiting until every pod in {args.env} and the platform is ready")
        took = sum(
            wait_ready(ns) for ns in [*PLATFORM_NAMESPACES, args.env] if namespace_exists(ns)
        )
    print(f"\nall pods in {args.env} ready ({took:.0f} s).  (kubectl context {CONTEXT})")
    print_public_url(args.env)


def namespace_exists(name: str) -> bool:
    return ok(kubectl("get", "namespace", name))


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
    # The RAM budget's numbers (deployment doc): what each namespace's pods use (working set, from
    # the kubelet), and the node's memory split into what the processes hold (anon) and the page
    # cache the kernel can give back (file).
    summary = json.loads(
        run(kubectl("get", "--raw", f"/api/v1/nodes/{NODE}/proxy/stats/summary"), capture=True)
    )
    per_ns: dict[str, float] = {}
    for pod in summary["pods"]:
        used = (pod.get("memory") or {}).get("workingSetBytes") or 0
        per_ns[pod["podRef"]["namespace"]] = (
            per_ns.get(pod["podRef"]["namespace"], 0) + used / 2**20
        )
    print("\nmemory by namespace (pods' working set, MiB):")
    for ns, mib in sorted(per_ns.items(), key=lambda kv: -kv[1]):
        print(f"   {ns:<20} {mib:7.0f}")
    print(f"   {'all pods':<20} {sum(per_ns.values()):7.0f}")
    cgroup = run(["docker", "exec", NODE, "cat", "/sys/fs/cgroup/memory.stat"], capture=True)
    stat = dict(line.split() for line in cgroup.splitlines())
    anon, file_ = int(stat["anon"]) / 2**20, int(stat["file"]) / 2**20
    kernel = int(stat.get("kernel", 0)) / 2**20
    print(
        f"\nnode: processes (anon) {anon:.0f} MiB + kernel {kernel:.0f} MiB;"
        f" page cache {file_:.0f} MiB (the kernel can give it back)"
    )
    apps = argo_applications()
    if apps:
        print("\nArgo CD (sync / health):")
        for app in sorted(apps, key=lambda a: a["metadata"]["name"]):
            st = app.get("status", {})
            revision = (st.get("sync", {}).get("revision") or "")[:8]
            print(
                f"   {app['metadata']['name']:<28} {st.get('sync', {}).get('status', '?'):<10}"
                f" {st.get('health', {}).get('status', '?'):<12} {revision}"
            )
    for env in ENV_PORTS:
        if deployment_exists(env, "web"):
            print_public_url(env)


def cmd_set_url(args: argparse.Namespace) -> None:
    """Point the join QR code at a new address (make k8s-set-url url=...). Only the URL changes, in
    operator-settings (outside Git, so Argo CD keeps it); only the web pod restarts; no data is
    touched."""
    if node_state() != "running":
        sys.exit("the cluster is not running: PUBLIC_BASE_URL=http://<LAN IP> make k8s-up")
    url = checked_url(args.url)
    step(f"{args.env}: the join QR code now points at {url}")
    set_operator_settings(args.env, {"PUBLIC_BASE_URL": url})
    restart(args.env, "web")
    deadline = time.monotonic() + 30
    while served_url(args.env) != url:
        if time.monotonic() > deadline:
            sys.exit(f"the web pod still serves {served_url(args.env)!r}, not {url!r}")
        time.sleep(1)
    print_public_url(args.env)


def cmd_images(args: argparse.Namespace) -> None:
    names = args.services or list(IMAGES)
    unknown = [n for n in names if n not in IMAGES]
    if unknown:
        sys.exit(f"unknown image(s): {', '.join(unknown)}; choose from {', '.join(IMAGES)}")
    build_images(names)


# Internal APIs the check scripts read directly (Compose maps them to host ports 8002-8005). On the
# cluster they are reached by port-forward, which enters the pod's own network namespace, so the
# NetworkPolicies (which keep every other pod out) do not apply to it.
FORWARDS = {
    "ENGINE": ("matching-engine", 18002),
    "MARKET": ("market-data-service", 18003),
    "PORTFOLIO": ("portfolio-service", 18004),
    "BOTS": ("bot-traders", 18005),
}


# What the check scripts (scripts/stack.py) log in with: the presenter's passcode, the bot secret.
SCRIPT_SECRETS = {
    "ADMIN_PASSCODE": ("admin-passcode", "value"),
    "BOT_SECRET": ("bot-secret", "value"),
}


def secret_value(namespace: str, name: str, key: str) -> str | None:
    import base64

    out = subprocess.run(  # noqa: S603 (fixed command)
        kubectl("get", "secret", name, "-n", namespace, "-o", "json"),
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return None
    encoded = (json.loads(out.stdout).get("data") or {}).get(key)
    return base64.b64decode(encoded).decode() if encoded else None


def port_open(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def cmd_run(args: argparse.Namespace) -> None:
    """Run one of the stack scripts (demo_trade.py, bots_check.py, ...) against an environment."""
    env = dict(os.environ)
    port = ENV_PORTS[args.env]
    env["GATEWAY"] = "http://localhost" if port == 80 else f"http://localhost:{port}"
    env["K8S_NAMESPACE"] = args.env
    # The environment's own secrets (sealed, random since Phase 6), not the Compose placeholders.
    for var, (secret, key) in SCRIPT_SECRETS.items():
        value = secret_value(args.env, secret, key)
        if value and var not in os.environ:
            env[var] = value
    forwards = []
    try:
        for var, (service, local) in FORWARDS.items():
            forwards.append(
                subprocess.Popen(  # noqa: S603 (fixed command)
                    kubectl("port-forward", "-n", args.env, f"svc/{service}", f"{local}:8000"),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            env[var] = f"http://localhost:{local}"
        deadline = time.monotonic() + 15
        while not all(port_open(local) for _, local in FORWARDS.values()):
            if time.monotonic() > deadline:
                sys.exit("port-forwards did not open")
            time.sleep(0.2)
        script = ROOT / "scripts" / args.script
        code = subprocess.run([sys.executable, str(script), *args.args], env=env, check=False)  # noqa: S603
        sys.exit(code.returncode)
    finally:
        for proc in forwards:
            proc.terminate()


def cmd_bots(args: argparse.Namespace) -> None:
    """Turn the 20 bots on or off (make k8s-bots / k8s-bots-stop), like make bots in Compose."""
    settings = {
        "BOTS_ENABLED": "false" if args.stop else "true",
        "BOT_RANDOM_SEED": args.seed,
        "BOT_ACTIVITY": args.activity,
    }
    # Tonight's choice, not a release: operator-settings (outside Git), then a restart to read it.
    set_operator_settings(args.env, settings)
    restart(args.env, "bot-traders")
    print("20 bots trading" if not args.stop else "bots stopped", f"in {args.env}")


def bots_enabled(env: str) -> bool:
    return operator_settings(env).get("BOTS_ENABLED") == "true"


def cmd_sync(args: argparse.Namespace) -> None:
    """Ask Argo CD to compare with Git now (it polls every 30 s anyway): make sync [env=] [s=]."""
    apps = [
        a
        for a in argo_applications(args.env)
        if not args.services
        or a["metadata"]["labels"].get("circuitbreaker.io/component") in args.services
    ]
    if not apps:
        which = f" for {', '.join(args.services)}" if args.services else ""
        sys.exit(f"Argo CD manages nothing in {args.env}{which}")
    for app in apps:
        run(
            kubectl(
                "annotate",
                "applications.argoproj.io",
                "-n",
                "argocd",
                app["metadata"]["name"],
                "argocd.argoproj.io/refresh=normal",
                "--overwrite",
            ),
            capture=True,
        )
    print(f"asked Argo CD to refresh {len(apps)} Application(s) in {args.env}")
    if not args.apply:
        return
    # Start a sync now, as `argocd app sync` does. Needed when automated sync will not try again:
    # it never retries a revision it already attempted, e.g. one cut short when the controller
    # restarted. Not after the SRE agent's rollback of a release from Git: that waits OutOfSync
    # for its revert pull request (ADR-0016).
    started = 0
    for app in apps:
        status = app.get("status", {})
        if status.get("sync", {}).get("status") != "OutOfSync" or app.get("operation"):
            continue
        policy = app["spec"].get("syncPolicy", {})
        operation = {
            "operation": {
                "initiatedBy": {"username": "make-sync"},
                "sync": {
                    "revision": status["sync"]["revision"],
                    "prune": policy.get("automated", {}).get("prune", False),
                    "syncOptions": policy.get("syncOptions", []),
                },
            }
        }
        run(
            kubectl(
                "patch",
                "applications.argoproj.io",
                "-n",
                "argocd",
                app["metadata"]["name"],
                "--type",
                "merge",
                "-p",
                json.dumps(operation),
            ),
            capture=True,
        )
        started += 1
        print(f"   sync started: {app['metadata']['name']} -> {status['sync']['revision'][:12]}")
    print(f"started {started} sync(s) in {args.env} (OutOfSync, no operation running)")


def cmd_argocd(_: argparse.Namespace) -> None:
    """Argo CD's web UI through a port-forward (no host port: a tool for operators, not phones)."""
    secret = run(
        kubectl("get", "secret", "argocd-initial-admin-secret", "-n", "argocd", "-o", "json"),
        capture=True,
        check=False,
    )
    if secret:
        import base64

        password = base64.b64decode(json.loads(secret)["data"]["password"]).decode()
        print(f"Argo CD: http://localhost:8443  user admin, password {password}  (Ctrl-C to stop)")
    subprocess.run(
        kubectl("port-forward", "-n", "argocd", "svc/argo-cd-argocd-server", "8443:80"), check=False
    )


def cmd_secret(args: argparse.Namespace) -> None:
    """One value of an environment's Secret (random since Phase 6: SealedSecrets, ADR-0019), e.g.
    the presenter's admin passcode (make k8s-passcode) or the bot secret for make k8s-test-phone."""
    value = secret_value(args.env, args.name, args.key)
    if not value:
        sys.exit(f"no Secret {args.name} (key {args.key}) in {args.env}")
    print(value)


# --------------------------------------------------------------------------- prepull (offline demo)

SEALED_SECRETS_VALUES = ROOT / "infra" / "platform" / "sealed-secrets.yaml"


def full_ref(image: str) -> str:
    """The name containerd stores: docker.io/library/ added to short Docker Hub names."""
    first = image.split("/", 1)[0]
    if "/" not in image or "." not in first and ":" not in first and first != "localhost":
        image = f"docker.io/{image}"
    if image.startswith("docker.io/") and image.count("/") == 1:
        image = image.replace("docker.io/", "docker.io/library/", 1)
    return image


def rendered_images(env: str) -> set[str]:
    """Every image the umbrella chart names for an environment, from Git (what Argo CD deploys)."""
    env_dir = ROOT / "deploy" / "envs" / env
    files = [
        env_dir / "global.yaml",
        *sorted(f for f in env_dir.glob("*.yaml") if f.name != "global.yaml"),
    ]
    args = [arg for f in files for arg in ("-f", str(f))]
    out = run(["helm", "template", RELEASE, str(CHART), "--namespace", env, *args], capture=True)
    return {
        m.group(1) for m in re.finditer(r"^\s+(?:- )?image: \"?([^\s\"]+)\"?$", out, re.MULTILINE)
    }


def wanted_images() -> tuple[set[str], list[str]]:
    """Images to keep on the node: what runs now, what Git names for both environments, and the
    Sealed Secrets controller (pinned by digest). Also the containers that pull on every start."""
    pods = json.loads(run(kubectl("get", "pods", "-A", "-o", "json"), capture=True))["items"]
    images: set[str] = set()
    always: list[str] = []
    for pod in pods:
        spec = pod["spec"]
        for c in spec.get("containers", []) + spec.get("initContainers", []):
            images.add(c["image"])
            if c.get("imagePullPolicy") == "Always":
                always.append(
                    f"{pod['metadata']['namespace']}/{pod['metadata']['name']}: {c['image']}"
                )
    for env in ("staging", "prod"):
        images |= rendered_images(env)
    sealed = re.search(
        r"^  registry: (\S+)\n  repository: (\S+)\n.*?^  tag: (\S+)",
        SEALED_SECRETS_VALUES.read_text(),
        re.MULTILINE | re.DOTALL,
    )
    if sealed:
        images.add("{}/{}:{}".format(*sealed.groups()))
    return {full_ref(i) for i in images}, always


def node_images() -> set[str]:
    """Names and digests the node's containerd holds."""
    out = json.loads(run(["docker", "exec", NODE, "crictl", "images", "-o", "json"], capture=True))
    held: set[str] = set()
    for img in out["images"]:
        held.update(img.get("repoTags") or [])
        held.update(img.get("repoDigests") or [])
    return held


def is_held(image: str, held: set[str]) -> bool:
    if "@" in image:  # pinned by digest: the digest is what counts
        name, digest = image.split("@", 1)
        return f"{name.rsplit(':', 1)[0] if ':' in name.split('/')[-1] else name}@{digest}" in held
    return image in held


def ghcr_login() -> str | None:
    """user:token from the cluster's sealed GHCR login (prod's, else staging's)."""
    for env in ("prod", "staging"):
        raw = secret_value(env, "ghcr-pull", ".dockerconfigjson")
        if raw:
            auth = json.loads(raw)["auths"]["ghcr.io"]["auth"]
            import base64

            return base64.b64decode(auth).decode()
    return None


def cmd_prepull(_: argparse.Namespace) -> None:
    """Every image the demo needs, on the node before the demo: with imagePullPolicy IfNotPresent a
    restarted or rescheduled pod then starts without the internet (the hotspot has none)."""
    if node_state() != "running":
        sys.exit("the cluster is not running: make k8s-up (or make platform) first")
    wanted, always = wanted_images()
    held = node_images()
    missing = sorted(i for i in wanted if not is_held(i, held))
    step(f"{len(wanted)} images: {len(wanted) - len(missing)} on the node, {len(missing)} to pull")
    login = ghcr_login() if any(i.startswith("ghcr.io/") for i in missing) else None
    failed = []
    for image in missing:
        cmd = ["docker", "exec", NODE, "crictl", "pull"]
        if image.startswith("ghcr.io/"):
            if not login:
                failed.append(f"{image} (no ghcr-pull Secret: make ghcr-token, commit, sync)")
                continue
            cmd += ["--creds", login]
        print(f"   pull {image}", flush=True)
        if not ok([*cmd, image]):
            failed.append(image)
    held = node_images()
    still = [i for i in wanted if not is_held(i, held)]
    for image in sorted(wanted):
        print(f"   {'ok  ' if image not in still else 'MISS'} {image}")
    for line in always:
        print(f"   WARN imagePullPolicy Always (pulls on every start, fails offline): {line}")
    absent = sorted(set(failed) | set(still))
    if absent:
        sys.exit(f"{len(absent)} image(s) not on the node: {', '.join(absent)}")
    print(f"\nall {len(wanted)} images are on the node; nothing needs the internet to start again")


# --------------------------------------------------------------------------- CI's platform check


def cmd_ci_secrets(args: argparse.Namespace) -> None:
    """CI's throwaway cluster (Terraform profile ci): the sealing key never leaves the laptop, so
    the SealedSecrets in Git cannot be decrypted there. Plain Secrets with the local placeholders
    instead, and the GHCR login from GHCR_USER / GHCR_TOKEN (the job's token, packages: read)."""
    import base64

    apply_secrets(args.env)
    user, token = os.environ.get("GHCR_USER"), os.environ.get("GHCR_TOKEN")
    if not (user and token):
        sys.exit("GHCR_USER and GHCR_TOKEN must be set")
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    config = {"auths": {"ghcr.io": {"username": user, "auth": auth}}}
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "ghcr-pull", "namespace": args.env},
        "type": "kubernetes.io/dockerconfigjson",
        "stringData": {".dockerconfigjson": json.dumps(config)},
    }
    run(kubectl("apply", "-f", "-"), input_text=json.dumps(secret), capture=True)
    print(f"{args.env}: placeholder Secrets and the GHCR login created (CI only)")


def cmd_wait_argo(args: argparse.Namespace) -> None:
    """Every Application of env Synced and Healthy, its last sync Succeeded (so its PostSync smoke
    test passed); then the smoke Jobs' results."""
    waited = wait_argo(args.env, timeout=args.timeout)
    failed = [
        a["metadata"]["name"]
        for a in argo_applications(args.env)
        if a.get("status", {}).get("operationState", {}).get("phase") != "Succeeded"
    ]
    jobs = json.loads(
        run(
            kubectl(
                "get",
                "jobs",
                "-n",
                args.env,
                "-l",
                "app.kubernetes.io/name=smoke-test",
                "-o",
                "json",
            ),
            capture=True,
        )  # fmt: skip
    )["items"]
    passed = [j["metadata"]["name"] for j in jobs if j.get("status", {}).get("succeeded")]
    print(f"Argo CD: {args.env} Synced and Healthy after {waited:.0f} s;"
          f" smoke Jobs passed: {len(passed)} of {len(jobs)}")  # fmt: skip
    if failed or len(passed) != len(jobs) or (args.smoke and not jobs):
        sys.exit(f"last sync not Succeeded: {', '.join(failed) or '-'}; smoke Jobs: {passed}")


def cmd_argo_teardown(args: argparse.Namespace) -> None:
    """Terraform's destroy, before the AppProject goes: delete Argo CD's ApplicationSets (so nothing
    re-creates Applications), then its Applications, and wait until they are gone. Each carries
    Argo CD's resources-finalizer (Argo CD deletes what it deployed, then lets the Application go),
    and Argo CD processes it only while Argo CD runs and the Application's project still exists:
    otherwise the Applications, and with them the argocd namespace, never go (CI, 2026-11-24)."""
    start = time.monotonic()
    for kind in ("applicationsets.argoproj.io", "applications.argoproj.io"):
        run(
            kubectl("delete", kind, "--all", "-n", "argocd", "--wait=false", "--ignore-not-found"),
            check=False,  # no Argo CD (its CRDs gone): nothing to delete
        )
    while apps := argo_applications():
        if time.monotonic() - start > args.timeout:
            names = ", ".join(a["metadata"]["name"] for a in apps)
            sys.exit(f"Argo CD: Applications still there after {args.timeout:.0f} s: {names}")
        time.sleep(5)
    print(f"Argo CD: its Applications and what they deployed are gone after"
          f" {time.monotonic() - start:.0f} s")  # fmt: skip


def cmd_charts(_: argparse.Namespace) -> None:
    cache_charts()
    print(f"charts in {CHART_CACHE.relative_to(ROOT)}: {', '.join(c for c, _, _ in CHARTS)}")


def cmd_registry(_: argparse.Namespace) -> None:
    ensure_registry()
    wait_api()
    connect_registry()


def cmd_reset(args: argparse.Namespace) -> None:
    if os.environ.get("CONFIRM") != "yes":
        answer = input(
            f"This DELETES the kind cluster '{CLUSTER}' and ALL its data. Type 'yes' to continue: "
        )
        if answer != "yes":
            sys.exit("Aborted.")
    run(["kind", "delete", "cluster", "--name", CLUSTER])
    # Terraform's state described that cluster; it no longer exists, so the next make platform
    # starts from nothing instead of trying to reach it.
    for name in ("terraform.tfstate", "terraform.tfstate.backup"):
        (TERRAFORM_DIR / name).unlink(missing_ok=True)
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
    set_url = sub.add_parser("set-url", help="change the URL in the join QR code, data kept")
    set_url.add_argument("url")
    set_url.add_argument("--env", choices=["prod", "staging"], default="prod")
    set_url.set_defaults(func=cmd_set_url)
    images = sub.add_parser("images")
    images.add_argument("services", nargs="*")
    images.set_defaults(func=cmd_images)
    sub.add_parser("reset").set_defaults(func=cmd_reset)
    sync = sub.add_parser("sync", help="ask Argo CD to look at Git now")
    sync.add_argument("--env", choices=["prod", "staging"], default="staging")
    sync.add_argument("services", nargs="*")
    sync.add_argument(
        "--apply", action="store_true", help="also start a sync of OutOfSync Applications now"
    )
    sync.set_defaults(func=cmd_sync)
    sub.add_parser("argocd").set_defaults(func=cmd_argocd)
    secret = sub.add_parser("secret", help="print one value of an environment's Secret")
    secret.add_argument("--env", choices=["prod", "staging"], default="prod")
    secret.add_argument("name")
    secret.add_argument("--key", default="value")
    secret.set_defaults(func=cmd_secret)
    sub.add_parser("charts").set_defaults(func=cmd_charts)
    ci_secrets = sub.add_parser("ci-secrets", help="CI only: placeholder Secrets + GHCR login")
    ci_secrets.add_argument("--env", choices=["prod", "staging"], default="staging")
    ci_secrets.set_defaults(func=cmd_ci_secrets)
    wait = sub.add_parser("wait-argo", help="wait until Argo CD synced env (and its smoke tests)")
    wait.add_argument("--env", choices=["prod", "staging"], default="staging")
    wait.add_argument("--timeout", type=float, default=900)
    wait.add_argument("--smoke", action="store_true", help="fail if no smoke Job ran")
    wait.set_defaults(func=cmd_wait_argo)
    teardown = sub.add_parser("argo-teardown", help="Terraform destroy: Argo CD deletes its apps")
    teardown.add_argument("--timeout", type=float, default=600)
    teardown.set_defaults(func=cmd_argo_teardown)
    sub.add_parser("prepull", help="put every image the demo needs on the node").set_defaults(
        func=cmd_prepull
    )
    sub.add_parser("registry").set_defaults(func=cmd_registry)
    run_ = sub.add_parser("run", help="run a scripts/ check against the cluster")
    run_.add_argument("--env", choices=["prod", "staging"], default="prod")
    run_.add_argument("script")
    run_.add_argument("args", nargs=argparse.REMAINDER)
    run_.set_defaults(func=cmd_run)
    bots = sub.add_parser("bots")
    bots.add_argument("--env", choices=["prod", "staging"], default="prod")
    bots.add_argument("--stop", action="store_true")
    bots.add_argument("--seed", default="")
    bots.add_argument("--activity", default="1.0")
    bots.set_defaults(func=cmd_bots)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
