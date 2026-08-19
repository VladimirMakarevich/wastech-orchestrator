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
    claude_config_home,
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


def _native_memory_glob(config_dir: Path) -> str:
    # Mirrors claude._native_memory_deny_tools: //-anchored, symlink-canonicalized, POSIX slashes.
    return "//" + config_dir.resolve().as_posix().lstrip("/") + "/**"


def test_native_memory_paths_denied_for_custom_config_dir(
    claude_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The spawned agent must not read/inject/leak Claude Code's native project memory, which
    # lives under the config dir OUTSIDE the target working tree. Write/Edit/Read are denied there,
    # honoring a custom CLAUDE_CONFIG_DIR. No denied_commands passed — the deny applies on its own,
    # by default (allow_native_memory off).
    config_dir = tmp_path / "isolated-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    argv = _argv(claude_config, make_request())
    disallowed = argv[argv.index("--disallowedTools") + 1]
    glob = _native_memory_glob(config_dir)
    assert f"Write({glob})" in disallowed
    assert f"Edit({glob})" in disallowed
    assert f"Read({glob})" in disallowed


def test_native_memory_deny_defaults_to_home_claude(
    claude_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Absent CLAUDE_CONFIG_DIR, the deny covers the ~/.claude default.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    argv = _argv(claude_config, make_request())
    disallowed = argv[argv.index("--disallowedTools") + 1]
    assert f"Write({_native_memory_glob(Path.home() / '.claude')})" in disallowed


def test_allow_native_memory_drops_the_deny_but_keeps_command_denies(
    claude_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # With the Claude-only opt-in on, the native-memory deny is
    # dropped so the agent may use its own auto-memory — but the command/read denies are untouched.
    config_dir = tmp_path / "isolated-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    opted_in = replace(claude_config, allow_native_memory=True)
    argv = _argv(opted_in, make_request(), denied=DENIED)
    disallowed = argv[argv.index("--disallowedTools") + 1]
    glob = _native_memory_glob(config_dir)
    assert f"Write({glob})" not in disallowed
    assert f"Edit({glob})" not in disallowed
    assert f"Read({glob})" not in disallowed
    # The publish blacklist is unaffected by the memory opt-in.
    assert "Bash(git commit:*)" in disallowed


def test_allow_native_memory_with_no_other_denies_omits_disallowed_flag(
    claude_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # With the opt-in on and no command/read denies passed, there is nothing left to deny, so the
    # flag is omitted entirely (rather than emitted empty).
    opted_in = replace(claude_config, allow_native_memory=True)
    argv = build_claude_argv(opted_in, make_request())
    assert "--disallowedTools" not in argv


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
        provider_homes=(Path("/home/me/.claude"),),
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
    # strict_isolation:false → the operator accepts unsandboxed Bash; no raise, no drop.
    argv = _argv(
        claude_config,
        make_request(permission_profile="workspace-write"),
        capability=SandboxCapability.LINUX_MISSING_DEPS,
        strict_isolation=False,
    )
    assert "Bash" in argv[argv.index("--tools") + 1]


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


def test_claude_config_home_left_to_memory_rule_not_re_denied_by_internal(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # ~/.claude is owned by the native-memory rule (gated by allow_native_memory); the internal
    # deny projection must exclude it so the opt-in is not broken.
    home = claude_config_home()
    argv = _argv(claude_config, make_request(), internal_deny=(Path("/repo/.worc"), home))
    disallowed = argv[argv.index("--disallowedTools") + 1]
    home_glob = "//" + home.as_posix().lstrip("/")
    assert f"Read({home_glob}/**)" in disallowed  # native-memory rule present
    assert f"Read({home_glob})" not in disallowed  # internal projection did NOT re-add the node


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
        provider_homes=(Path("/home/me/.claude"),),
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


def test_build_sandbox_settings_network_grant_allows_domains() -> None:
    deny = InternalDenyPolicy(
        control_home=Path("/repo/.worc"),
        private_home=Path("/repo/.worc"),
        env_file=None,
        provider_homes=(),
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
        provider_homes=(Path("/home/me/.codex"),),
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


def test_read_isolation_off_lifts_internal_reads_keeps_writes_and_blacklist(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # The private set becomes READABLE (no Read deny) but stays Write/Edit-denied; the public
    # denied_read_paths blacklist (.env/secrets) keeps its Read deny; command + write-guard denies
    # stay (write side).
    argv = _argv(
        claude_config,
        make_request(permission_profile="workspace-write", write_guard=_write_guard()),
        internal_deny=_INTERNAL_DENY,
        denied=DENIED,
        denied_read=(".env", "secrets/**"),
        read_isolation_off=True,
    )
    disallowed = argv[argv.index("--disallowedTools") + 1]
    assert "Write(//repo/.worc/**)" in disallowed and "Edit(//repo/.worc/**)" in disallowed
    assert "Read(//repo/.worc)" not in disallowed and "Read(//repo/.worc/**)" not in disallowed
    # Public blacklist read-deny stays (decision: keep the target-repo secret blacklist enforced).
    assert "Read(.env)" in disallowed and "Read(secrets/**)" in disallowed
    # Write side stays: command denies + write-guard Write/Edit.
    assert "Bash(git commit:*)" in disallowed
    assert "Write(//repo/.worc-io/**)" in disallowed


def test_read_isolation_off_lifts_the_native_memory_read_deny_but_keeps_the_write_deny(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Reading native Claude memory (~/.claude) is restored for native discovery; WRITING it is not.
    # Regression: this rule is the ONLY thing covering ~/.claude (the internal projection excludes
    # it), so skipping it wholesale left that store with zero deny rules on the shipped default and
    # agents wrote memory files into the operator's HOME — outside the clone, the frozen bundle, and
    # the redaction net.
    home_glob = "//" + claude_config_home().as_posix().lstrip("/")
    argv = _argv(claude_config, make_request(), denied=DENIED, read_isolation_off=True)
    disallowed = argv[argv.index("--disallowedTools") + 1]
    assert f"Read({home_glob}/**)" not in disallowed
    assert f"Write({home_glob}/**)" in disallowed
    assert f"Edit({home_glob}/**)" in disallowed


def test_read_isolation_off_with_allow_native_memory_drops_every_deny(
    claude_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # The opt-in is the only switch that lifts the write side, and it still does with the read hatch
    # open — the operator owns that risk, and the escape hatch must keep working.
    home_glob = "//" + claude_config_home().as_posix().lstrip("/")
    opted_in = replace(claude_config, allow_native_memory=True)
    argv = _argv(opted_in, make_request(), denied=DENIED, read_isolation_off=True)
    disallowed = argv[argv.index("--disallowedTools") + 1]
    for kind in ("Read", "Write", "Edit"):
        assert f"{kind}({home_glob}/**)" not in disallowed
    assert "Bash(git commit:*)" in disallowed  # the publish blacklist is untouched either way


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
        # path to publishing — that stays the orchestrator's alone.
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


def test_granted_read_only_shell_kept_on_native_windows_when_isolation_is_off() -> None:
    # strict_isolation: false is the operator saying they own the risk — the same arm the adapter
    # already takes for workspace-write on this host.
    plan = resolve_claude_tools(
        "read-only",
        SandboxCapability.NATIVE_WINDOWS,
        False,
        strict_isolation=False,
        git_evidence=True,
    )
    assert "Bash" in plan.tools
    assert "Bash(git log:*)" in plan.allow_patterns
    assert plan.needs_sandbox is False


def test_granted_read_only_shell_refuses_a_linux_host_missing_sandbox_deps() -> None:
    # The refusal used to be reachable only through workspace-write. Re-keying it on "the plan keeps
    # Bash" is what makes it fire here too: a shell that cannot be sandboxed is not run at all.
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
