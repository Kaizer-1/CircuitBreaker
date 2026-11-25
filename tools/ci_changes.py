#!/usr/bin/env python3
"""CI's first job: is this change documentation only?

Prints `code=false` (for $GITHUB_OUTPUT) only when every changed file is documentation: anything under docs/ or a
Markdown file (*.md) anywhere. CI then skips what documentation cannot affect: the image builds (and their
manifests), the integration tests and the end-to-end run. Everything else still runs, including the checks that read
documentation (the event catalog against the event schemas, gitleaks). When in doubt it says `code=true`: no base to
compare with (a new branch, a force push), a diff that fails, or no changed file at all. Deployment doc section 4.

    python3 tools/ci_changes.py <base> <head>     # the files changed from <base> to <head> (three-dot diff)
    python3 tools/ci_changes.py --self-test
"""

import subprocess
import sys

NO_COMMIT = "0" * 40  # github.event.before for a new branch


def is_documentation(path: str) -> bool:
    return path.startswith("docs/") or path.endswith(".md")


def code_changed(paths: list[str]) -> bool:
    return not paths or not all(is_documentation(p) for p in paths)


def changed_files(base: str, head: str) -> list[str] | None:
    """The files a pull request (or a push) changed; None if git cannot tell."""
    if not base or base == NO_COMMIT:
        return None
    done = subprocess.run(  # noqa: S603 (fixed command, commit ids from GitHub)
        ["git", "diff", "--name-only", f"{base}...{head}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        print(f"git diff failed: {done.stderr.strip()}", file=sys.stderr)
        return None
    return [line for line in done.stdout.splitlines() if line]


def self_test() -> None:
    cases = {
        ("README.md",): False,
        ("docs/07-roadmap.md", "docs/design/mockups/terminal.html"): False,
        ("services/order-service/README.md", "api/README.md"): False,
        ("README.md", "services/order-service/src/order_service/main.py"): True,
        (".github/workflows/ci.yml",): True,
        ("api/order-service.yaml",): True,  # a spec is code: services are tested against it
        ("deploy/envs/staging/web.yaml",): True,
        ("charts/circuitbreaker/values.yaml", "docs/adr/README.md"): True,
        (): True,  # nothing known: build everything
    }
    wrong = [
        f"{paths}: code={got}, expected {expected}"
        for paths, expected in cases.items()
        if (got := code_changed(list(paths))) is not expected
    ]
    if changed_files("", "HEAD") is not None or changed_files(NO_COMMIT, "HEAD") is not None:
        wrong.append("a missing base must mean unknown (everything runs)")
    if wrong:
        sys.exit("ci_changes self-test FAILED:\n  " + "\n  ".join(wrong))
    print(f"ci_changes: {len(cases) + 1} cases passed")


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        return 0
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    paths = changed_files(sys.argv[1], sys.argv[2])
    code = paths is None or code_changed(paths)
    what = "unknown" if paths is None else f"{len(paths)} file(s)"
    print(f"changed: {what}; documentation only: {not code}", file=sys.stderr)
    print(f"code={'true' if code else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
