"""Opt-in host smoke for the real Codex permission-profile boundary.

Set ``WORC_RUN_CODEX_POLICY_SMOKE=1`` on a Windows, macOS, Linux, or WSL host with a supported
Codex CLI.  The default suite remains hermetic and never launches a real provider.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from wastech_orchestrator.providers.codex_policy import (
    permission_config_values,
    render_exec_policy,
)

_OPT_IN = os.environ.get("WORC_RUN_CODEX_POLICY_SMOKE") == "1"


@pytest.mark.skipif(not _OPT_IN, reason="set WORC_RUN_CODEX_POLICY_SMOKE=1")
def test_real_codex_denies_direct_and_interpreter_reads(tmp_path: Path) -> None:
    version = subprocess.run(
        ["codex", "--version"], capture_output=True, text=True, check=True, shell=False
    ).stdout
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    assert match is not None and tuple(map(int, match.groups())) >= (0, 144, 4)

    (tmp_path / ".env").write_text("TOKEN=direct-secret", encoding="utf-8")
    (tmp_path / "secrets" / "nested").mkdir(parents=True)
    (tmp_path / "secrets" / "nested" / "token.txt").write_text("indirect-secret", encoding="utf-8")
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "private.pem").write_text("custom-secret", encoding="utf-8")
    (tmp_path / "allowed.txt").write_text("allowed", encoding="utf-8")

    values = permission_config_values(
        sandbox="workspace-write",
        network_access=False,
        denied_read_paths=(".env", "secrets/**", "custom/*.pem"),
    )
    base = ["codex", "sandbox", "--cd", os.fspath(tmp_path)]
    for value in values:
        base += ["--config", value]

    assert _run_read(base, "allowed.txt").returncode == 0
    for denied in (".env", "secrets/nested/token.txt", "custom/private.pem"):
        assert _run_read(base, denied).returncode != 0

    direct = (
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "type", ".env"]
        if os.name == "nt"
        else ["/bin/cat", ".env"]
    )
    assert subprocess.run([*base, *direct], check=False, shell=False).returncode != 0


@pytest.mark.skipif(not _OPT_IN, reason="set WORC_RUN_CODEX_POLICY_SMOKE=1")
def test_real_execpolicy_denies_wrapped_commands_but_allows_git_inspection(
    tmp_path: Path,
) -> None:
    rules = tmp_path / "deny.rules"
    rules.write_text(render_exec_policy(("git commit", "custom-tool deploy")), encoding="utf-8")

    denied = [["git", "commit"], ["custom-tool", "deploy"]]
    resolved_git = shutil.which("git")
    if resolved_git is not None:
        denied.append([resolved_git, "commit"])
    if os.name == "nt":
        denied.append([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "git", "commit"])
    else:
        denied.append(["/bin/sh", "-c", shlex.join(["git", "commit"])])

    for command in denied:
        assert _policy_decision(rules, command) == "forbidden"
    assert _policy_decision(rules, ["git", "status"]) != "forbidden"


def _run_read(base: list[str], path: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            *base,
            sys.executable,
            "-c",
            "from pathlib import Path; Path(__import__('sys').argv[1]).read_bytes()",
            path,
        ],
        capture_output=True,
        check=False,
        shell=False,
    )


def _policy_decision(rules: Path, command: list[str]) -> str | None:
    result = subprocess.run(
        ["codex", "execpolicy", "check", "--rules", os.fspath(rules), *command],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    for line in result.stdout.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            decision = parsed.get("decision")
            return decision if isinstance(decision, str) else None
    return None
