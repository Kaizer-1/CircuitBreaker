#!/usr/bin/env python3
"""CI's half of GitOps (ADR-0014): which images to build, and the staging tag bumps.

    plan --service S [--env staging]     does S need a new image? (prints key=value for CI)
    bump --env E --from DIR [--push]     one commit per service built: its image in deploy/envs/E/

Build once: an image is built only when its folder changed, and tagged with the 12-character SHA of
the commit it was built from. "Changed" compares git trees: the folder at the commit the
environment runs now (its tag) against HEAD. Identical trees are identical build inputs (the build
context is the folder), so an unchanged service keeps its tag, is neither rebuilt nor redeployed,
and the image staging tested is the one prod gets (make promote copies the tag; nothing is rebuilt
for prod).

A service's file holds its registry and tag together, so a service moves to GHCR in the same commit
that gives it a GHCR tag.

Standard library only (runs on the CI runner and the laptop).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from k8s import IMAGES  # image name -> build context (the 8 services + web)

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = "ghcr.io/kaizer-1"
IMAGE_BLOCK = re.compile(r"^  image:\n(?:    .*\n)*", re.MULTILINE)


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip() if result.returncode == 0 else ""


def values_file(env: str, service: str) -> Path:
    return ROOT / "deploy" / "envs" / env / f"{service}.yaml"


def current_image(env: str, service: str) -> dict[str, str]:
    """The registry and tag in the service's file ("" when absent: the chart's default)."""
    match = IMAGE_BLOCK.search(values_file(env, service).read_text())
    image: dict[str, str] = {}
    for line in match.group(0).splitlines()[1:] if match else []:
        key, _, value = line.strip().partition(":")
        image[key] = value.strip().strip('"')
    return image


def with_image(text: str, service: str, registry: str, tag: str) -> str:
    block = f'  image:\n    registry: {registry}\n    tag: "{tag}"\n'
    if IMAGE_BLOCK.search(text):
        return IMAGE_BLOCK.sub(lambda _: block, text, count=1)
    if f"\n{service}:\n" not in f"\n{text}":
        sys.exit(f"no top-level `{service}:` key to put the image under")
    return re.sub(
        rf"^{re.escape(service)}:\n", lambda m: m.group(0) + block, text, count=1, flags=re.M
    )


# --------------------------------------------------------------------------- plan


def plan(env: str, service: str) -> tuple[bool, str]:
    path = IMAGES[service]
    image = current_image(env, service)
    tag = image.get("tag", "")
    if image.get("registry") != REGISTRY:
        return True, f"{env} runs it from {image.get('registry') or 'the local registry'}"
    commit = git("rev-parse", "--verify", "-q", f"{tag}^{{commit}}", check=False)
    if not commit:
        return True, f"{env}'s tag {tag} is not a commit in this history"
    before = git("rev-parse", f"{commit}:{path}", check=False)
    now = git("rev-parse", f"HEAD:{path}")
    if before != now:
        return True, f"{path} changed since {tag}"
    return False, f"{path} unchanged since {tag}: keep it"


def cmd_plan(args: argparse.Namespace) -> None:
    build, reason = plan(args.env, args.service)
    image = current_image(args.env, args.service)
    print(f"image={REGISTRY}/circuitbreaker/{args.service}")
    print(f"build={'true' if build else 'false'}")
    print(f"tag={git('rev-parse', '--short=12', 'HEAD') if build else image.get('tag', '')}")
    print(f"reason={reason}")


# --------------------------------------------------------------------------- bump


def cmd_bump(args: argparse.Namespace) -> None:
    """DIR holds one <service>.json per image CI built and pushed: {"service", "tag"}."""
    built = sorted(
        (json.loads(p.read_text()) for p in Path(args.from_dir).glob("*.json")),
        key=lambda b: b["service"],
    )
    commits = 0
    for b in built:
        service, tag = b["service"], b["tag"]
        if service not in IMAGES or not re.fullmatch(r"[0-9a-f]{12}", tag):
            sys.exit(f"refusing a bad build record: {b}")
        target = values_file(args.env, service)
        old = target.read_text()
        new = with_image(old, service, REGISTRY, tag)
        if new == old:
            print(f"   {service:<20} already {tag}")
            continue
        target.write_text(new)
        git("add", str(target))
        source = b.get("commit", tag)
        git(
            "commit",
            "-q",
            "-m",
            f"chore({args.env}): {service} {tag}",
            "-m",
            f"Built and scanned by CI from {source}; Argo CD deploys it (ADR-0014).",
        )
        commits += 1
        print(f"   {service:<20} -> {tag}")
    print(f"{commits} commit(s) for {args.env}")
    if commits and args.push:
        # Another run may have pushed meanwhile: rebase onto it (bumps touch one file each).
        for attempt in range(1, 4):
            if (
                subprocess.run(
                    ["git", "-C", str(ROOT), "push", "-q", "origin", "HEAD:main"],  # noqa: S603, S607
                    check=False,
                ).returncode
                == 0
            ):
                print("pushed")
                return
            print(f"push refused (attempt {attempt}): rebasing onto origin/main")
            git("pull", "-q", "--rebase", "origin", "main")
        sys.exit("could not push the tag bumps")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--service", choices=sorted(IMAGES), required=True)
    p.add_argument("--env", choices=["staging", "prod"], default="staging")
    p.set_defaults(func=cmd_plan)
    b = sub.add_parser("bump")
    b.add_argument("--env", choices=["staging", "prod"], default="staging")
    b.add_argument("--from", dest="from_dir", required=True)
    b.add_argument("--push", action="store_true", help="push the commits to main (CI)")
    b.set_defaults(func=cmd_bump)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
