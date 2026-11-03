#!/usr/bin/env python3
"""Wait until every container of the given Compose profiles is ready.

`docker compose up --wait` treats the one-shot init jobs (which exit 0 by design) as
failures, so the Makefile uses this instead. A container is ready when it is healthy, or
running without a healthcheck, or (for the `*-init` jobs) exited with code 0. Any other
exit, or a timeout, fails with a list of what is not ready.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

TIMEOUT_SECONDS = 420
POLL_SECONDS = 3


def containers(profiles: list[str]) -> list[dict[str, object]]:
    cmd = ["docker", "compose"]
    for p in profiles:
        cmd += ["--profile", p]
    cmd += ["ps", "-a", "--format", "json"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    if not out:
        return []
    if out.startswith("["):
        return list(json.loads(out))
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def verdict(c: dict[str, object]) -> tuple[str, str]:
    """Return ("ready" | "waiting" | "failed", detail)."""
    service = str(c.get("Service", ""))
    state = str(c.get("State", ""))
    health = str(c.get("Health", ""))
    exit_code = c.get("ExitCode", 0)
    if state == "exited":
        if service.endswith("-init") and exit_code == 0:
            return "ready", "completed"
        return "failed", f"exited with code {exit_code}"
    if state == "running":
        if health == "healthy" or health == "":
            return "ready", "healthy" if health else "running"
        if health == "unhealthy":
            return "waiting", "unhealthy (retrying)"
        return "waiting", health or state
    return "waiting", state


def main() -> int:
    profiles = sys.argv[1:] or ["infra"]
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last = ""
    while True:
        status = {str(c.get("Service")): verdict(c) for c in containers(profiles)}
        failed = {k: v[1] for k, v in status.items() if v[0] == "failed"}
        waiting = {k: v[1] for k, v in status.items() if v[0] == "waiting"}
        if failed:
            print(
                "FAILED: " + ", ".join(f"{k} ({v})" for k, v in failed.items()),
                file=sys.stderr,
            )
            return 1
        if status and not waiting:
            print(f"all {len(status)} containers ready")
            return 0
        line = "waiting for: " + ", ".join(f"{k} ({v})" for k, v in sorted(waiting.items()))
        if line != last:
            print(line, flush=True)
            last = line
        if time.monotonic() > deadline:
            print(f"TIMEOUT after {TIMEOUT_SECONDS}s. {line}", file=sys.stderr)
            return 1
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
