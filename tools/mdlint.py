#!/usr/bin/env python3
"""Lint this repository's Markdown corpus with wastech-mdlint.

One entry point for the local shell and the pre-commit hook, so both run the same corpus with the
same rules rather than two hand-written command lines that drift. Two responsibilities justify a
script instead of a bare command:

* **Finding the linter.** Normally it is the pinned `@wastech-mdlint/cli` devDependency, so
  `npm ci` is the whole setup. Two fallbacks survive that for the people who develop the linter
  itself: an explicit env var, then a sibling checkout. Resolution is most-explicit-first and is
  reported when it fails, never guessed silently.
* **Covering both branch states.** The shared config describes the corpus that exists on every
  branch; the branch carrying the derived documentation adds a second, additive config for the
  rules that presuppose it. Running the shared config plus every overlay that is actually present
  makes the command correct on either branch with no branch-name logic anywhere.

Exit codes mirror the linter's own: ``0`` clean, ``1`` findings, ``2`` operational failure. A linter
that cannot be found is a skip with a note, so committing a typo fix never requires ``npm ci`` to
have been run — but not under ``CI``, where the same case is a hard failure, because an automated
caller that reports success for a gate it never ran is worse than a red one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE_CONFIG = "wastech-mdlint.config.json"

# Additive configs, each run as its own pass when the file is present. Presence IS the branch
# signal: the derived documentation and the config asserting it exist on the same branch, and the
# absence of that config everywhere else is what keeps it out of every merge.
OVERLAY_CONFIGS = ("wastech-mdlint.docs.config.json",)

HOME_ENV_VAR = "WASTECH_MDLINT_HOME"

# The linter is a workspace monorepo: this is the CLI package's built entry point inside a
# checkout of it — the layout of a source checkout, not of the published package, which is why it
# differs from the `node_modules` path below. `npm install` there writes no files here.
CLI_ENTRY = Path("packages") / "cli" / "dist" / "index.js"

# Findings must fail the build, so the threshold is pinned here rather than left to the linter's
# default — the default is the same today, and a gate should not move because a tool's default did.
FAIL_ON = "error"

# A per-run ceiling: the whole corpus lints in about two seconds, so anything approaching this is a
# hang rather than a slow run, and a hook or a CI step must not wait on it indefinitely.
RUN_TIMEOUT_SECONDS = 300


def repo_root() -> Path:
    """The repository checkout this script belongs to."""
    return Path(__file__).resolve().parents[1]


def note(message: str) -> None:
    """Write an operational note to stderr, keeping stdout free for the linter's own report."""
    sys.stderr.write(f"mdlint: {message}\n")


def find_cli(root: Path) -> Path | None:
    """Locate the linter's CLI entry point, or ``None`` when none is available.

    The installed dependency wins: it is the version this repository pins, so a stale sibling
    checkout can never quietly decide what the gate means. The env var and the sibling directory
    are the escape hatch for developing the linter against this corpus, in that order.
    """
    installed = root / "node_modules" / "@wastech-mdlint" / "cli" / "dist" / "index.js"
    if installed.is_file():
        return installed

    configured = os.environ.get(HOME_ENV_VAR)
    if configured:
        candidate = Path(configured).expanduser() / CLI_ENTRY
        if candidate.is_file():
            return candidate
        note(
            f"{HOME_ENV_VAR} is set to {Path(configured).expanduser().as_posix()} "
            f"but {CLI_ENTRY.as_posix()} is not built there — run `npm ci && npm run build` in it"
        )
        return None

    sibling = root.parent / "wastech-mdlint" / CLI_ENTRY
    if sibling.is_file():
        return sibling
    return None


def configs_to_run(root: Path) -> list[str]:
    """The configs to lint with: the shared one, then every overlay present in this checkout."""
    present = [BASE_CONFIG]
    present.extend(name for name in OVERLAY_CONFIGS if (root / name).is_file())
    return present


def run_pass(node: str, cli: Path, root: Path, config: str, extra_args: list[str]) -> int:
    """Lint the repository once with ``config`` and return the linter's exit code."""
    command = [
        node,
        str(cli),
        "lint",
        ".",
        "--config",
        config,
        "--fail-on",
        FAIL_ON,
        *extra_args,
    ]
    # An argument list, never a shell string: the config name and any pass-through flag reach the
    # process as separate argv entries, so nothing here is exposed to shell interpolation.
    completed = subprocess.run(
        command,
        cwd=root,
        timeout=RUN_TIMEOUT_SECONDS,
        check=False,
    )
    return completed.returncode


def main(argv: list[str]) -> int:
    """Run every applicable lint pass and return the worst exit code any of them produced."""
    root = repo_root()
    node = shutil.which("node")
    cli = find_cli(root) if node else None

    if node is None or cli is None:
        missing = "node is not on PATH" if node is None else "the linter is not installed"
        if os.environ.get("CI"):
            note(f"cannot run the Markdown gate: {missing}")
            return 2
        note(
            f"skipped ({missing}). Run `npm ci` to install the pinned linter, or point "
            f"{HOME_ENV_VAR} at a built wastech-mdlint checkout to run one you are developing."
        )
        return 0

    worst = 0
    for config in configs_to_run(root):
        worst = max(worst, run_pass(node, cli, root, config, argv))
    return worst


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
