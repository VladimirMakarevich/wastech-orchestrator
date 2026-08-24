"""Unit tests for the Claude Code command/prompt builders."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import ProviderConfig
from wastech_orchestrator.providers.base import AgentRunRequest, ErrorClass, ProviderError
from wastech_orchestrator.providers.claude import (
    SandboxCapability,
    build_claude_argv,
    build_context_footer,
    build_effective_prompt,
    build_sandbox_settings,
    map_permission,
    resolve_claude_tools,
)
from wastech_orchestrator.runtime_layout import InternalDenyPolicy, ProviderWriteGuardPolicy

DENIED = ("git commit", "git push", "gh pr create")


def _probe(capability: SandboxCapability) -> Callable[[], SandboxCapability]:
    return lambda: capability


def _argv(config: ProviderConfig, request: AgentRunRequest, **kwargs: object) -> list[str]:
    """Build the argv with an injected host so tests are deterministic on any CI host.

    Defaults to a sandbox-available host (macOS) so a plain workspace-write request builds cleanly;
    a test forcing another platform passes ``capability=...``.
    """
    capability = kwargs.get("capability", SandboxCapability.MACOS)
    assert isinstance(capability, SandboxCapability)
    return build_claude_argv(
        config,
        request,
        denied_commands=kwargs.get("denied", ()),  # type: ignore[arg-type]
        denied_read_paths=kwargs.get("denied_read", ()),  # type: ignore[arg-type]
        internal_deny_read_paths=kwargs.get("internal_deny", ()),  # type: ignore[arg-type]
        sandbox_probe=_probe(capability),
        strict_isolation=bool(kwargs.get("strict_isolation", True)),
        read_isolation_off=bool(kwargs.get("read_isolation_off", False)),
    )


def test_argv_is_claude_print_with_stream_json(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request())
    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv


def test_disables_project_setting_sources(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Security lockdown: no user/project/local setting sources are loaded, so project
    # settings / skills / plugins / hooks / MCP are never discovered. The empty value = load none.
    # (this also turns off native CLAUDE.md auto-load — the agent reads root files itself.)
    argv = _argv(claude_config, make_request())
    assert argv[argv.index("--setting-sources") + 1] == ""


def test_no_repository_instruction_injection(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Claude no longer injects repository instructions. --setting-sources "" stays (security:
    # no hooks/MCP/skills), so native CLAUDE.md auto-load is off too — the agent reads the repo's
    # root instruction files itself (role-prompt-directed), and they are write-denied for the run.
    argv = _argv(claude_config, make_request())
    assert "--append-system-prompt-file" not in argv
    assert "--append-system-prompt" not in argv


def test_workspace_write_maps_to_accept_edits(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(permission_profile="workspace-write"))
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_read_only_maps_to_dontask_with_readonly_allowlist(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # `dontAsk` (the documented headless read-only mode, replacing the legacy `default`
    # alias) so a clarification goes through the role's structured `human_input` field, not the
    # CLI's
    # plan-mode UX; Edit/Write/Bash absent from `--tools`/`--allowedTools` is the actual mutation
    # gate.
    argv = _argv(claude_config, make_request(permission_profile="read-only"))
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    for flag in ("--tools", "--allowedTools"):
        tools = argv[argv.index(flag) + 1]
        assert "Edit" not in tools and "Write" not in tools and "Bash" not in tools


def test_map_permission_rejects_full_access_and_unknown() -> None:
    with pytest.raises(ProviderError) as exc:
        map_permission("danger-full-access")
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR
    with pytest.raises(ProviderError):
        map_permission("anything-goes")


def test_network_access_off_by_default_no_web_tools(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    allowed = _argv(claude_config, make_request())[
        _argv(claude_config, make_request()).index("--allowedTools") + 1
    ]
    assert "WebFetch" not in allowed and "WebSearch" not in allowed


def test_network_access_allows_web_tools_when_granted(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(network_access=True))
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "WebFetch" in allowed and "WebSearch" in allowed
    # The filesystem permission mode is unchanged — network is the only thing added.
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_denied_commands_become_disallowed_tools(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(), denied=DENIED)
    disallowed = argv[argv.index("--disallowedTools") + 1]
    assert "Bash(git commit:*)" in disallowed
    assert "Bash(git push:*)" in disallowed
    assert "Bash(gh pr create:*)" in disallowed
    # Both shells. Keeping this list at all was justified by "otherwise there is no trace in
    # the log of an attempt" — and on Windows, where PowerShell is the shell, there was none.
    assert "PowerShell(git commit:*)" in disallowed
    assert "PowerShell(git push:*)" in disallowed
    assert "PowerShell(gh pr create:*)" in disallowed


def test_the_full_editor_set_is_denied_by_path_in_the_advanced_mode(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest], tmp_path: Path
) -> None:
    # The floor on the tool side is a list of names — and it was written from memory,
    # missing the fourth editor the pinned binary's own registry carries. `MultiEdit` in a path deny
    # is what keeps a `read-only` node in the mode from editing the working tree with a tool that
    # appeared in no list at all.
    guard = ProviderWriteGuardPolicy(
        exchange_root=tmp_path / ".worc-io",
        git_dir=tmp_path / ".git",
        git_common_dir=tmp_path / ".git",
        hooks_dir=tmp_path / ".git" / "hooks",
        tasks_dir=tmp_path / "tasks",
    )
    argv = _argv(
        claude_config,
        make_request(permission_profile="read-only", write_guard=guard),
        strict_isolation=False,
    )
    disallowed = argv[argv.index("--disallowedTools") + 1]
    gitdir = "//" + (tmp_path / ".git").resolve().as_posix().lstrip("/") + "/**"
    for kind in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        assert f"{kind}({gitdir})" in disallowed  # the path-scoped floor names every editor
        assert kind in disallowed.split(",")  # and the bare deny for a read-only node too


def test_the_shipped_default_keeps_the_historical_editor_pair(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest], tmp_path: Path
) -> None:
    # The asymmetry is deliberate: with `--tools` still emitted a tool outside the allowlist does
    # not exist for the session, so naming it in a deny is noise in every argv the default builds.
    guard = ProviderWriteGuardPolicy(
        exchange_root=tmp_path / ".worc-io",
        git_dir=tmp_path / ".git",
        git_common_dir=tmp_path / ".git",
        hooks_dir=tmp_path / ".git" / "hooks",
        tasks_dir=tmp_path / "tasks",
    )
    argv = _argv(claude_config, make_request(write_guard=guard))
    disallowed = argv[argv.index("--disallowedTools") + 1]
    gitdir = "//" + (tmp_path / ".git").resolve().as_posix().lstrip("/") + "/**"
    assert f"Write({gitdir})" in disallowed and f"Edit({gitdir})" in disallowed
    assert f"MultiEdit({gitdir})" not in disallowed
    assert f"NotebookEdit({gitdir})" not in disallowed


@pytest.mark.parametrize("read_isolation_off", [False, True])
def test_no_deny_of_any_kind_on_the_claude_config_home(
    claude_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    read_isolation_off: bool,
) -> None:
    # The Claude config home carries no deny of any kind — no Read, no Write/Edit, no glob at any
    # depth — at either read-isolation value. The home glob prefix covers every shape a deny rule
    # could take (`/**`, `/*`, `/*/*`).
    config_dir = tmp_path / "isolated-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    argv = _argv(
        claude_config, make_request(), denied=DENIED, read_isolation_off=read_isolation_off
    )
    disallowed = argv[argv.index("--disallowedTools") + 1]
    home_glob = "//" + config_dir.resolve().as_posix().lstrip("/")
    assert home_glob not in disallowed
    # The publish blacklist is untouched by the removal.
    assert "Bash(git commit:*)" in disallowed


def test_a_flow_node_cannot_escalate_the_permission_mode(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # `--permission-mode` is last-wins in this CLI and `extra_args` are appended verbatim,
    # so a flow node — the one surface an operator does not review — could hand itself the rank
    # directly under the forbidden bypass value. On a read-only node that turns "the tool exists
    # but asks" into "auto-approved" for everything not named in a deny.
    with pytest.raises(ProviderError) as excinfo:
        _argv(claude_config, make_request(extra_args=["--permission-mode", "auto"]))
    assert excinfo.value.error_class is ErrorClass.CONFIGURATION_ERROR
    assert "weaker than the requested profile" in str(excinfo.value)


def test_the_escalation_check_covers_the_inline_form_and_the_provider_config(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Both spellings and both sources: the check runs over the COMBINED set, so it cannot be reached
    # around by moving the token from the node to the provider block or by using `--flag=value`.
    with pytest.raises(ProviderError):
        _argv(claude_config, make_request(extra_args=["--permission-mode=auto"]))
    with pytest.raises(ProviderError):
        _argv(
            replace(claude_config, extra_args=("--permission-mode", "auto")),
            make_request(),
        )


def test_a_permission_mode_that_is_not_weaker_is_still_allowed(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # The operator keeps the legal override: only a WEAKER rank than the profile's own mode is
    # refused, so restating the mode a workspace-write node already has is
    # fine — and so is picking a stricter one.
    argv = _argv(claude_config, make_request(extra_args=["--permission-mode", "acceptEdits"]))
    assert argv[-2:] == ["--permission-mode", "acceptEdits"]
    argv = _argv(claude_config, make_request(extra_args=["--permission-mode", "dontAsk"]))
    assert argv[-2:] == ["--permission-mode", "dontAsk"]


def test_no_prompt_text_is_interpolated_into_argv(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    request = make_request(prompt="SECRET PROMPT CONTENT do-not-leak")
    argv = _argv(claude_config, request)
    assert all("SECRET PROMPT CONTENT" not in token for token in argv)


def test_model_flag_uses_request_override_then_config(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    assert "--model" not in _argv(claude_config, make_request())
    cfg = replace(claude_config, model="cfg-model")
    argv_cfg = _argv(cfg, make_request())
    assert argv_cfg[argv_cfg.index("--model") + 1] == "cfg-model"
    argv_req = _argv(cfg, make_request(model="req-model"))
    assert argv_req[argv_req.index("--model") + 1] == "req-model"


def test_max_turns_flag_only_when_configured(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    assert "--max-turns" not in _argv(claude_config, make_request())
    cfg = replace(claude_config, max_turns=5)
    argv = _argv(cfg, make_request())
    assert argv[argv.index("--max-turns") + 1] == "5"


def test_output_schema_is_passed_as_json_schema(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    }
    argv = _argv(claude_config, make_request(output_schema=schema))

    assert json.loads(argv[argv.index("--json-schema") + 1]) == schema


def test_forbidden_extra_args_are_rejected(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    bad = replace(claude_config, extra_args=("--dangerously-skip-permissions",))
    with pytest.raises(ProviderError) as exc:
        _argv(bad, make_request())
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR
    assert exc.value.is_fallback_eligible is False


def test_forbidden_extra_args_in_request_are_rejected(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    with pytest.raises(ProviderError) as exc:
        _argv(claude_config, make_request(extra_args=["--yolo"]))
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR


@pytest.mark.parametrize("profile", ["read-only", "workspace-write"])
@pytest.mark.parametrize(
    "extra_args",
    [
        ("--permission-mode", "bypassPermissions"),
        ("--permission-mode=bypassPermissions",),
    ],
)
def test_bypass_permission_mode_never_builds_argv(
    claude_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
    extra_args: tuple[str, ...],
    profile: str,
) -> None:
    # Operator extra_args are appended last, so the CLI's last-wins resolution would let this token
    # replace the mode the profile maps to. Nothing may select it, so the argv is never built at all
    # — in either spelling, at either access level.
    cfg = replace(claude_config, extra_args=extra_args)
    with pytest.raises(ProviderError) as exc:
        _argv(cfg, make_request(permission_profile=profile))
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    "reserved",
    [
        ("--add-dir", "../lib"),
        ("--tools", "Bash"),
        ("--settings", "/tmp/x.json"),
        ("--mcp-config", "/tmp/mcp.json"),
        ("--strict-mcp-config",),
        ("--agents", "{}"),
        ("--plugin-dir", "/tmp/p"),
        ("--chrome",),
        ("--worktree",),
        ("--append-system-prompt", "hi"),
        ("--resume", "sess"),
        ("--safe-mode",),
        ("--setting-sources=user",),
    ],
)
def test_reserved_extra_args_are_rejected(
    claude_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
    reserved: tuple[str, ...],
) -> None:
    # An authority-bearing Claude flag in extra_args (tools/settings/MCP/plugins/agents/
    # add-dir/Chrome/worktree/system-prompt/session) is hard-rejected regardless of strict_isolation
    # —
    # it would re-open a surface the adapter deliberately closed. Distinct from the gated
    # --permission-mode escalation, which still passes through.
    cfg = replace(claude_config, extra_args=reserved)
    with pytest.raises(ProviderError) as exc:
        _argv(cfg, make_request())
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR
    assert exc.value.is_fallback_eligible is False


def test_context_footer_lists_only_present_paths(
    make_request: Callable[..., AgentRunRequest],
) -> None:
    request = make_request(task_path="/logs/t/task.md", plan_path="/logs/t/plan.md")
    footer = build_context_footer(request)
    assert "/logs/t/task.md" in footer
    assert "/logs/t/plan.md" in footer
    assert "diff" not in footer  # diff_path is None


def test_context_footer_includes_human_input_path(
    make_request: Callable[..., AgentRunRequest],
) -> None:
    footer = build_context_footer(make_request(human_input_path="/logs/t/hitl/planning.json"))
    assert "human_input: /logs/t/hitl/planning.json" in footer


def test_context_footer_names_the_supervisor_packet_as_a_packet(
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # Its own label, not a reused plan/checks field: the footer, the rendered prompt, and the prompt
    # audit all read from this, so a reused field would make the packet undiagnosable.
    footer = build_context_footer(
        make_request(supervisor_packet_path="/io/t/supervisor/packet.json")
    )
    assert "packet: /io/t/supervisor/packet.json" in footer


def test_effective_prompt_appends_footer(make_request: Callable[..., AgentRunRequest]) -> None:
    request = make_request(prompt="Do the thing.", task_path="/logs/t/task.md")
    effective = build_effective_prompt(request)
    assert effective.startswith("Do the thing.")
    assert "/logs/t/task.md" in effective


def test_effective_prompt_prepends_security_preamble(
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # Order at the single neutral seam is preamble → prompt → footer.
    request = make_request(
        prompt="Do the thing.",
        task_path="/logs/t/task.md",
        security_preamble="[Orchestrator security contract]\nRule.",
    )
    effective = build_effective_prompt(request)
    assert effective.startswith("[Orchestrator security contract]\nRule.\n\nDo the thing.")
    assert effective.index("Rule.") < effective.index("Do the thing.") < effective.index("task.md")


def test_effective_prompt_without_preamble_is_byte_for_byte_today(
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # An unset preamble prepends nothing: prompt-only stays the bare prompt, and the footer path
    # is exactly ``prompt\n\nfooter`` (today's output).
    assert build_effective_prompt(make_request(prompt="P")) == "P"
    with_footer = build_effective_prompt(make_request(prompt="P", task_path="/t/task.md"))
    assert with_footer.startswith("P\n\n")
    assert not with_footer.startswith("[")  # no preamble prefix
    assert "task: /t/task.md" in with_footer


def test_reasoning_request_level_adds_effort_flag(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(reasoning="xhigh"))
    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_reasoning_config_level_adds_effort_flag(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(claude_config, reasoning="high")
    argv = _argv(cfg, make_request())
    assert argv[argv.index("--effort") + 1] == "high"


def test_reasoning_request_beats_config(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(claude_config, reasoning="low")
    argv = _argv(cfg, make_request(reasoning="max"))
    assert argv[argv.index("--effort") + 1] == "max"


def test_no_reasoning_means_no_effort_flag(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request())
    assert "--effort" not in argv


def test_session_id_adds_resume_flag(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(session_id="abc-123"))
    assert argv[argv.index("--resume") + 1] == "abc-123"


def test_no_session_id_means_no_resume_flag(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request())
    assert "--resume" not in argv


def test_effort_before_max_turns(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(claude_config, max_turns=10, reasoning="high")
    argv = _argv(cfg, make_request())
    assert argv.index("--effort") < argv.index("--max-turns")


# --- platform branching, tool policy, internal denies, sandbox settings --------------------------

_INTERNAL_DENY = (
    Path("/repo/.worc"),
    Path("/repo/.worc/.env"),
    Path("/repo/.worc/runs"),
)


def _write_guard() -> ProviderWriteGuardPolicy:
    return ProviderWriteGuardPolicy(
        exchange_root=Path("/repo/.worc-io"),
        git_dir=Path("/repo/.git"),
        git_common_dir=Path("/repo/.git"),
        hooks_dir=Path("/repo/.git/hooks"),
        tasks_dir=Path("/repo/tasks"),
    )


def _deny_policy() -> InternalDenyPolicy:
    return InternalDenyPolicy(
        control_home=Path("/repo/.worc"),
        private_home=Path("/repo/.worc"),
        env_file=Path("/repo/.worc/.env"),
    )


def test_strict_mcp_config_and_setting_sources_close_config_surface(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request())
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--mcp-config" not in argv  # strict mode + no config => zero MCP servers


def test_tools_is_a_hard_allowlist_mirroring_allowedtools(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(permission_profile="workspace-write"))
    tools = argv[argv.index("--tools") + 1]
    allowed = argv[argv.index("--allowedTools") + 1]
    assert tools == allowed == "Read,Glob,Grep,Edit,Write,Bash"


def test_workspace_write_macos_keeps_bash(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(
        claude_config,
        make_request(permission_profile="workspace-write"),
        capability=SandboxCapability.MACOS,
    )
    assert "Bash" in argv[argv.index("--tools") + 1]


def test_native_windows_workspace_write_omits_bash(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # No supported Bash sandbox on native Windows → strict workspace-write drops Bash (restricted
    # mode); Edit/Write remain, and there is no --settings sandbox file.
    argv = _argv(
        claude_config,
        make_request(permission_profile="workspace-write"),
        capability=SandboxCapability.NATIVE_WINDOWS,
    )
    tools = argv[argv.index("--tools") + 1]
    assert "Bash" not in tools
    assert "Edit" in tools and "Write" in tools
    assert "--settings" not in argv


def test_linux_missing_deps_workspace_write_raises_capability_unavailable(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    with pytest.raises(ProviderError) as exc:
        _argv(
            claude_config,
            make_request(permission_profile="workspace-write"),
            capability=SandboxCapability.LINUX_MISSING_DEPS,
        )
    assert exc.value.error_class is ErrorClass.CAPABILITY_UNAVAILABLE
    assert exc.value.is_fallback_eligible is False  # never an unconditional fallback


def test_linux_missing_deps_read_only_is_unaffected(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # A read-only node needs no Bash sandbox, so a sandbox-less host is fine everywhere.
    argv = _argv(
        claude_config,
        make_request(permission_profile="read-only"),
        capability=SandboxCapability.LINUX_MISSING_DEPS,
    )
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"


def test_non_strict_isolation_keeps_bash_on_unsandboxed_host(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # strict_isolation:false → the operator accepts unsandboxed Bash; no raise, no drop. The shell
    # is now read off --allowedTools: the advanced mode emits no existence gate at all.
    argv = _argv(
        claude_config,
        make_request(permission_profile="workspace-write"),
        capability=SandboxCapability.LINUX_MISSING_DEPS,
        strict_isolation=False,
    )
    assert "--tools" not in argv
    assert "Bash" in argv[argv.index("--allowedTools") + 1].split(",")


def test_internal_read_denies_seal_read_write_edit(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(), internal_deny=_INTERNAL_DENY)
    disallowed = argv[argv.index("--disallowedTools") + 1]
    for tool in ("Read", "Write", "Edit"):
        assert f"{tool}(//repo/.worc)" in disallowed  # the node itself (covers a secret file)
        assert f"{tool}(//repo/.worc/**)" in disallowed  # and its subtree


def test_write_guard_denies_write_edit_but_keeps_exchange_readable(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    req = make_request(permission_profile="workspace-write", write_guard=_write_guard())
    argv = _argv(claude_config, req)
    disallowed = argv[argv.index("--disallowedTools") + 1]
    for path in ("//repo/.worc-io", "//repo/.git", "//repo/.git/hooks", "//repo/tasks"):
        assert f"Write({path}/**)" in disallowed and f"Edit({path}/**)" in disallowed
    # The exchange stays READABLE — no Read deny on it (only Write/Edit).
    assert "Read(//repo/.worc-io/**)" not in disallowed
    assert "Read(//repo/.worc-io)" not in disallowed
    # Governance/instruction files are ordinary, editable content — never in the deny set.
    for name in ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md"):
        assert name not in disallowed


def test_settings_flag_present_only_when_path_given(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv_with = build_claude_argv(
        claude_config,
        make_request(permission_profile="workspace-write"),
        sandbox_settings_path="/repo/.worc/logs/x/settings.json",
        sandbox_probe=_probe(SandboxCapability.MACOS),
    )
    assert argv_with[argv_with.index("--settings") + 1] == "/repo/.worc/logs/x/settings.json"


def test_network_adds_web_tools_to_both_tools_and_allowed(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(claude_config, make_request(network_access=True))
    for flag in ("--tools", "--allowedTools"):
        value = argv[argv.index(flag) + 1]
        assert "WebFetch" in value and "WebSearch" in value


@pytest.mark.parametrize(
    "raw",
    [
        Path("/repo/dir with spaces/.worc"),
        Path("/repo/ünïçødé/.worc"),
        Path("/C:/Users/me/.worc"),  # windows-drive-shaped absolute
    ],
)
def test_internal_deny_globs_handle_spaces_unicode_and_drive_paths(
    claude_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
    raw: Path,
) -> None:
    argv = _argv(claude_config, make_request(), internal_deny=(raw,))
    disallowed = argv[argv.index("--disallowedTools") + 1]
    node = "//" + raw.as_posix().lstrip("/")
    assert f"Read({node}/**)" in disallowed
    # An unrelated parent directory must NOT be denied.
    assert "Read(//repo/**)" not in disallowed


def test_build_sandbox_settings_shape_is_hardened() -> None:
    deny = InternalDenyPolicy(
        control_home=Path("/repo/.worc"),
        private_home=Path("/repo/.worc"),
        env_file=Path("/repo/.worc/.env"),
        runs_home=Path("/repo/.worc/runs"),
    )
    settings = build_sandbox_settings(deny, _write_guard(), network_access=False)["sandbox"]
    assert settings["enabled"] is True
    assert settings["failIfUnavailable"] is True
    assert settings["allowUnsandboxedCommands"] is False
    assert settings["excludedCommands"] == []
    assert "enableWeakerNestedSandbox" not in settings
    # Plain absolute paths (NOT the // tool-glob grammar).
    assert "/repo/.worc" in settings["filesystem"]["denyRead"]
    assert not any(p.startswith("//") for p in settings["filesystem"]["denyRead"])
    # denyWrite = internal set + exchange/git/tasks; denyRead = internal set only.
    assert "/repo/.worc-io" in settings["filesystem"]["denyWrite"]
    assert "/repo/.worc-io" not in settings["filesystem"]["denyRead"]
    # Credentials: deny-only (never mask / tlsTerminate).
    assert settings["credentials"]["files"] == [{"path": "/repo/.worc/.env", "mode": "deny"}]
    assert "tlsTerminate" not in settings["network"]
    assert settings["network"]["allowedDomains"] == []


def test_the_advanced_mode_grants_write_outside_the_clone_and_keeps_every_carve_out() -> None:
    """``allowWrite`` on the volume root, with the floor listed one entry at a time.

    The carve-outs are asserted by NAME rather than by "the write guard is in there", because the
    short form of the floor ("`.git` and `.worc`") does not show all of them: the frozen ``runs``
    tree and the resolved env-file are exactly what an implementer reading that short form drops.
    The provider config homes are deliberately NOT carve-outs — nothing in the mode covers them.

    What this does NOT prove, and cannot: that the CLI ranks a ``denyWrite`` inside an
    ``allowWrite`` the way this file assumes. That needs a live probe on a real host, and the loud
    preflight line on floor 1 says it is not proven.
    """
    deny = InternalDenyPolicy(
        control_home=Path("/repo/.worc"),
        private_home=Path("/repo/.worc"),
        env_file=Path("/etc/worc/.env"),  # resolved outside the private home on purpose
        runs_home=Path("/repo/.worc/runs"),
    )
    settings = build_sandbox_settings(
        deny,
        _write_guard(),
        network_access=True,
        allow_write_root=Path("/"),
    )["sandbox"]
    assert settings["filesystem"]["allowWrite"] == ["/"]
    deny_write = set(settings["filesystem"]["denyWrite"])
    assert {
        "/repo/.worc",  # the control plane
        "/etc/worc/.env",  # the orchestrator's own secrets, wherever they resolved
        "/repo/.worc/runs",  # the frozen bundles, denied by name rather than by location
        "/repo/.worc-io",  # the curated exchange
        "/repo/.git",  # gitdir and, for a linked worktree, the shared common dir
        "/repo/.git/hooks",
        "/repo/tasks",  # the committed lifecycle tree
    } <= deny_write
    # And the provider config homes are NOT in it — no deny covers them in the mode.
    assert not any(p.endswith((".claude", ".codex")) for p in deny_write)


def test_no_allow_write_key_appears_outside_the_advanced_mode() -> None:
    # The shipped default's file, key for key. A grant leaking in unconditionally is the one mistake
    # in this phase that would remove the floor everywhere at once, so this pins the exact key set
    # rather than the absence of one name.
    settings = build_sandbox_settings(_deny_policy(), _write_guard(), network_access=False)
    assert set(settings["sandbox"]["filesystem"]) == {"denyRead", "denyWrite"}


def test_sandbox_settings_carry_no_deny_on_the_claude_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The settings-file surface: the OS sandbox policy carries no denyRead and no denyWrite on the
    # Claude config home — including in the advanced mode, whose volume-wide allowWrite is not
    # carved back out for it.
    config_dir = tmp_path / "isolated-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    for kwargs in ({}, {"allow_write_root": Path("/")}):
        settings = build_sandbox_settings(
            _deny_policy(), _write_guard(), network_access=False, **kwargs
        )["sandbox"]
        home = str(config_dir.resolve())
        assert home not in settings["filesystem"].get("denyRead", [])
        assert home not in settings["filesystem"]["denyWrite"]


def test_build_sandbox_settings_network_grant_allows_domains() -> None:
    deny = InternalDenyPolicy(
        control_home=Path("/repo/.worc"),
        private_home=Path("/repo/.worc"),
        env_file=None,
    )
    settings = build_sandbox_settings(deny, None, network_access=True)["sandbox"]
    assert settings["network"]["allowedDomains"] == ["*"]
    assert "credentials" not in settings  # no env-file → no credentials block


# --- read-isolation escape hatch (security.disable_read_isolation) --------------------


def _read_isolation_off_deny() -> InternalDenyPolicy:
    return InternalDenyPolicy(
        control_home=Path("/repo/.worc"),
        private_home=Path("/repo/.worc"),
        env_file=Path("/repo/.worc/.env"),
        runs_home=Path("/repo/.worc/runs"),
    )


def test_read_isolation_off_uses_project_setting_sources_and_drops_strict_mcp(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Native project discovery restored — --setting-sources project (not "") and no
    # --strict-mcp-config, so CLAUDE.md + project settings/hooks/MCP/skills load. The
    # injection stays retired regardless.
    argv = _argv(claude_config, make_request(), read_isolation_off=True)
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" not in argv
    assert "--append-system-prompt-file" not in argv


@pytest.mark.parametrize("read_isolation_off", [False, True])
def test_the_private_set_is_read_denied_at_either_read_isolation_setting(
    claude_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
    read_isolation_off: bool,
) -> None:
    """The private set keeps its Read deny even with read-isolation OFF.

    Losing it would put the resolved env-file — which is in that set — within reach of the plain
    ``Read`` tool on the shipped default, and the rule that withholds those names from the child
    environment is justified by exactly that read being denied. Parameterized over both settings
    because the point is that the verdict does not depend on the hatch: read-isolation restores
    native *discovery*, never the orchestrator's own secrets.
    """
    argv = _argv(
        claude_config,
        make_request(permission_profile="workspace-write", write_guard=_write_guard()),
        internal_deny=_INTERNAL_DENY,
        denied=DENIED,
        denied_read=(".env", "secrets/**"),
        read_isolation_off=read_isolation_off,
    )
    disallowed = argv[argv.index("--disallowedTools") + 1]
    assert "Read(//repo/.worc)" in disallowed and "Read(//repo/.worc/**)" in disallowed
    assert "Read(//repo/.worc/.env)" in disallowed
    assert "Write(//repo/.worc/**)" in disallowed and "Edit(//repo/.worc/**)" in disallowed
    # Public blacklist read-deny stays (decision: keep the target-repo secret blacklist enforced).
    assert "Read(.env)" in disallowed and "Read(secrets/**)" in disallowed
    # Write side stays: command denies + write-guard Write/Edit.
    assert "Bash(git commit:*)" in disallowed
    assert "Write(//repo/.worc-io/**)" in disallowed


def test_read_isolation_off_sandbox_lifts_denyread_keeps_denywrite() -> None:
    settings = build_sandbox_settings(
        _read_isolation_off_deny(), _write_guard(), network_access=False, read_isolation_off=True
    )["sandbox"]
    assert settings["filesystem"]["denyRead"] == []  # read side lifted
    # Write side intact: internal set + exchange/git/tasks still write-denied.
    assert "/repo/.worc" in settings["filesystem"]["denyWrite"]
    assert "/repo/.worc-io" in settings["filesystem"]["denyWrite"]
    # The env-file credential deny is a targeted secret protection kept regardless.
    assert settings["credentials"]["files"] == [{"path": "/repo/.worc/.env", "mode": "deny"}]


def test_read_isolation_default_argv_is_byte_identical(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Regression: at defaults (read_isolation_off unset == False) the argv is unchanged.
    req = make_request(permission_profile="workspace-write", write_guard=_write_guard())
    default = _argv(claude_config, req, internal_deny=_INTERNAL_DENY, denied=DENIED)
    explicit_off = _argv(
        claude_config, req, internal_deny=_INTERNAL_DENY, denied=DENIED, read_isolation_off=False
    )
    assert default == explicit_off
    assert default[default.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in default
    assert "Read(//repo/.worc/**)" in default[default.index("--disallowedTools") + 1]


def test_resolve_claude_tools_read_only_is_platform_independent() -> None:
    for cap in SandboxCapability:
        plan = resolve_claude_tools("read-only", cap, False)
        assert plan.mode == "dontAsk"
        assert plan.tools == ("Read", "Glob", "Grep")
        assert plan.needs_sandbox is False


# --- read-only git evidence: the grant, the platform arm, and the argv split -----------

_SANDBOX_HOSTS = (SandboxCapability.MACOS, SandboxCapability.LINUX_AVAILABLE)


def test_read_only_grant_adds_a_shell_scoped_to_the_git_verbs() -> None:
    # The whole point of the grant: an audit node gets a shell it can inspect history with. Bash
    # enters the existence gate; every verb it may run is a read-only one, expressed as a pattern.
    for cap in _SANDBOX_HOSTS:
        plan = resolve_claude_tools("read-only", cap, False, git_evidence=True)
        assert plan.mode == "dontAsk"
        assert plan.tools == ("Read", "Glob", "Grep", "Bash")
        assert plan.needs_sandbox is True  # a shell on a sandbox host is always sandboxed
        assert "Bash(git log:*)" in plan.allow_patterns
        assert "Bash(git show:*)" in plan.allow_patterns
        # No mutating verb is reachable through the allowlist, so the grant cannot become a second
        # path to publishing — that stays the orchestrator's.
        assert not any(
            f"Bash(git {verb}:*)" in plan.allow_patterns
            for verb in ("commit", "push", "add", "checkout", "reset", "clean")
        )


def test_read_only_without_the_grant_is_unchanged_everywhere() -> None:
    # The declaration is the only thing that adds reach: an undeclared node resolves exactly as it
    # does today on every host, sandbox or not.
    for cap in SandboxCapability:
        plan = resolve_claude_tools("read-only", cap, False, git_evidence=False)
        assert plan.tools == ("Read", "Glob", "Grep")
        assert plan.allow_patterns == ()
        assert plan.needs_sandbox is False


def test_grant_is_inert_on_workspace_write() -> None:
    # A workspace-write node already has an unscoped shell. The grant must not narrow it — that
    # would be a restriction wearing the name of a capability — so the plan is untouched.
    for cap in _SANDBOX_HOSTS:
        granted = resolve_claude_tools("workspace-write", cap, False, git_evidence=True)
        plain = resolve_claude_tools("workspace-write", cap, False)
        assert granted == plain
        assert granted.allow_patterns == ()
        assert "Bash" in granted.tools


def test_granted_read_only_shell_drops_on_native_windows_under_strict() -> None:
    # Native Windows has no supported Bash sandbox. The feature degrades to today's read-only node
    # (the role prompt's capability-conditional wording then applies) rather than quietly becoming
    # an unsandboxed shell — the same choice the adapter already makes for workspace-write.
    plan = resolve_claude_tools(
        "read-only", SandboxCapability.NATIVE_WINDOWS, False, git_evidence=True
    )
    assert plan.tools == ("Read", "Glob", "Grep")
    assert plan.allow_patterns == ()  # patterns for a tool that no longer exists would be noise
    assert plan.needs_sandbox is False


def test_read_only_shell_kept_on_native_windows_when_isolation_is_off() -> None:
    # strict_isolation: false is the operator saying they own the risk — the same arm the adapter
    # already takes for workspace-write on this host. The shell no longer depends on the grant, and
    # is no longer scoped by it: in the advanced mode the verb list is not applied at all, so the
    # names go out bare even for a node that did declare git_evidence.
    plan = resolve_claude_tools(
        "read-only",
        SandboxCapability.NATIVE_WINDOWS,
        False,
        strict_isolation=False,
        git_evidence=True,
    )
    assert "Bash" in plan.tools
    assert plan.allow_patterns == ()
    assert plan.needs_sandbox is False


def test_granted_read_only_shell_refuses_a_linux_host_missing_sandbox_deps() -> None:
    # Keyed on "the plan keeps Bash" rather than on the profile, so it fires for a granted
    # read-only shell too: a shell that cannot be sandboxed is not run at all.
    with pytest.raises(ProviderError) as excinfo:
        resolve_claude_tools(
            "read-only", SandboxCapability.LINUX_MISSING_DEPS, False, git_evidence=True
        )
    assert excinfo.value.error_class is ErrorClass.CAPABILITY_UNAVAILABLE
    assert "bubblewrap+socat" in str(excinfo.value)
    # ...and under strict_isolation: false the operator keeps it, unsandboxed.
    plan = resolve_claude_tools(
        "read-only",
        SandboxCapability.LINUX_MISSING_DEPS,
        False,
        strict_isolation=False,
        git_evidence=True,
    )
    assert "Bash" in plan.tools
    assert plan.needs_sandbox is False


def test_workspace_write_platform_behavior_is_unchanged_by_the_re_keying() -> None:
    # Bash is in the workspace-write baseline, so "profile == workspace-write" and "the plan keeps
    # Bash" select the same arm. Pinned so the re-keying cannot regress the existing profile.
    assert resolve_claude_tools("workspace-write", SandboxCapability.MACOS, False).needs_sandbox
    assert resolve_claude_tools(
        "workspace-write", SandboxCapability.LINUX_AVAILABLE, False
    ).needs_sandbox
    dropped = resolve_claude_tools("workspace-write", SandboxCapability.NATIVE_WINDOWS, False)
    assert "Bash" not in dropped.tools
    assert dropped.tools == ("Read", "Glob", "Grep", "Edit", "Write")
    with pytest.raises(ProviderError):
        resolve_claude_tools("workspace-write", SandboxCapability.LINUX_MISSING_DEPS, False)


def test_argv_puts_names_in_tools_and_patterns_in_allowed_tools(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(
        claude_config,
        make_request(permission_profile="read-only", git_evidence=True),
        denied=DENIED,
    )
    tools = argv[argv.index("--tools") + 1].split(",")
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    # --tools is the hard existence gate and takes bare names only; a pattern there would be read
    # as a tool that does not exist.
    assert tools == ["Read", "Glob", "Grep", "Bash"]
    assert not any("(" in name for name in tools)
    # --allowedTools carries the patterns, and a bare `Bash` must NOT be in it: a bare name
    # auto-approves every invocation of that tool and overrides its own narrower pattern, which
    # would hand back exactly the unrestricted shell the patterns exist to prevent.
    assert "Bash" not in allowed
    assert "Bash(git log:*)" in allowed
    assert allowed[:3] == ["Read", "Glob", "Grep"]  # unscoped tools keep their bare names
    # The denied-commands floor is untouched and still beats any allow.
    disallowed = argv[argv.index("--disallowedTools") + 1]
    assert "Bash(git commit:*)" in disallowed
    assert "Bash(git push:*)" in disallowed


def test_argv_is_byte_identical_for_a_node_that_does_not_declare_the_grant(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # The regression the split has to satisfy: with no patterns, --tools and --allowedTools carry
    # the same joined string they always have. Checked on both profiles.
    for profile in ("read-only", "workspace-write"):
        req = make_request(permission_profile=profile, write_guard=_write_guard())
        argv = _argv(claude_config, req, internal_deny=_INTERNAL_DENY, denied=DENIED)
        assert argv[argv.index("--tools") + 1] == argv[argv.index("--allowedTools") + 1]


def test_granted_read_only_shell_write_denies_the_whole_clone(tmp_path: Path) -> None:
    # What keeps such a node read-only is the sandbox, not the verb list: the clone root is
    # write-denied, so a command the allowlist somehow let through still cannot change the repo.
    settings = build_sandbox_settings(
        _deny_policy(),
        None,  # a read-only attempt carries no write_guard
        network_access=False,
        deny_write_root=Path("/repo"),
    )["sandbox"]
    assert "/repo" in settings["filesystem"]["denyWrite"]
    assert settings["allowUnsandboxedCommands"] is False


def test_sandbox_settings_unchanged_when_no_deny_write_root_is_passed() -> None:
    policy = _deny_policy()
    assert build_sandbox_settings(
        policy, _write_guard(), network_access=False
    ) == build_sandbox_settings(policy, _write_guard(), network_access=False, deny_write_root=None)


# --- Advanced mode: the tool-existence gate is gone ----------------------------------------------


def test_advanced_mode_emits_no_existence_gate_and_gives_every_node_a_shell(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # The whole point of the inversion: --tools stops being emitted, so a tool this codebase has
    # never heard of exists for the session. read-only is the case that changes most — it had no
    # shell at all, and now carries both of them, bare (every invocation auto-approves; a headless
    # run has nobody to answer a prompt).
    for profile in ("read-only", "workspace-write"):
        argv = _argv(
            claude_config, make_request(permission_profile=profile), strict_isolation=False
        )
        assert "--tools" not in argv, profile
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        assert "Bash" in allowed and "PowerShell" in allowed, profile
        # Bare names only — the git-evidence verb scoping is not applied in this mode.
        assert not any("(" in name for name in allowed), profile
        # ...and each name once: workspace-write already carries Bash, and a duplicate is noise in
        # the one artifact somebody reads during an incident.
        assert len(allowed) == len(set(allowed)), profile
        # dontAsk would auto-deny everything not on that list, which would leave a read-only node
        # exactly as tool-bound as before the gate was removed.
        assert argv[argv.index("--permission-mode") + 1] == "acceptEdits", profile


def test_advanced_mode_floor_survives_every_other_relaxation(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # The floor is the path-scoped write denies on gitdir/common-dir/hooks/tasks — the one part of
    # the deny list a shell cannot walk around, because the CLI's own editing tools never pass
    # through the OS sandbox. It has to hold at EVERY combination of the other relaxations, since
    # each of them is an independent switch an operator can flip.
    for read_isolation_off in (False, True):
        for profile in ("read-only", "workspace-write"):
            req = make_request(permission_profile=profile, write_guard=_write_guard())
            argv = _argv(
                claude_config,
                req,
                internal_deny=_INTERNAL_DENY,
                strict_isolation=False,
                read_isolation_off=read_isolation_off,
            )
            case = (read_isolation_off, profile)
            disallowed = argv[argv.index("--disallowedTools") + 1]
            for path in ("//repo/.git", "//repo/.git/hooks", "//repo/tasks", "//repo/.worc"):
                for kind in ("Write", "Edit", "NotebookEdit"):
                    assert f"{kind}({path}/**)" in disallowed, (case, path, kind)
            assert "EnterWorktree" in disallowed.split(","), case


def test_advanced_mode_keeps_a_read_only_node_from_writing(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # A shell is not a licence to write. With the existence gate gone, the write tools exist unless
    # they are named, so read-only now denies them by bare name; workspace-write must not, or the
    # profile would stop meaning anything.
    read_only = _argv(
        claude_config, make_request(permission_profile="read-only"), strict_isolation=False
    )
    denied = read_only[read_only.index("--disallowedTools") + 1].split(",")
    assert {"Write", "Edit", "NotebookEdit"} <= set(denied)
    writer = _argv(
        claude_config, make_request(permission_profile="workspace-write"), strict_isolation=False
    )
    assert "Write" not in writer[writer.index("--disallowedTools") + 1].split(",")


def test_the_advanced_mode_is_online_for_every_node_whatever_the_flow_granted(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    """The mode opens the network for every node, and that is three surfaces.

    For one phase these two names were DENIED here for a node whose flow granted none — the tool
    list holding the network axis shut while the phase that opens it deliberately was still ahead.
    That deny is gone: the mode is online, so they are auto-approved instead. Worth keeping next to
    the sandbox file's own assertion (see the write/network settings test) because these are
    different boundaries: ``WebFetch``/``WebSearch`` never pass through the OS sandbox at all, so
    ``allowedDomains`` says nothing about them and this list is all there is.
    """
    for granted in (False, True):
        request = make_request(network_access=granted)
        argv = _argv(claude_config, request, strict_isolation=False)
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        assert {"WebFetch", "WebSearch"} <= set(allowed), granted
        assert "WebFetch" not in denied and "WebSearch" not in denied, granted


def test_outside_the_mode_the_web_tools_still_follow_the_flow_grant(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # The counterweight: on the shipped default the existence gate is what withholds them, and a
    # node with no grant has neither the tools nor a reason to name them in the denies.
    offline = _argv(claude_config, make_request(network_access=False))
    assert "WebFetch" not in offline[offline.index("--tools") + 1].split(",")
    online = _argv(claude_config, make_request(network_access=True))
    assert "WebFetch" in online[online.index("--tools") + 1].split(",")


def test_advanced_mode_keeps_the_shell_on_a_host_that_cannot_sandbox_it(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Owner decision: a host with no OS sandbox is a loud line, not a refusal. Both classes are
    # injected rather than probed, so this holds on any CI host. What must NOT happen is a
    # CAPABILITY_UNAVAILABLE — that would stop a configuration which runs today.
    for capability in (SandboxCapability.NATIVE_WINDOWS, SandboxCapability.LINUX_MISSING_DEPS):
        plan = resolve_claude_tools(
            "read-only", capability, False, strict_isolation=False, git_evidence=False
        )
        assert "Bash" in plan.tools, capability
        # No sandbox file is written for such an attempt — there is nothing to write it against.
        assert plan.needs_sandbox is False, capability
        argv = _argv(
            claude_config,
            make_request(permission_profile="read-only"),
            capability=capability,
            strict_isolation=False,
        )
        assert "--settings" not in argv, capability


def test_sandbox_file_states_the_headless_auto_approval_instead_of_inheriting_it(
    claude_config: ProviderConfig,
) -> None:
    # Relying on the vendor default is a bet: were it ever false, every sandboxed command would ask
    # permission and a headless node would burn its turns on prompts nobody can answer.
    for read_isolation_off in (False, True):
        settings = build_sandbox_settings(
            _deny_policy(),
            _write_guard(),
            network_access=False,
            read_isolation_off=read_isolation_off,
        )["sandbox"]
        assert settings["autoAllowBashIfSandboxed"] is True
        assert settings["allowUnsandboxedCommands"] is False


def test_the_shipped_default_keeps_the_tool_flags_and_deny_membership_it_always_had(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # The inversion is reachable only through strict_isolation: false. This pins the three things
    # the shipped default is allowed to rely on, and nothing wider: both tool flags carry the same
    # joined string, no editor beyond the historical pair enters the path denies (a tool outside
    # --tools does not exist there, so naming it would be noise), and no friction name is emitted.
    # It is deliberately NOT a golden argv — no assertion here says the whole list is unchanged.
    for profile in ("read-only", "workspace-write"):
        req = make_request(permission_profile=profile, write_guard=_write_guard())
        argv = _argv(claude_config, req, internal_deny=_INTERNAL_DENY, denied=DENIED)
        assert argv[argv.index("--tools") + 1] == argv[argv.index("--allowedTools") + 1]
        disallowed = argv[argv.index("--disallowedTools") + 1]
        assert "NotebookEdit" not in disallowed, profile
        for name in ("EnterWorktree", "AskUserQuestion", "CronCreate", "RemoteTrigger"):
            assert name not in disallowed.split(","), profile
