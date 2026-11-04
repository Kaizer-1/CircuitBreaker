#!/usr/bin/env python3
"""Fail if the files that must be identical in every service have drifted apart.

The services share no runtime library on purpose (docs/05-coding-standards.md, "Duplication over coupling").
Instead the shared plumbing is copied from services/order-service by tools/scaffold.py, and this check makes
sure the copies stay equal. After renaming the package and service name, every copy of a managed file must
match the others byte for byte.

    python3 tools/check_template_drift.py          # check (make lint, CI)
    python3 tools/check_template_drift.py --sync   # copy the template's managed files to every service
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES_DIR = ROOT / "services"

# Relative to a service folder. `{pkg}` is the service's Python package.
MANAGED = [
    "Dockerfile",
    ".dockerignore",
    ".python-version",
    "src/{pkg}/runtime/*.py",
    "tests/__init__.py",
    "tests/unit/__init__.py",
    "tests/contract/__init__.py",
    "tests/integration/__init__.py",
    "tests/unit/test_runtime.py",
    "tests/unit/test_stubs.py",
    "tests/contract/test_api_spec.py",
]


def normalised(path: Path, name: str, pkg: str) -> str:
    return path.read_text().replace(pkg, "<PKG>").replace(name, "<SERVICE>")


TEMPLATE = "order-service"


def sync() -> int:
    """Copy every managed file from the template service into the others, renamed for each."""
    template_dir = SERVICES_DIR / TEMPLATE
    template_pkg = TEMPLATE.replace("-", "_")
    written = 0
    for service_dir in sorted(p for p in SERVICES_DIR.iterdir() if (p / "pyproject.toml").exists()):
        name = service_dir.name
        if name == TEMPLATE:
            continue
        pkg = name.replace("-", "_")
        for pattern in MANAGED:
            for source in sorted(template_dir.glob(pattern.format(pkg=template_pkg))):
                rel = source.relative_to(template_dir).as_posix().replace(template_pkg, pkg)
                text = source.read_text().replace(template_pkg, pkg).replace(TEMPLATE, name)
                target = service_dir / rel
                if not target.exists() and not rel.startswith("src/"):
                    continue  # only runtime/ must exist everywhere (the gateway has no test_stubs.py)
                if not target.exists() or target.read_text() != text:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text)
                    written += 1
                    print(f"synced {name}/{rel}")
    print(f"{written} file(s) copied from {TEMPLATE}")
    return 0


def main() -> int:
    if "--sync" in sys.argv[1:]:
        return sync()
    copies: dict[str, dict[str, str]] = defaultdict(
        dict
    )  # managed path -> {service: normalised text}
    for service_dir in sorted(p for p in SERVICES_DIR.iterdir() if (p / "pyproject.toml").exists()):
        name = service_dir.name
        pkg = name.replace("-", "_")
        for pattern in MANAGED:
            for file in sorted(service_dir.glob(pattern.format(pkg=pkg))):
                rel = file.relative_to(service_dir).as_posix().replace(pkg, "<PKG>")
                copies[rel][name] = normalised(file, name, pkg)

    problems = 0
    for rel, by_service in sorted(copies.items()):
        reference_service, reference = next(iter(by_service.items()))
        for service, text in by_service.items():
            if text != reference:
                problems += 1
                print(f"DRIFT {rel}: {service} differs from {reference_service}")
    # A runtime file that exists in one service must exist in all of them.
    services = {p.name for p in SERVICES_DIR.iterdir() if (p / "pyproject.toml").exists()}
    for rel, by_service in sorted(copies.items()):
        if rel.startswith("src/<PKG>/runtime/") and set(by_service) != services:
            problems += 1
            print(f"MISSING {rel}: absent from {sorted(services - set(by_service))}")

    if problems:
        print(
            f"\n{problems} problem(s). Fix the odd one out, or change services/order-service and re-copy."
        )
        return 1
    print(f"OK: {len(copies)} managed files identical across {len(services)} services")
    return 0


if __name__ == "__main__":
    sys.exit(main())
