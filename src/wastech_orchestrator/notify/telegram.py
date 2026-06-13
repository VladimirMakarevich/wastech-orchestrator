"""Telegram transport for :class:`Notifier` (spec §4.7).

The Core does not import this module directly — it talks to a :class:`Notifier`. The factory
:func:`build_notifier` resolves the bot token and chat id **only** from the env vars *named* by
:class:`TelegramConfig`; with the feature disabled or either env var missing/blank it returns a
silent :class:`NullNotifier`. All sends are best-effort: any Telegram or network failure is logged
at warning level (with token + chat id redacted) and never re-raised — a transport failure must
never change a task's terminal outcome.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from wastech_orchestrator.config.schema import TelegramConfig
from wastech_orchestrator.notify.interface import (
    AskKind,
    AskResult,
    Notifier,
    NullNotifier,
)
from wastech_orchestrator.providers.redaction import REDACTED, redact_text

_LOG = logging.getLogger(__name__)
_TRANSPORT_LOG_LOCK = threading.Lock()
_TRANSPORT_LOG_PREFIXES = ("httpx", "httpcore", "telegram")
_TRANSPORT_SILENT_LEVEL = logging.CRITICAL + 1

# Approval-reply matchers — case-insensitive, exact match against the trimmed reply. Anything else
# leaves ``approved=None`` so the caller can treat an unrecognized reply as "unknown" (not "no").
_APPROVE_WORDS: frozenset[str] = frozenset({"y", "yes", "approve", "approved", "ok", "confirm"})
_DENY_WORDS: frozenset[str] = frozenset({"n", "no", "deny", "denied", "cancel", "reject"})


class _TelegramClient(Protocol):
    """The minimum surface the notifier needs from a Telegram client (real or fake).

    A real implementation wraps ``python-telegram-bot`` (or a thin HTTP client) so tests can pass
    a deterministic fake without touching the network. ``poll_reply`` returns ``None`` when no
    matching reply arrived before the deadline.
    """

    def send_message(self, *, chat_id: str, text: str) -> None: ...

    def poll_reply(self, *, chat_id: str, deadline_monotonic: float) -> str | None: ...

    def get_me(self) -> str: ...  # returns bot @username (first_name as fallback)


@dataclass(frozen=True)
class _Secrets:
    bot_token: str
    chat_id: str


class TelegramNotifier:
    """Real Telegram-backed :class:`Notifier`. Use :func:`build_notifier` to construct from config.

    The client is injected so unit tests can avoid the network entirely. The notifier holds the
    resolved (token, chat id) only to forward them to the client and to feed redaction at the
    logger; they are never written to artifacts, SQLite, or any log without redaction.
    """

    def __init__(
        self,
        *,
        client: _TelegramClient,
        secrets: _Secrets,
        ask_timeout_s: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._secrets = secrets
        self._ask_timeout_s = max(0, int(ask_timeout_s))
        self._monotonic = monotonic

    def send_notification(
        self,
        *,
        task_id: str,
        final_status: str,
        pr_url: str | None,
        reason: str | None,
    ) -> None:
        body = _format_terminal_message(
            task_id=task_id, final_status=final_status, pr_url=pr_url, reason=reason
        )
        self._safe_send(body, op="send_notification", task_id=task_id)

    def ask_human(
        self,
        *,
        question: str,
        context: str,
        task_id: str,
        kind: AskKind,
        timeout_s: int,
    ) -> AskResult:
        prompt = _format_ask_message(question=question, context=context, task_id=task_id, kind=kind)
        if not self._safe_send(prompt, op="ask_human", task_id=task_id):
            # If we could not even deliver the prompt, treat it as a deterministic timeout so the
            # caller does not block on a reply that will never come.
            return AskResult(answered=False, timed_out=True)

        effective_timeout = min(max(0, int(timeout_s)), self._ask_timeout_s)
        deadline = self._monotonic() + effective_timeout
        try:
            reply = self._client.poll_reply(
                chat_id=self._secrets.chat_id, deadline_monotonic=deadline
            )
        except Exception as exc:  # noqa: BLE001 — transport failures are best-effort
            self._warn("ask_human poll failed", task_id=task_id, error=str(exc))
            return AskResult(answered=False, timed_out=True)

        if reply is None:
            return AskResult(answered=False, timed_out=True)
        return _interpret_reply(reply, kind=kind)

    def _safe_send(self, body: str, *, op: str, task_id: str) -> bool:
        try:
            self._client.send_message(chat_id=self._secrets.chat_id, text=body)
            return True
        except Exception as exc:  # noqa: BLE001 — Telegram/network failure must not propagate
            self._warn(f"{op} send failed", task_id=task_id, error=str(exc))
            return False

    def _warn(self, message: str, **fields: Any) -> None:
        # Redact the bot token and chat id from anything we attach to a record — secrets must never
        # reach the log sink (§12.6). The global RedactionFilter is a second line of defence.
        extras = {k: self._redact(str(v)) for k, v in fields.items()}
        _LOG.warning(
            self._redact(message),
            extra={"logfmt_fields": extras},
        )

    def _redact(self, value: str) -> str:
        # ``redact_text`` deliberately ignores literals shorter than four characters to avoid
        # mangling normal prose. Known Telegram credentials are different: remove them regardless
        # of length, then apply the shared structural redactor.
        redacted = value
        for secret in self._secret_literals():
            if secret:
                redacted = redacted.replace(secret, REDACTED)
        return redact_text(redacted, extra_secrets=self._secret_literals())

    def _secret_literals(self) -> tuple[str, ...]:
        return (self._secrets.bot_token, self._secrets.chat_id)


def build_notifier(
    cfg: TelegramConfig,
    env: Mapping[str, str] | None = None,
    *,
    client_factory: Callable[[_Secrets, TelegramConfig], _TelegramClient] | None = None,
) -> Notifier:
    """Resolve the transport from config + env (spec §4.7).

    Returns a silent :class:`NullNotifier` when:
    * ``cfg.enabled`` is ``False``; or
    * the env var named by ``cfg.bot_token_env`` is missing or blank; or
    * the env var named by ``cfg.chat_id_env`` is missing or blank.

    Otherwise builds a :class:`TelegramNotifier` backed by a real Telegram HTTP client (or the
    ``client_factory`` override, useful for adapters/tests). A single ``debug`` line is logged to
    record the chosen path; secret *values* are never logged.
    """
    environ = env if env is not None else os.environ
    if not cfg.enabled:
        _LOG.debug(
            "telegram notifier disabled",
            extra={"logfmt_fields": {"reason": "disabled"}},
        )
        return NullNotifier()

    bot_token = (environ.get(cfg.bot_token_env) or "").strip()
    chat_id = (environ.get(cfg.chat_id_env) or "").strip()
    if not bot_token or not chat_id:
        missing = []
        if not bot_token:
            missing.append(cfg.bot_token_env)
        if not chat_id:
            missing.append(cfg.chat_id_env)
        _LOG.debug(
            "telegram notifier disabled",
            extra={"logfmt_fields": {"reason": "missing_env", "vars": ",".join(missing)}},
        )
        return NullNotifier()

    secrets = _Secrets(bot_token=bot_token, chat_id=chat_id)
    factory = client_factory or _default_client_factory
    return TelegramNotifier(
        client=factory(secrets, cfg),
        secrets=secrets,
        ask_timeout_s=cfg.ask_timeout_s,
    )


def check_telegram_preflight(
    cfg: TelegramConfig,
    env: Mapping[str, str] | None = None,
    *,
    client_factory: Callable[[_Secrets, TelegramConfig], _TelegramClient] | None = None,
) -> tuple[bool, str]:
    """Return ``(ok, report_line)`` for inclusion in the preflight report (spec §6.7).

    * ``enabled: false`` → ``(True, "telegram: SKIP (disabled)")`` — never fails preflight.
    * Missing env vars → ``(False, "telegram: FAIL — env var(s) not set: …")``.
    * Credentials present + ``get_me()`` OK → ``(True, "telegram: OK (bot=@…, …)")``.
    * Credentials present + ``get_me()`` fails → ``(False, "telegram: FAIL — API error (…)")``,
      with the bot token and chat id redacted from the error message.
    """
    if not cfg.enabled:
        return True, "telegram: SKIP (disabled)"

    environ: Mapping[str, str] = env if env is not None else os.environ
    bot_token = (environ.get(cfg.bot_token_env) or "").strip()
    chat_id = (environ.get(cfg.chat_id_env) or "").strip()

    missing = [n for n, v in [(cfg.bot_token_env, bot_token), (cfg.chat_id_env, chat_id)] if not v]
    if missing:
        return False, f"telegram: FAIL — env var(s) not set: {', '.join(missing)}"

    secrets = _Secrets(bot_token=bot_token, chat_id=chat_id)
    client = (client_factory or _default_client_factory)(secrets, cfg)
    try:
        username = client.get_me()
        return True, f"telegram: OK (bot=@{username}, chat_id configured)"
    except Exception as exc:  # noqa: BLE001 — surface a safe summary, never re-raise
        safe = redact_text(str(exc), extra_secrets=(bot_token, chat_id))
        return False, f"telegram: FAIL — API error ({safe})"


def _format_terminal_message(
    *,
    task_id: str,
    final_status: str,
    pr_url: str | None,
    reason: str | None,
) -> str:
    parts = [f"[{task_id}] status={final_status}"]
    if pr_url:
        parts.append(f"pr={pr_url}")
    if reason:
        parts.append(f"reason={reason}")
    return " ".join(parts)


def _format_ask_message(
    *,
    question: str,
    context: str,
    task_id: str,
    kind: AskKind,
) -> str:
    header = f"[{task_id}] {kind}"
    body = question.strip()
    ctx = context.strip()
    if ctx:
        return f"{header}\n{body}\n\nContext:\n{ctx}"
    return f"{header}\n{body}"


def _interpret_reply(reply: str, *, kind: AskKind) -> AskResult:
    text = reply.strip()
    if kind == "approval":
        normalized = text.lower()
        if normalized in _APPROVE_WORDS:
            return AskResult(answered=True, text=text, approved=True)
        if normalized in _DENY_WORDS:
            return AskResult(answered=True, text=text, approved=False)
        # An ambiguous reply still counts as an answer (we relayed it), just unknown approval.
        return AskResult(answered=True, text=text, approved=None)
    return AskResult(answered=True, text=text, approved=None)


def _default_client_factory(secrets: _Secrets, _cfg: TelegramConfig) -> _TelegramClient:
    """Build the real client; the dependency import remains lazy until the first operation."""
    return _HttpTelegramClient(bot_token=secrets.bot_token)


class _HttpTelegramClient:
    """Thin synchronous wrapper around ``python-telegram-bot``'s HTTP surface.

    ``python-telegram-bot`` 21+ is asynchronous. Each synchronous entry point owns a short-lived
    event loop and uses ``Bot`` as an async context manager, which initializes and shuts down its
    HTTP client correctly. Exceptions propagate to the notifier, which logs and swallows them.
    """

    def __init__(self, *, bot_token: str) -> None:
        self._bot_token = bot_token
        self._last_update_id: int | None = None

    def send_message(self, *, chat_id: str, text: str) -> None:
        import asyncio

        from telegram import Bot

        async def send() -> None:
            async with Bot(self._bot_token) as bot:
                await bot.send_message(chat_id=chat_id, text=text)

        with _suppress_transport_request_logs():
            asyncio.run(send())

    def get_me(self) -> str:
        import asyncio

        from telegram import Bot

        async def fetch() -> str:
            async with Bot(self._bot_token) as bot:
                me = await bot.get_me()
                return me.username or me.first_name

        with _suppress_transport_request_logs():
            return asyncio.run(fetch())

    def poll_reply(self, *, chat_id: str, deadline_monotonic: float) -> str | None:
        import asyncio

        from telegram import Bot

        target_chat = int(chat_id)

        async def poll() -> str | None:
            async with Bot(self._bot_token) as bot:
                while True:
                    remaining = deadline_monotonic - time.monotonic()
                    if remaining <= 0:
                        return None
                    poll_timeout = max(1, min(50, int(remaining)))
                    try:
                        updates = await asyncio.wait_for(
                            bot.get_updates(
                                offset=(self._last_update_id + 1)
                                if self._last_update_id is not None
                                else None,
                                timeout=poll_timeout,
                            ),
                            timeout=remaining,
                        )
                    except TimeoutError:
                        return None
                    for update in updates:
                        self._last_update_id = update.update_id
                        msg = update.message
                        if msg is None or msg.chat.id != target_chat:
                            continue
                        if isinstance(msg.text, str) and msg.text:
                            return msg.text

        with _suppress_transport_request_logs():
            return asyncio.run(poll())


@contextmanager
def _suppress_transport_request_logs() -> Iterator[None]:
    """Prevent third-party HTTP loggers from emitting token-bearing Telegram request URLs."""
    with _TRANSPORT_LOG_LOCK:
        manager = logging.Logger.manager.loggerDict
        names = set(_TRANSPORT_LOG_PREFIXES)
        names.update(
            name
            for name in manager
            if isinstance(name, str)
            and any(
                name == prefix or name.startswith(f"{prefix}.")
                for prefix in _TRANSPORT_LOG_PREFIXES
            )
        )
        loggers = [logging.getLogger(name) for name in names]
        previous_levels = [logger.level for logger in loggers]
        try:
            for logger in loggers:
                if logger.getEffectiveLevel() < _TRANSPORT_SILENT_LEVEL:
                    logger.setLevel(_TRANSPORT_SILENT_LEVEL)
            yield
        finally:
            for logger, level in zip(loggers, previous_levels, strict=True):
                logger.setLevel(level)
