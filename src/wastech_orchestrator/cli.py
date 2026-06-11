"""CLI entry point.

A skeleton for now: it parses the `run` and `watch` commands. The pipeline stages
are implemented incrementally per the roadmap (see orchestrator_final_plan.md §15).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wastech-orchestrator",
        description="Orchestrator for coding agents (Codex / Claude Code) on top of Git.",
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")

    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="run a single task from a file")
    run_cmd.add_argument("task_file", help="path to the task file (.md or .json)")

    sub.add_parser("watch", help="watch the tasks folder and run the tasks in it")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # TODO(stage-05): wire up the Orchestrator Core (see docs/rules/architecture.md).
    raise SystemExit(
        f"Command '{args.command}' is not implemented yet. "
        f"See the roadmap in orchestrator_final_plan.md §15."
    )


if __name__ == "__main__":
    sys.exit(main())
