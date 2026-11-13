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
    "cassandra-admin": {"password": ("CASSANDRA_ADMIN_PASSWORD", "cassandra")},
    "postgres-admin": {"password": ("POSTGRES_ADMIN_PASSWORD", "postgres")},
    "order-service-db": {"password": ("ORDER_SERVICE_DB_PASSWORD", "cb_dev_order_service")},
    "matching-engine-db": {"password": ("MATCHING_ENGINE_DB_PASSWORD", "cb_dev_matching_engine")},
    "market-data-service-db": {"password": ("MARKET_DATA_DB_PASSWORD", "cb_dev_market_data")},
    "sre-agent-db": {"password": ("SRE_AGENT_DB_PASSWORD", "cb_dev_sre_agent")},
    "portfolio-service-db": {"password": ("PORTFOLIO_DB_PASSWORD", "cb_dev_portfolio")},
}


# Third-party charts, pinned (ADR-0018): (release, chart, repo, version, namespace, values file).
PLATFORM = [
    ("traefik", "traefik", "oci://ghcr.io/traefik/helm", "42.3.1", "traefik", "traefik.yaml"),
    (
        "kube-prometheus-stack",
        "kube-prometheus-stack",
        "oci://ghcr.io/prometheus-community/charts",
        "92.22.0",
        "observability",
        "kube-prometheus-stack.yaml",
    ),
    (
        "loki",
        "loki",
        "oci://ghcr.io/grafana-community/helm-charts",
        "20.4.1",
        "observability",
        "loki.yaml",
    ),
    # Alloy is not published as an OCI artifact, and its repository's index is too large for a slow
    # connection: the release package itself, checked against the index's sha256 (CHART_DIGESTS).
    (
        "alloy",
        "alloy",
        "https://github.com/grafana/helm-charts/releases/download/alloy-1.15.0/alloy-1.15.0.tgz",
        "1.15.0",
        "observability",
        "alloy.yaml",
    ),
]
# Grafana's admin login (anonymous visitors may only look). Phase 6: Terraform creates it.
GRAFANA_PASSWORD = os.environ.get("GRAFANA_ADMIN_PASSWORD", "cb-dev-grafana")


CHART_CACHE = ROOT / "build" / "k8s" / "charts"
# sha256 of chart packages downloaded by URL (from the chart repository's index.yaml).
CHART_DIGESTS = {
    "alloy-1.15.0.tgz": "2ecc96c05004cc01a45ab23cb390194ad5259ec9d02b42bf148d4468d013aaa3",
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


def install_platform() -> None:
    ensure_namespace("observability")
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "grafana-admin", "namespace": "observability"},
        "type": "Opaque",
        "stringData": {"admin-user": "admin", "admin-password": GRAFANA_PASSWORD},
    }
    run(kubectl("apply", "-f", "-"), input_text=json.dumps(secret), capture=True)
    for release, chart, repo, version, namespace, values in PLATFORM:
        step(f"{release} (chart {chart} {version}, namespace {namespace})")
        run(
            [
                "helm",
                "upgrade",
                "--install",
                release,
                cached_chart(chart, repo, version),
                "--kube-context",
                CONTEXT,
                "--namespace",
                namespace,
                "--create-namespace",
                "-f",
                str(ROOT / "infra" / "platform" / values),
            ],
            capture=True,
        )
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


def public_base_url(env: str) -> str:
    """Where phones reach this environment: PUBLIC_BASE_URL, else localhost (the QR box warns)."""
    port = ENV_PORTS[env]
    default = "http://localhost" if port == 80 else f"http://localhost:{port}"
    return os.environ.get("PUBLIC_BASE_URL", default).rstrip("/")


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
    overrides: dict[str, dict[str, object]] = {
        "global": {"environment": env, "publicBaseUrl": public_base_url(env)},
        **{name: {"image": {"tag": tag}} for name, tag in tags.items()},
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
    if not args.skip_images:
        step("images")
        build_images(list(IMAGES))
    install_platform()
    deploy(args.env)
    step(f"waiting until every pod in {args.env} and the platform is ready")
    took = sum(wait_ready(ns) for ns in [*sorted({p[4] for p in PLATFORM}), args.env])
    url = public_base_url(args.env)
    print(f"\nall pods in {args.env} ready ({took:.0f} s).  {url}/   (kubectl context {CONTEXT})")


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
    deploy(args.env, {"bot-traders": {"env": settings}})
    wait_ready(args.env)
    print("20 bots trading" if not args.stop else "bots stopped", f"in {args.env}")


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
