"""Tests for the shared forbidden-flag detector and the gated full-access selector detector."""

from __future__ import annotations

import pytest

from wastech_orchestrator.security.forbidden_args import (
    CODEX_SAFE_CONFIG_KEYS,
    FORBIDDEN_SANDBOX_VALUE,
    CodexExtraArgsError,
    find_forbidden_args,
    find_full_access_args,
    parse_codex_extra_args,
    render_codex_extra_args,
)


@pytest.mark.parametrize(
    "args",
    [
        ("--dangerously-bypass-approvals-and-sandbox",),
        ("--dangerously-skip-permissions",),
        ("--dangerously-bypass-hook-trust",),
        ("--yolo",),
        ("--ignore-rules",),
        ("--model", "gpt-x", "--yolo"),  # offending flag not first
        ("--sandbox",),  # dangling: no value (last token)
        ("-s",),  # dangling short form
        ("--sandbox=",),  # trailing '=' with empty value
    ],
)
def test_forbidden_args_are_detected(args: tuple[str, ...]) -> None:
    assert find_forbidden_args(args) != []


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--model", "gpt-5"),
        ("--sandbox", "workspace-write"),
        ("--sandbox=read-only",),
        ("--json", "--output-last-message", "/tmp/last.txt"),
        # The structured full-access selectors are NO LONGER an absolute ban — they are gated by
        # strict_isolation (find_full_access_args), so find_forbidden_args must let them through.
        ("--sandbox=danger-full-access",),
        ("--sandbox", "danger-full-access"),
        ("-s", "danger-full-access"),
        ("-s=danger-full-access",),
        ("--permission-mode", "bypassPermissions"),
        ("--permission-mode=bypassPermissions",),
    ],
)
def test_safe_args_yield_no_reasons(args: tuple[str, ...]) -> None:
    assert find_forbidden_args(args) == []


@pytest.mark.parametrize(
    "args",
    [
        ("--sandbox=danger-full-access",),
        ("--sandbox", "danger-full-access"),
        ("-s", "danger-full-access"),
        ("-s=danger-full-access",),
        ("--permission-mode", "bypassPermissions"),
        ("--permission-mode=bypassPermissions",),
        ("--model", "gpt-x", "--sandbox", "danger-full-access"),  # selector not first
    ],
)
def test_full_access_selectors_are_detected(args: tuple[str, ...]) -> None:
    assert find_full_access_args(args) != []


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--sandbox", "workspace-write"),
        ("--sandbox=read-only",),
        ("--permission-mode", "acceptEdits"),  # an escalation, but not the full bypass value
        ("--dangerously-skip-permissions",),  # an absolute-ban flag, not a structured selector
    ],
)
def test_non_full_access_args_yield_no_full_access_reasons(args: tuple[str, ...]) -> None:
    assert find_full_access_args(args) == []


def test_full_access_reason_names_the_forbidden_sandbox_value() -> None:
    reasons = find_full_access_args(("--sandbox", "danger-full-access"))
    assert any(FORBIDDEN_SANDBOX_VALUE in reason for reason in reasons)


def test_full_access_reason_names_bypass_permission_mode() -> None:
    reasons = find_full_access_args(("--permission-mode", "bypassPermissions"))
    assert any("bypassPermissions" in reason for reason in reasons)


@pytest.mark.parametrize(
    "option",
    [
        "--add-dir",
        "--sandbox",
        "-s",
        "--profile",
        "-p",
        "--enable",
        "--disable",
        "--oss",
        "--local-provider",
        "--image",
        "-i",
        "--model",
        "-m",
        "--cd",
        "-C",
        "--output-schema",
        "--output-last-message",
        "-o",
        "--json",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-rules",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
    ],
)
def test_codex_cli_options_outside_closed_allowlist_are_rejected(option: str) -> None:
    with pytest.raises(CodexExtraArgsError) as exc:
        parse_codex_extra_args((option, "operator-value"))
    assert option in str(exc.value)
    assert "operator-value" not in str(exc.value)


@pytest.mark.parametrize(
    "key",
    [
        "approval_policy",
        "sandbox_mode",
        "sandbox_permissions",
        "default_permissions",
        "permissions.unconfined.filesystem",
        "sandbox_workspace_write.writable_roots",
        "sandbox_workspace_write.network_access",
        "web_search",
        "tools.web_search",
        "features.web_search_request",
        "features.apps",
        "apps.example.enabled",
        "mcp_servers.example.command",
        "hooks.Stop",
        "plugins.example.mcp_servers.server.enabled",
        "shell_environment_policy.inherit",
        "projects.workspace.trust_level",
        "model_provider",
        "model_providers.example.base_url",
        "openai_base_url",
        "notify",
        "developer_instructions",
        "model_instructions_file",
        "project_doc_max_bytes",
        "project_doc_fallback_filenames",
        "skills.config",
        "agents.worker.config_file",
        "computer_use.windows.always_allowed_app_ids",
        "windows.sandbox",
        "allow_login_shell",
    ],
)
@pytest.mark.parametrize("flag", ["-c", "--config"])
def test_codex_authority_config_keys_are_rejected_without_values(flag: str, key: str) -> None:
    secret = "not-a-pattern-secret-987654"
    with pytest.raises(CodexExtraArgsError) as exc:
        parse_codex_extra_args((flag, f'{key}="{secret}"'))
    assert key in str(exc.value)
    assert secret not in str(exc.value)


@pytest.mark.parametrize(
    "args",
    [
        ("-c", 'model_verbosity="low"'),
        ("--config", 'model_verbosity="low"'),
        ('-c=model_verbosity="low"',),
        ('--config=model_verbosity="low"',),
        (
            "--strict-config",
            "-c",
            'model_verbosity="low"',
            "--ignore-user-config",
            "-c",
            'personality="pragmatic"',
        ),
    ],
)
def test_codex_safe_syntaxes_are_parsed_and_canonicalized(args: tuple[str, ...]) -> None:
    rendered = render_codex_extra_args(args)
    assert all(
        item.option in {"--config", "--strict-config", "--ignore-user-config"}
        for item in parse_codex_extra_args(args)
    )
    assert "-c" not in rendered
    assert all("=" not in token for token in rendered if token.startswith("--config"))


def test_every_documented_safe_codex_config_key_is_accepted() -> None:
    args = tuple(token for key in sorted(CODEX_SAFE_CONFIG_KEYS) for token in ("-c", f"{key}=true"))
    parsed = parse_codex_extra_args(args)
    assert len(parsed) == len(CODEX_SAFE_CONFIG_KEYS)


def test_repeated_codex_config_overrides_preserve_order() -> None:
    args = (
        "-c",
        'personality="pragmatic"',
        "--strict-config",
        '--config=model_verbosity="low"',
        '-c=model_reasoning_summary="auto"',
    )
    assert render_codex_extra_args(args) == (
        "--config",
        'personality="pragmatic"',
        "--strict-config",
        "--config",
        'model_verbosity="low"',
        "--config",
        'model_reasoning_summary="auto"',
    )


@pytest.mark.parametrize(
    "args",
    [
        ("-c",),
        ("--config=",),
        ("--config", "--strict-config"),
        ("--config", "missing-equals"),
        ("--config", ".bad=value"),
        ("--strict-config=value",),
        ("positional",),
    ],
)
def test_codex_malformed_extra_args_fail_closed(args: tuple[str, ...]) -> None:
    with pytest.raises(CodexExtraArgsError):
        parse_codex_extra_args(args)
