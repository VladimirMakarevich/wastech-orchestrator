"""Unit tests for the Codex command/prompt builders."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import ProviderConfig
from wastech_orchestrator.providers.base import AgentRunRequest, ErrorClass, ProviderError
from wastech_orchestrator.providers.codex import (
    build_codex_argv,
    build_context_footer,
    build_effective_prompt,
)
from wastech_orchestrator.providers.codex_profile import PROFILE_NAME
from wastech_orchestrator.runtime_layout import InternalDenyPolicy, ProviderWriteGuardPolicy

LAST_MSG = "/logs/task/stages/planning/1-codex/last-message.txt"


def _argv(
    config: ProviderConfig,
    request: AgentRunRequest,
    *,
    schema: str | None = None,
    deny_policy: InternalDenyPolicy | None = None,
    denied_read_paths: Sequence[str] = (),
    strict_isolation: bool = True,
    read_isolation_off: bool = False,
) -> list[str]:
    return build_codex_argv(
        config,
        request,
        output_schema_path=schema,
        last_message_path=LAST_MSG,
        deny_policy=deny_policy,
        strict_isolation=strict_isolation,
        denied_read_paths=denied_read_paths,
        read_isolation_off=read_isolation_off,
    )


def _config_values(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, token in enumerate(argv[:-1]) if token in ("-c", "--config")]


def _profile_arg(argv: list[str]) -> str:
    matches = [v for v in _config_values(argv) if v.startswith(f"permissions.{PROFILE_NAME}=")]
    assert len(matches) == 1, f"expected one permission-profile -c value, got {matches}"
    return matches[0]


def _fs_rule(path: str, grant: str) -> str:
    """The rendered ``"<key>" = "<grant>"`` a POSIX fixture *path* produces in the inline profile.

    Two normalizations stand between the fixture literal and the rendered TOML, and both are
    platform-dependent: the generator maps the path through the ``to_native=str`` seam (so
    ``/clone`` becomes ``\\clone`` on native Windows) and the renderer escapes each backslash for a
    TOML basic string. Asserting the raw POSIX literal would only ever match on POSIX.
    """
    return f'"{str(Path(path)).replace(chr(92), chr(92) * 2)}" = "{grant}"'


def _assert_reasoning_config(argv: list[str], value: str) -> None:
    assert f'model_reasoning_effort="{value}"' in _config_values(argv)


def test_leaves_project_doc_discovery_enabled(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Codex's native AGENTS.md project-doc discovery is intentionally left ENABLED — the agent
    # reads the repo's root instruction files itself; no ``project_doc_max_bytes`` override is set.
    argv = _argv(codex_config, make_request())
    assert not any(c.startswith("project_doc_max_bytes") for c in _config_values(argv))
    assert "--reasoning-effort" not in argv


def test_argv_is_codex_exec_reading_from_stdin(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    request = make_request(working_directory="/clone")
    argv = _argv(codex_config, request)
    assert argv[:4] == ["codex", "--ask-for-approval", "never", "exec"]
    assert argv[-1] == "-"  # prompt comes from stdin
    assert "--cd" in argv and argv[argv.index("--cd") + 1] == "/clone"
    assert argv.index("--ask-for-approval") < argv.index("exec")
    assert "--json" in argv
    assert argv[argv.index("--output-last-message") + 1] == LAST_MSG
    # Isolation is a generated permission profile, NOT the legacy --sandbox mode flag.
    assert "--sandbox" not in argv
    assert f'default_permissions="{PROFILE_NAME}"' in _config_values(argv)


def test_profile_selected_and_user_config_ignored(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # The generated profile is injected as ONE inline-table -c value and selected as
    # default_permissions; the operator's base config.toml is ignored (auth still uses CODEX_HOME).
    argv = _argv(codex_config, make_request(working_directory="/clone"))
    profile = _profile_arg(argv)
    assert '"extends" = ":workspace"' in profile
    assert _fs_rule("/clone", "write") in profile
    assert "--ignore-user-config" in argv


def test_project_marked_untrusted(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request(working_directory="/clone"))
    assert 'projects."/clone".trust_level="untrusted"' in _config_values(argv)


def test_tool_surfaces_disabled(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request())
    disabled = {argv[i + 1] for i, tok in enumerate(argv[:-1]) if tok == "--disable"}
    assert {"hooks", "multi_agent", "computer_use", "plugins"} <= disabled


def test_the_extra_browser_surfaces_and_the_memory_store_are_disabled_too(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Ам4-11, owner decision of 2026-08-20 on a live `codex features list` inventory: the deny is
    # extended only where an ENABLED flag is a distinct surface that executes something or reaches
    # data. An external browser and full CDP access reach the operator's own browser session, the
    # in-app browser is a third name for the same class, and `memories` persists task content
    # outside this orchestrator's redaction net — none of the four passes through the profiled
    # shell, which is what the sandbox covers.
    argv = _argv(codex_config, make_request())
    disabled = {argv[i + 1] for i, tok in enumerate(argv[:-1]) if tok == "--disable"}
    assert {
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "in_app_browser",
        "memories",
    } <= disabled
    # And the names deliberately left enabled: disabling the profiled shell itself would remove the
    # agent's ability to work, and the rest are either sub-surfaces of an already-denied flag or
    # ship disabled anyway.
    assert (
        not {
            "unified_exec",
            "plugin_sharing",
            "remote_plugin",
            "enable_mcp_apps",
            "standalone_web_search",
        }
        & disabled
    )


def test_deny_policy_and_write_guard_projected_into_profile(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    deny = InternalDenyPolicy(
        control_home=Path("/clone/.worc"),
        private_home=Path("/clone/.worc"),
        env_file=None,
        provider_homes=(Path("/home/op/.codex"),),
    )
    wg = ProviderWriteGuardPolicy(
        exchange_root=Path("/clone/.worc-io"),
        git_dir=Path("/clone/.git"),
        git_common_dir=Path("/clone/.git"),
        hooks_dir=Path("/clone/.git/hooks"),
        tasks_dir=Path("/clone/tasks"),
    )
    argv = _argv(
        codex_config,
        make_request(working_directory="/clone", write_guard=wg),
        deny_policy=deny,
        denied_read_paths=(".env", "secrets/**"),
    )
    profile = _profile_arg(argv)
    assert _fs_rule("/clone/.worc", "deny") in profile  # private/control home denied
    assert _fs_rule("/home/op/.codex", "deny") in profile  # provider auth home denied
    assert _fs_rule("/clone/.env", "deny") in profile  # denied_read_paths projected
    assert _fs_rule("/clone/secrets", "deny") in profile
    assert _fs_rule("/clone/.worc-io", "read") in profile  # exchange readable, write-denied
    assert _fs_rule("/clone/tasks", "read") in profile
    # Governance/instruction files are editable content — never projected as a deny.
    for name in ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md"):
        assert name not in profile


def test_no_prompt_text_is_interpolated_into_argv(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    request = make_request(prompt="SECRET PROMPT CONTENT do-not-leak")
    argv = _argv(codex_config, request)
    assert all("SECRET PROMPT CONTENT" not in token for token in argv)


def test_no_legacy_sandbox_network_override(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # The old workspace-write network override is gone; network lives in the profile, and it follows
    # the attempt's effective grant rather than being pinned off.
    for networked in (True, False):
        argv = _argv(codex_config, make_request(network_access=networked))
        assert "sandbox_workspace_write.network_access=true" not in _config_values(argv)
        enabled = "true" if networked else "false"
        assert f'"network" = {{ "enabled" = {enabled} }}' in _profile_arg(argv)


def test_web_search_disabled_when_offline(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # An offline node must also deny the host web_search tool (backend-side, outside the profile
    # network policy) so network_access=false is truly offline.
    argv = _argv(codex_config, make_request())
    assert 'web_search="disabled"' in _config_values(argv)


def test_web_search_not_disabled_when_network_granted(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    """An online node keeps web_search — and its profile is online too, on the same flag.

    Before this the two halves disagreed: ``web_search`` followed the grant while the profile pinned
    ``network.enabled = false``, so a node the flow put online had a web tool and an offline shell.
    The pair is one decision now (ТA.8.1). The combination is legal for a ``read-only`` node;
    ``workspace-write`` + network is refused by the flow validator outside the advanced mode, which
    is why that pairing is only ever built in the mode's own tests.
    """
    argv = _argv(codex_config, make_request(network_access=True))
    assert 'web_search="disabled"' not in _config_values(argv)
    assert '"network" = { "enabled" = true }' in _profile_arg(argv)


def test_the_advanced_mode_is_online_for_a_node_that_was_granted_no_network(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    """ТA.8.1/ТA.8.4: in the mode BOTH surfaces open, whatever the flow granted.

    Half a network is the failure mode worth its own test, because the two are enforced in different
    places: the profile's sandbox network is what a shell (``restore``, ``npm ci``) needs, while
    ``web_search`` runs on the backend, outside that profile entirely. One effective flag drives
    both, so neither can open without the other.
    """
    argv = _argv(codex_config, make_request(network_access=False), strict_isolation=False)
    assert '"network" = { "enabled" = true }' in _profile_arg(argv)
    assert 'web_search="disabled"' not in _config_values(argv)


def test_outside_the_mode_an_offline_node_is_offline_on_both_surfaces(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # The counterweight, and the shipped default: nothing about the network moved for a node with no
    # grant. Both halves in one place, so a later edit cannot open one of them quietly.
    argv = _argv(codex_config, make_request(network_access=False))
    assert '"network" = { "enabled" = false }' in _profile_arg(argv)
    assert 'web_search="disabled"' in _config_values(argv)


def test_output_schema_flag_only_when_requested(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    no_schema = _argv(codex_config, make_request())
    assert "--output-schema" not in no_schema
    with_schema = _argv(codex_config, make_request(), schema="/path/schema.json")
    assert with_schema[with_schema.index("--output-schema") + 1] == "/path/schema.json"


def test_model_flag_uses_request_override_then_config(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # No model anywhere -> no flag.
    assert "--model" not in _argv(codex_config, make_request())
    # Config model used when set.
    cfg = replace(codex_config, model="cfg-model")
    argv_cfg = _argv(cfg, make_request())
    assert argv_cfg[argv_cfg.index("--model") + 1] == "cfg-model"
    # Request override wins over config.
    argv_req = _argv(cfg, make_request(model="req-model"))
    assert argv_req[argv_req.index("--model") + 1] == "req-model"


def test_forbidden_extra_args_are_rejected(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    bad = replace(codex_config, extra_args=("--dangerously-bypass-approvals-and-sandbox",))
    with pytest.raises(ProviderError) as exc:
        _argv(bad, make_request())
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR
    assert exc.value.is_fallback_eligible is False


def test_forbidden_extra_args_in_request_are_rejected(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    with pytest.raises(ProviderError) as exc:
        _argv(codex_config, make_request(extra_args=["--yolo"]))
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    "flag",
    [
        "-c",
        "--config",
        "-p",
        "--profile",
        "-P",
        "--sandbox",
        "--add-dir",
        "--ignore-user-config",
        # Approval/sandbox-mode selectors an operator must not slip in — they
        # replace the ``never`` approval policy the adapter owns and/or turn on a ``--sandbox`` mode
        # that makes Codex drop our ``default_permissions="worc"`` profile (private-file denials).
        "--full-auto",
        "-a",
        "--ask-for-approval",
    ],
)
def test_reserved_authority_extra_args_are_rejected(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest], flag: str
) -> None:
    # An operator cannot select/replace the owned permission, config, or tool authority.
    cfg = replace(codex_config, extra_args=(flag, "x"))
    with pytest.raises(ProviderError) as exc:
        _argv(cfg, make_request())
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR
    # inline form is caught too
    with pytest.raises(ProviderError):
        _argv(replace(codex_config, extra_args=(f"{flag}=x",)), make_request())


def test_reserved_approval_flag_in_request_extra_args_is_rejected(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # A flow node's ``extra_args`` (request-level) is checked exactly like config-level, so a node
    # cannot weaken the envelope with ``--full-auto`` either (envelope-cannot-be-weakened rule).
    with pytest.raises(ProviderError) as exc:
        _argv(codex_config, make_request(extra_args=["--full-auto"]))
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR


def test_benign_extra_args_are_appended(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(codex_config, extra_args=("--image", "/tmp/diagram.png"))
    argv = _argv(cfg, make_request())
    assert "--image" in argv and "/tmp/diagram.png" in argv


@pytest.mark.parametrize(
    "extra_args",
    [
        ("--sandbox", "danger-full-access"),
        ("--sandbox=danger-full-access",),
        ("-s", "danger-full-access"),
    ],
)
def test_full_access_selector_never_builds_argv(
    codex_config: ProviderConfig,
    make_request: Callable[..., AgentRunRequest],
    extra_args: tuple[str, ...],
) -> None:
    # Selecting full access discarded the generated profile wholesale — writable `.git`, and a
    # canary with no profile left to prove. Nothing may select it, so the argv is never built.
    cfg = replace(codex_config, extra_args=extra_args)
    with pytest.raises(ProviderError) as exc:
        _argv(cfg, make_request())
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR


def test_default_isolation_argv_unchanged(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Regression: default (read-isolation ON) → user config ignored, project untrusted, all
    # non-shell tool surfaces disabled (incl. hooks).
    argv = _argv(codex_config, make_request())
    assert "--ignore-user-config" in argv
    assert any('trust_level="untrusted"' in c for c in _config_values(argv))
    disabled = [argv[i + 1] for i, t in enumerate(argv[:-1]) if t == "--disable"]
    assert "hooks" in disabled and "multi_agent" in disabled and "plugins" in disabled


def test_read_isolation_off_restores_native_codex_config(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Native config discovery restored — user config loaded (no --ignore-user-config), project
    # TRUSTED (so .codex config/rules apply), and the `hooks` feature re-enabled. The heavier
    # autonomous tool surfaces stay disabled (out of read-isolation scope).
    argv = _argv(codex_config, make_request(), read_isolation_off=True)
    assert "--ignore-user-config" not in argv
    assert any('trust_level="trusted"' in c for c in _config_values(argv))
    disabled = [argv[i + 1] for i, t in enumerate(argv[:-1]) if t == "--disable"]
    assert "hooks" not in disabled
    assert "multi_agent" in disabled and "plugins" in disabled


def test_read_only_request_extends_read_only_profile(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(
        codex_config, make_request(working_directory="/clone", permission_profile="read-only")
    )
    profile = _profile_arg(argv)
    assert '"extends" = ":read-only"' in profile
    assert _fs_rule("/clone", "read") in profile
    assert "--sandbox" not in argv


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


def test_context_footer_empty_when_no_paths(
    make_request: Callable[..., AgentRunRequest],
) -> None:
    assert build_context_footer(make_request()) == ""


def test_effective_prompt_appends_footer(make_request: Callable[..., AgentRunRequest]) -> None:
    request = make_request(prompt="Do the thing.", task_path="/logs/t/task.md")
    effective = build_effective_prompt(request)
    assert effective.startswith("Do the thing.")
    assert "/logs/t/task.md" in effective


def test_reasoning_request_level_adds_reasoning_effort(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request(reasoning="high"))
    _assert_reasoning_config(argv, "high")


def test_reasoning_minimal_passes_through(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request(reasoning="minimal"))
    _assert_reasoning_config(argv, "minimal")


def test_reasoning_xhigh_passes_through(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request(reasoning="xhigh"))
    _assert_reasoning_config(argv, "xhigh")


def test_reasoning_max_clamped_to_xhigh(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request(reasoning="max"))
    _assert_reasoning_config(argv, "xhigh")


def test_reasoning_config_level_adds_reasoning_effort(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(codex_config, reasoning="medium")
    argv = _argv(cfg, make_request())
    _assert_reasoning_config(argv, "medium")


def test_reasoning_request_beats_config(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    cfg = replace(codex_config, reasoning="low")
    argv = _argv(cfg, make_request(reasoning="high"))
    _assert_reasoning_config(argv, "high")


def test_no_reasoning_means_no_reasoning_effort_flag(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    argv = _argv(codex_config, make_request())
    assert "--reasoning-effort" not in argv
    assert not any(value.startswith("model_reasoning_effort=") for value in _config_values(argv))


def test_session_id_builds_exec_resume_with_isolation_before_resume(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Durable sessions: ``codex exec [exec-options] resume <ID>``. codex 0.144.x grammar: exec-level
    # options (--cd/--json/--output-schema, the profile/config-isolation -c and
    # --ignore-user-config/--disable) MUST precede ``resume``; only -m/--model and -c/--config
    # (model_reasoning_effort) follow it. This keeps fresh/resume isolation identical.
    argv = _argv(
        codex_config,
        make_request(session_id="sess-123", model="gpt-5.4", reasoning="high"),
        schema="/logs/schema.json",
    )
    resume = argv.index("resume")
    assert argv[resume + 1] == "sess-123"  # id positional right after resume
    for flag in (
        "--cd",
        "--json",
        "--output-last-message",
        "--output-schema",
        "--ignore-user-config",
    ):
        assert argv.index(flag) < resume, f"{flag} must precede resume"
    # the permission-profile + default_permissions + project-trust -c values are all exec-level
    for i, tok in enumerate(argv):
        if tok in ("-c", "--config") and (
            argv[i + 1].startswith(f"permissions.{PROFILE_NAME}=")
            or argv[i + 1].startswith("default_permissions=")
            or argv[i + 1].startswith("projects.")
        ):
            assert i < resume, f"exec-level -c {argv[i + 1]!r} must precede resume"
    # resume-compatible options follow the subcommand.
    assert argv.index("--model") > resume
    assert argv.index('model_reasoning_effort="high"') > resume
    assert argv[-1] == "-"  # prompt still comes from stdin
    assert "--ask-for-approval" in argv  # security flags preserved


def test_no_session_id_has_no_resume(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    assert "resume" not in _argv(codex_config, make_request())


def test_advanced_mode_hands_back_every_feature_surface_but_keeps_the_profile(
    codex_config: ProviderConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Every name in the set was disabled unconditionally (all but `hooks`, which follows
    # read-isolation), and the only other way to reach them was the full-access escape this product
    # removed. Keeping them off in the one mode that exists to remove restrictions would turn a
    # floor control into a plain loss of function — including the browser surfaces and the memory
    # store the deny grew by in Ам4-11.
    argv = _argv(
        codex_config,
        make_request(working_directory="/clone"),
        strict_isolation=False,
        read_isolation_off=True,
    )
    assert "--disable" not in argv
    # What does NOT become optional: the generated profile and its selection. It is the local floor
    # in this mode, and it is what the pre-launch canary re-runs to prove that floor exists at all.
    assert f'default_permissions="{PROFILE_NAME}"' in _config_values(argv)
    assert any(v.startswith(f"permissions.{PROFILE_NAME}=") for v in _config_values(argv))
