"""Точка входа CLI.

Пока скелет: разбирает команды `run` и `watch`. Реализация стадий пайплайна
добавляется по дорожной карте (см. orchestrator_final_plan.md §15).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wastech-orchestrator",
        description="Оркестратор кодинг-агентов (Codex / Claude Code) поверх Git.",
    )
    parser.add_argument("--config", default="config.yaml", help="путь к config.yaml")

    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="выполнить одну задачу из файла")
    run_cmd.add_argument("task_file", help="путь к файлу задачи (.md или .json)")

    sub.add_parser("watch", help="следить за папкой задач и выполнять их")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # TODO(stage-05): подключить Orchestrator Core (см. docs/rules/architecture.md).
    raise SystemExit(
        f"Команда '{args.command}' ещё не реализована. "
        f"См. дорожную карту в orchestrator_final_plan.md §15."
    )


if __name__ == "__main__":
    sys.exit(main())
