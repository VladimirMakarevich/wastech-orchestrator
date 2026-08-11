#!/usr/bin/env python3
"""Stage a curated Markdown documentation tree for MkDocs."""

from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path

REPO_URL = "https://github.com/VladimirMakarevich/wastech-orchestrator"
STAGE_DIR = Path(".site-src")

# Every page published by the site, in `mkdocs.yml` nav order. The two lists are one decision split
# across two files, so `strict: true` turns any drift into a failed build: a nav entry with no
# staged file aborts the build outright, and a staged file with no nav entry publishes an orphan
# page nothing links to. `tests/test_stage_site_docs.py` asserts the two agree — keep them in step.
# Contributor-only docs (`docs/likec4/`, `docs/analysis/`, `docs/backlog/`) are deliberately absent:
# they are read in the repository, and `rewrite_links` sends links to them to GitHub.
PUBLIC_MARKDOWN = [
    Path("docs/index.md"),
    Path("docs/how-it-works.md"),
    Path("docs/operations.md"),
    Path("docs/cookbook.md"),
    Path("docs/how-to.md"),
    Path("docs/glossary.md"),
    Path("docs/configuration.md"),
    Path("docs/task-authoring.md"),
    Path("docs/flow-authoring.md"),
    Path("docs/telegram.md"),
    Path("docs/worc_architecture.md"),
]

PUBLIC_ASSETS = [
    Path("docs/assets"),
]

LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def public_sources(root: Path) -> set[Path]:
    return {(root / path).resolve() for path in PUBLIC_MARKDOWN}


def destination_for(source: Path, root: Path) -> Path:
    relative = source.relative_to(root)
    if relative.parts[0] == "docs":
        relative = Path(*relative.parts[1:])
    return STAGE_DIR / relative


def is_remote_or_anchor(target: str) -> bool:
    return target.startswith(("#", "http://", "https://", "mailto:"))


def split_target(target: str) -> tuple[str, str]:
    for separator in ("#", "?"):
        if separator in target:
            base, suffix = target.split(separator, 1)
            return base, separator + suffix
    return target, ""


def github_url(path: Path, ref: str, suffix: str) -> str:
    normalized = path.as_posix()
    return f"{REPO_URL}/blob/{ref}/{normalized}{suffix}"


def site_relative_url(source: Path, target_path: Path, suffix: str, root: Path) -> str:
    source_dest = destination_for(source, root)
    target_dest = destination_for(target_path, root)
    relative = os.path.relpath(target_dest, start=source_dest.parent)
    return Path(relative).as_posix() + suffix


def rewrite_links(markdown: str, source: Path, public: set[Path], root: Path, ref: str) -> str:
    def replace(match: re.Match[str]) -> str:
        label = match.group(1)
        target = match.group(2)
        if is_remote_or_anchor(target):
            return match.group(0)

        base, suffix = split_target(target)
        if not base:
            return match.group(0)

        target_path = (source.parent / base).resolve()
        if target_path in public:
            rewritten = site_relative_url(source, target_path, suffix, root)
        else:
            rewritten = github_url(target_path.relative_to(root), ref, suffix)
        return f"[{label}]({rewritten})"

    return LINK_RE.sub(replace, markdown)


def copy_asset_dir(source: Path, root: Path) -> None:
    if not source.exists():
        return
    target = destination_for(source, root)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def stage(ref: str) -> None:
    root = repo_root()
    stage_root = root / STAGE_DIR
    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_root.mkdir()

    public = public_sources(root)
    for source in sorted(public):
        target = root / destination_for(source, root)
        target.parent.mkdir(parents=True, exist_ok=True)
        markdown = source.read_text(encoding="utf-8")
        target.write_text(rewrite_links(markdown, source, public, root, ref), encoding="utf-8")

    for asset_dir in PUBLIC_ASSETS:
        copy_asset_dir(root / asset_dir, root)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ref",
        default="main",
        help="Git ref used for source links that point outside the published site.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stage(args.ref)


if __name__ == "__main__":
    main()
