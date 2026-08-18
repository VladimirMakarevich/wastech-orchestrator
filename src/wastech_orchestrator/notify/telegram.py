"""Telegram transport for :class:`Notifier`.

The Core does not import this module directly — it talks to a :class:`Notifier`. The factory
:func:`build_notifier` resolves the bot token and chat id **only** from the env vars *named* by
:class:`TelegramConfig`; with the feature disabled or either env var missing/blank it returns a
silent :class:`NullNotifier`. Terminal notifications are best-effort. Blocking HITL returns typed
timeout/transport/invalid-response failures to the Core, which applies fail-closed task semantics.
Transport exceptions are logged with token + chat id redacted and are never re-raised.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Coroutine, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from wastech_orchestrator.config.schema import TelegramConfig
from wastech_orchestrator.notify.interface import (
    TRACE_GIT_CONTROL_DRIFT,
    TRACE_REWORK_EXHAUSTED,
    TRACE_UNEXPECTED_WRITE,
    AskHandle,
    AskKind,
    AskResult,
    Notifier,
    NullNotifier,
    TerminalDetails,
    terminal_reason_prose,
)
from wastech_orchestrator.providers.redaction import REDACTED, redact_text

_LOG = logging.getLogger(__name__)
_TRANSPORT_LOG_LOCK = threading.Lock()
_TRANSPORT_LOG_PREFIXES = ("httpx", "httpcore", "telegram")
_TRANSPORT_SILENT_LEVEL = logging.CRITICAL + 1
_TELEGRAM_TEXT_LIMIT = 4096
_TRUNCATION_SUFFIX = "\n\n[message truncated by wastech-orchestrator]"
_ALLOWED_UPDATES = ("message", "callback_query")

# Feedback shown on the button itself when a callback is pressed. Every press in our chat is
# acknowledged so the operator never sees "nothing happened"; a stale/duplicate press gets an alert.
_ACK_APPROVED = "Approved — continuing."
_ACK_DENIED = "Denied — will reconsider."
_ACK_STALE = "This approval is no longer active — check the latest message for the current request."


@dataclass(frozen=True)
class _SentPrompt:
    message_id: int
    update_offset: int | None


@dataclass(frozen=True)
class _ClientReply:
    text: str
    approved: bool | None = None


class _TelegramClient(Protocol):
    """The minimum surface the notifier needs from a Telegram client (real or fake).

    A real implementation wraps ``python-telegram-bot`` (or a thin HTTP client) so tests can pass
    a deterministic fake without touching the network. ``poll_reply`` returns ``None`` when no
    matching reply arrived before the deadline.
    """

    def send_message(self, *, chat_id: str, text: str) -> None: ...

    def send_prompt(
        self,
        *,
        chat_id: str,
        text: str,
        kind: AskKind,
        interaction_id: str,
    ) -> _SentPrompt: ...

    def poll_reply(
        self,
        *,
        chat_id: str,
        prompt_message_id: int,
        update_offset: int | None,
        interaction_id: str,
        kind: AskKind,
        deadline_monotonic: float,
    ) -> _ClientReply | None: ...

    def get_me(self) -> str: ...  # bare username (first_name fallback); the caller prepends '@'

    def get_chat(self, *, chat_id: str) -> str: ...

    def get_webhook_url(self) -> str: ...

    def check_polling(self) -> None: ...


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
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._secrets = secrets
        self._ask_timeout_s = max(0, int(ask_timeout_s))
        self._monotonic = monotonic
        self._wall_clock = wall_clock

    def send_notification(
        self,
        *,
        task_id: str,
        final_status: str,
        pr_url: str | None,
        reason: str | None,
        contacts: tuple[str, ...] = (),
        governance_changed: tuple[str, ...] = (),
        details: TerminalDetails | None = None,
    ) -> None:
        body = _format_terminal_message(
            task_id=task_id,
            final_status=final_status,
            pr_url=pr_url,
            reason=reason,
            contacts=contacts,
            governance_changed=governance_changed,
            details=details,
        )
        self._safe_send(body, op="send_notification", task_id=task_id)

    def send_trace(self, *, task_id: str, node_id: str, outcome: str) -> None:
        body = _format_trace_message(task_id=task_id, node_id=node_id, outcome=outcome)
        self._safe_send(body, op="send_trace", task_id=task_id)

    def ask_human(
        self,
        *,
        question: str,
        context: str,
        task_id: str,
        kind: AskKind,
        timeout_s: int,
        interaction_id: str = "adhoc",
        contacts: tuple[str, ...] = (),
    ) -> AskResult:
        handle = self.start_ask(
            question=question,
            context=context,
            task_id=task_id,
            kind=kind,
            timeout_s=timeout_s,
            interaction_id=interaction_id,
            contacts=contacts,
        )
        return self.wait_for_answer(handle)

    def start_ask(
        self,
        *,
        question: str,
        context: str,
        task_id: str,
        kind: AskKind,
        timeout_s: int,
        interaction_id: str,
        contacts: tuple[str, ...] = (),
    ) -> AskHandle:
        effective_timeout = min(max(0, int(timeout_s)), self._ask_timeout_s)
        prompt = _format_ask_message(
            question=question,
            context=context,
            task_id=task_id,
            kind=kind,
            contacts=contacts,
        )
        safe_prompt = self._outgoing(prompt)
        try:
            sent = self._client.send_prompt(
                chat_id=self._secrets.chat_id,
                text=safe_prompt,
                kind=kind,
                interaction_id=interaction_id,
            )
        except Exception as exc:
            self._warn("ask_human send failed", task_id=task_id, error=str(exc))
            return AskHandle(
                interaction_id=interaction_id,
                kind=kind,
                expires_at=self._wall_clock(),
                delivered=False,
            )
        return AskHandle(
            interaction_id=interaction_id,
            kind=kind,
            expires_at=self._wall_clock() + effective_timeout,
            message_id=sent.message_id,
            update_offset=sent.update_offset,
        )

    def wait_for_answer(self, handle: AskHandle) -> AskResult:
        if not handle.delivered or handle.message_id is None:
            return AskResult(
                answered=False,
                failure="transport_error",
                interaction_id=handle.interaction_id,
                message_id=handle.message_id,
            )
        remaining = max(0.0, handle.expires_at - self._wall_clock())
        deadline = self._monotonic() + remaining
        try:
            reply = self._client.poll_reply(
                chat_id=self._secrets.chat_id,
                prompt_message_id=handle.message_id,
                update_offset=handle.update_offset,
                interaction_id=handle.interaction_id,
                kind=handle.kind,
                deadline_monotonic=deadline,
            )
        except Exception as exc:
            self._warn(
                "ask_human poll failed",
                interaction_id=handle.interaction_id,
                error=str(exc),
            )
            return AskResult(
                answered=False,
                failure="transport_error",
                interaction_id=handle.interaction_id,
                message_id=handle.message_id,
            )
        if reply is None:
            return AskResult(
                answered=False,
                timed_out=True,
                failure="timeout",
                interaction_id=handle.interaction_id,
                message_id=handle.message_id,
            )
        if handle.kind == "approval" and reply.approved is None:
            return AskResult(
                answered=True,
                text=self._redact(reply.text),
                failure="invalid_response",
                interaction_id=handle.interaction_id,
                message_id=handle.message_id,
            )
        return AskResult(
            answered=True,
            text=self._redact(reply.text),
            approved=reply.approved,
            interaction_id=handle.interaction_id,
            message_id=handle.message_id,
        )

    def _safe_send(self, body: str, *, op: str, task_id: str) -> bool:
        try:
            self._client.send_message(chat_id=self._secrets.chat_id, text=self._outgoing(body))
            return True
        except Exception as exc:
            self._warn(f"{op} send failed", task_id=task_id, error=str(exc))
            return False

    def _warn(self, message: str, **fields: Any) -> None:
        # Redact the bot token and chat id from anything we attach to a record — secrets must never
        # reach the log sink. The global RedactionFilter is a second line of defence.
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

    def _outgoing(self, value: str) -> str:
        return _limit_message(self._redact(value))


def build_notifier(
    cfg: TelegramConfig,
    env: Mapping[str, str] | None = None,
    *,
    client_factory: Callable[[_Secrets, TelegramConfig], _TelegramClient] | None = None,
) -> Notifier:
    """Resolve the transport from config + env.

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
    """Return ``(ok, report_line)`` for inclusion in the preflight report.

    * ``enabled: false`` → ``(True, "telegram: SKIP (disabled)")`` — never fails preflight.
    * Missing env vars → ``(False, "telegram: FAIL — env var(s) not set: …")``.
    * Credentials present + bot/chat/polling checks OK → ``(True, "telegram: OK (…)")``.
    * An invalid numeric chat id, configured webhook, inaccessible chat, or API failure is fatal.
    """
    if not cfg.enabled:
        return True, "telegram: SKIP (disabled)"

    environ: Mapping[str, str] = env if env is not None else os.environ
    bot_token = (environ.get(cfg.bot_token_env) or "").strip()
    chat_id = (environ.get(cfg.chat_id_env) or "").strip()

    missing = [n for n, v in [(cfg.bot_token_env, bot_token), (cfg.chat_id_env, chat_id)] if not v]
    if missing:
        return False, f"telegram: FAIL — env var(s) not set: {', '.join(missing)}"
    if re.fullmatch(r"-?[1-9][0-9]*", chat_id) is None:
        return False, "telegram: FAIL — chat id must be a numeric Telegram chat id"

    secrets = _Secrets(bot_token=bot_token, chat_id=chat_id)
    try:
        client = (client_factory or _default_client_factory)(secrets, cfg)
        username = client.get_me()
        chat = client.get_chat(chat_id=chat_id)
        webhook_url = client.get_webhook_url()
        if webhook_url:
            return (
                False,
                "telegram: FAIL — an outgoing webhook is configured; HITL requires polling",
            )
        client.check_polling()
        return True, f"telegram: OK (bot=@{username}, chat={chat}, polling ready)"
    except Exception as exc:
        safe = str(exc)
        for secret in (bot_token, chat_id):
            safe = safe.replace(secret, REDACTED)
        safe = redact_text(safe, extra_secrets=(bot_token, chat_id))
        return False, f"telegram: FAIL — API error ({safe})"


# Maps a terminal status to a glanceable severity glyph for the operator-facing notification
# 🛑 (a human is required; the branch is preserved) is deliberately distinct from a clean
# ✅ finish, per the operator's request for a strong needs-attention marker, and from the trace
# vocabulary's ⚠️. Mirrors the _TRACE_EMOJI pattern used for the live per-node trace.
_STATUS_EMOJI: dict[str, str] = {
    "done": "✅",
    "manual_action_required": "🛑",
    "failed": "❌",
}

# Statuses whose terminal notification expands into the enriched body when details are available; a
# clean `done` stays terse — a successful task must not become noisy.
_ATTENTION_STATUSES = frozenset({"manual_action_required", "failed"})

# One-line cap for the agent-authored blocking-finding reason echoed into the chat. Redaction still
# runs on the whole message (via `_outgoing`); this only keeps a single finding readable.
_FINDING_REASON_LIMIT = 200


def _format_terminal_message(
    *,
    task_id: str,
    final_status: str,
    pr_url: str | None,
    reason: str | None,
    contacts: tuple[str, ...] = (),
    governance_changed: tuple[str, ...] = (),
    details: TerminalDetails | None = None,
) -> str:
    glyph = _STATUS_EMOJI.get(final_status, "")
    prefix = f"{glyph} " if glyph else ""
    if details is not None and final_status in _ATTENTION_STATUSES:
        return _format_attention_message(
            prefix=prefix,
            task_id=task_id,
            final_status=final_status,
            pr_url=pr_url,
            reason=reason,
            contacts=contacts,
            governance_changed=governance_changed,
            details=details,
        )
    parts = [f"{prefix}[{task_id}] status={final_status}"]
    if pr_url:
        parts.append(f"pr={pr_url}")
    if reason:
        parts.append(f"reason={reason}")
    if contacts:
        parts.append(f"contacts={' '.join(contacts)}")
    if governance_changed:
        # A non-blocking notice — this run edited its own governance/instruction files.
        parts.append(f"governance={','.join(governance_changed)}")
    return " ".join(parts)


def _format_attention_message(
    *,
    prefix: str,
    task_id: str,
    final_status: str,
    pr_url: str | None,
    reason: str | None,
    contacts: tuple[str, ...],
    governance_changed: tuple[str, ...],
    details: TerminalDetails,
) -> str:
    """The enriched multi-line body for a needs-attention terminal.

    Plain text + emoji (no parse_mode): id + severity glyph, then title, where it stopped, a prose
    reason, the top blocking finding + its paths, and the on-disk report to open next. Every section
    is emitted only when its datum is present, so a call site with thin context degrades gracefully.
    """
    lines = [f"{prefix}{final_status} — {task_id}"]
    if details.title:
        lines.append(details.title)
    body: list[str] = []
    stopped = _stopped_line(details)
    if stopped:
        body.append(stopped)
    why = terminal_reason_prose(reason)
    if why:
        body.append(f"Why: {why}")
    if details.finding is not None:
        body.append(f"Blocking ({details.finding.severity}): {_one_line(details.finding.reason)}")
        if details.finding.paths:
            body.append(f"Paths: {', '.join(details.finding.paths)}")
    if details.branch:
        body.append(f"Branch: {details.branch}")
    if pr_url:
        body.append(f"PR: {pr_url}")
    if contacts:
        body.append(f"Contacts: {' '.join(contacts)}")
    if governance_changed:
        body.append(f"Governance files changed: {', '.join(governance_changed)}")
    if details.report_path:
        body.append(f"Details: {details.report_path}")
    if body:
        lines.append("")  # blank line separates the header/title from the body
        lines.extend(body)
    return "\n".join(lines)


def _stopped_line(details: TerminalDetails) -> str | None:
    """The 'Stopped at: <node> (<loop> loop), after N fix rounds' line; None with no stop node."""
    if not details.stop_node:
        return None
    text = f"Stopped at: {details.stop_node}"
    if details.loop:
        text += f" ({details.loop} loop)"
    if details.fix_rounds is not None:
        plural = "" if details.fix_rounds == 1 else "s"
        text += f", after {details.fix_rounds} fix round{plural}"
    return text


def _one_line(text: str, *, limit: int = _FINDING_REASON_LIMIT) -> str:
    """Collapse to a single bounded line for an agent-authored finding reason."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


# Maps a node's edge-selecting outcome (NodeOutcome.kind) to a glanceable emoji. The distinct
# leading glyph also keeps a trace line visually separable from HITL gate prompts in the same chat.
# Three labels here are synthetic (not raw NodeOutcome.kinds), all rendered ⚠️ so they read as
# "moved on, may need follow-up" rather than a clean pass: TRACE_REWORK_EXHAUSTED is a non-blocking
# evaluator that accepted only because its max_rework_per_stage budget ran out,
# TRACE_UNEXPECTED_WRITE is a node with a shell but no write access that changed the working tree,
# and TRACE_GIT_CONTROL_DRIFT is such a node changing git control state.
_TRACE_EMOJI: dict[str, str] = {
    "done": "✅",
    "accept": "✅",
    "pass": "✅",
    "rework": "🔁",
    "fail": "❌",
    TRACE_REWORK_EXHAUSTED: "⚠️",
    TRACE_UNEXPECTED_WRITE: "⚠️",
    TRACE_GIT_CONTROL_DRIFT: "⚠️",
}


def _format_trace_message(*, task_id: str, node_id: str, outcome: str) -> str:
    emoji = _TRACE_EMOJI.get(outcome, "▶️")
    return f"[{task_id}] {emoji} {node_id} → {outcome}"


def _format_ask_message(
    *,
    question: str,
    context: str,
    task_id: str,
    kind: AskKind,
    contacts: tuple[str, ...] = (),
) -> str:
    header = f"[{task_id}] {kind}"
    body = question.strip()
    ctx = context.strip()
    contact_line = f"\nContacts: {' '.join(contacts)}" if contacts else ""
    if ctx:
        return f"{header}{contact_line}\n{body}\n\nContext:\n{ctx}"
    return f"{header}{contact_line}\n{body}"


def _limit_message(text: str) -> str:
    if len(text) <= _TELEGRAM_TEXT_LIMIT:
        return text
    keep = _TELEGRAM_TEXT_LIMIT - len(_TRUNCATION_SUFFIX)
    return text[:keep] + _TRUNCATION_SUFFIX


def _default_client_factory(secrets: _Secrets, _cfg: TelegramConfig) -> _TelegramClient:
    """Build the real client; the dependency import remains lazy until the first operation."""
    return _HttpTelegramClient(bot_token=secrets.bot_token)


def _run_sync[T](factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run one fresh coroutine behind the synchronous notifier contract.

    A caller that already owns an asyncio loop cannot use :func:`asyncio.run` on that thread. In
    that case the coroutine is constructed and run on a dedicated worker; otherwise the direct path
    avoids the thread. A factory is required so no coroutine object crosses event-loop boundaries.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="worc-telegram") as pool:
        return pool.submit(lambda: asyncio.run(factory())).result()


class _HttpTelegramClient:
    """Thin synchronous wrapper around ``python-telegram-bot``'s HTTP surface.

    ``python-telegram-bot`` 21+ is asynchronous. Each synchronous entry point owns a short-lived
    event loop, moved to a worker when the caller already has a running loop, and uses ``Bot`` as an
    async context manager, which initializes and shuts down its HTTP client correctly. Exceptions
    propagate to the notifier, which logs and swallows them.
    """

    def __init__(self, *, bot_token: str) -> None:
        self._bot_token = bot_token

    def send_message(self, *, chat_id: str, text: str) -> None:
        from telegram import Bot

        async def send() -> None:
            async with Bot(self._bot_token) as bot:
                await bot.send_message(chat_id=chat_id, text=text)

        with _suppress_transport_request_logs():
            _run_sync(send)

    def get_me(self) -> str:
        from telegram import Bot

        async def fetch() -> str:
            async with Bot(self._bot_token) as bot:
                me = await bot.get_me()
                return me.username or me.first_name

        with _suppress_transport_request_logs():
            return _run_sync(fetch)

    def get_chat(self, *, chat_id: str) -> str:
        from telegram import Bot

        async def fetch() -> str:
            async with Bot(self._bot_token) as bot:
                chat = await bot.get_chat(chat_id=int(chat_id))
                return chat.title or chat.full_name or chat.type

        with _suppress_transport_request_logs():
            return _run_sync(fetch)

    def get_webhook_url(self) -> str:
        from telegram import Bot

        async def fetch() -> str:
            async with Bot(self._bot_token) as bot:
                info = await bot.get_webhook_info()
                return info.url or ""

        with _suppress_transport_request_logs():
            return _run_sync(fetch)

    def check_polling(self) -> None:
        from telegram import Bot
        from telegram.error import Conflict

        async def check() -> None:
            async with Bot(self._bot_token) as bot:
                try:
                    await bot.get_updates(limit=1, timeout=0, allowed_updates=_ALLOWED_UPDATES)
                except Conflict as exc:
                    # Another getUpdates consumer already holds this bot token (HTTP 409). Diagnose
                    # it clearly at preflight so HITL is not silently broken at run time.
                    raise RuntimeError(
                        "another process is already polling this bot token "
                        "(Telegram 409 Conflict); only one poller may run per bot token"
                    ) from exc

        with _suppress_transport_request_logs():
            _run_sync(check)

    def send_prompt(
        self,
        *,
        chat_id: str,
        text: str,
        kind: AskKind,
        interaction_id: str,
    ) -> _SentPrompt:
        from telegram import Bot, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

        async def send() -> _SentPrompt:
            async with Bot(self._bot_token) as bot:
                offset = await self._drain_pending(bot)
                markup: InlineKeyboardMarkup | ForceReply
                if kind == "approval":
                    markup = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "Approve",
                                    callback_data=f"hitl:{interaction_id}:yes",
                                ),
                                InlineKeyboardButton(
                                    "Deny",
                                    callback_data=f"hitl:{interaction_id}:no",
                                ),
                            ]
                        ]
                    )
                else:
                    markup = ForceReply(
                        selective=True,
                        input_field_placeholder="Reply to this message",
                    )
                message = await bot.send_message(
                    chat_id=int(chat_id),
                    text=text,
                    reply_markup=markup,
                )
                return _SentPrompt(message_id=message.message_id, update_offset=offset)

        with _suppress_transport_request_logs():
            return _run_sync(send)

    async def _drain_pending(self, bot: Any) -> int | None:
        offset: int | None = None
        for _ in range(10):
            updates = await bot.get_updates(
                offset=offset,
                limit=100,
                timeout=0,
                allowed_updates=_ALLOWED_UPDATES,
            )
            if not updates:
                return offset
            offset = max(update.update_id for update in updates) + 1
        raise RuntimeError("telegram update backlog is too large to drain safely")

    async def _ack(self, bot: Any, query_id: str, text: str, *, alert: bool = False) -> None:
        """Acknowledge a callback press (clears the client's spinner and shows ``text``).

        Best-effort: a failure to acknowledge (e.g. a too-old query) must never break the poll loop
        or mask a matched reply.
        """
        try:
            await bot.answer_callback_query(query_id, text=text, show_alert=alert)
        except Exception:
            _LOG.debug("telegram answer_callback_query failed", exc_info=True)

    def poll_reply(
        self,
        *,
        chat_id: str,
        prompt_message_id: int,
        update_offset: int | None,
        interaction_id: str,
        kind: AskKind,
        deadline_monotonic: float,
    ) -> _ClientReply | None:
        from telegram import Bot

        target_chat = int(chat_id)
        expected_yes = f"hitl:{interaction_id}:yes"
        expected_no = f"hitl:{interaction_id}:no"

        async def poll() -> _ClientReply | None:
            from telegram.error import Conflict

            offset = update_offset
            async with Bot(self._bot_token) as bot:
                while True:
                    remaining = deadline_monotonic - time.monotonic()
                    if remaining <= 0:
                        return None
                    poll_timeout = max(1, min(50, int(remaining)))
                    try:
                        updates = await asyncio.wait_for(
                            bot.get_updates(
                                offset=offset,
                                timeout=poll_timeout,
                                allowed_updates=_ALLOWED_UPDATES,
                                read_timeout=poll_timeout + 5,
                            ),
                            timeout=remaining + 5,
                        )
                    except TimeoutError:
                        return None
                    except Conflict as exc:
                        # Telegram allows one getUpdates consumer per bot token; a second poller
                        # (e.g. another orchestrator clone sharing it) yields HTTP 409. Surface a
                        # clear diagnosis — the notifier maps it to a transport_error and fails
                        # closed — instead of letting presses vanish into the other consumer.
                        raise RuntimeError(
                            "another process is consuming getUpdates for this bot token "
                            "(Telegram 409 Conflict); only one poller may run per bot token"
                        ) from exc
                    for update in updates:
                        # Always advance past a consumed update: re-fetching a near-miss forever
                        # would spin the loop to the deadline. We never advance *silently* — a
                        # callback in our chat is acknowledged and logged first, so a stale
                        # or duplicate press stays visible to the operator, not dropped.
                        offset = update.update_id + 1
                        if kind == "question":
                            msg = update.message
                            if (
                                msg is None
                                or msg.chat.id != target_chat
                                or msg.reply_to_message is None
                                or msg.reply_to_message.message_id != prompt_message_id
                                or not isinstance(msg.text, str)
                                or not msg.text.strip()
                            ):
                                continue
                            return _ClientReply(text=msg.text.strip())
                        query = update.callback_query
                        if query is None:
                            continue  # not a callback (e.g. a stray message); nothing to ack
                        cbmsg = query.message
                        chat_match = cbmsg is not None and cbmsg.chat.id == target_chat
                        data = query.data
                        if (
                            chat_match
                            and cbmsg is not None
                            and cbmsg.message_id == prompt_message_id
                            and data in (expected_yes, expected_no)
                        ):
                            approved = data == expected_yes
                            await self._ack(
                                bot, query.id, _ACK_APPROVED if approved else _ACK_DENIED
                            )
                            return _ClientReply(
                                text="approved" if approved else "denied",
                                approved=approved,
                            )
                        # A press we cannot act on. If it is (or might be) ours, acknowledge it so
                        # the operator gets feedback instead of "nothing happened", and log why.
                        if chat_match or cbmsg is None:
                            reason = (
                                "message_none"
                                if cbmsg is None
                                else "unexpected_data"
                                if data not in (expected_yes, expected_no)
                                else "wrong_message_id"
                            )
                            _LOG.warning(
                                "telegram callback near-miss (%s): acknowledged but not actionable",
                                reason,
                                extra={
                                    "logfmt_fields": {
                                        "interaction_id": interaction_id,
                                        "reason": reason,
                                        "got_data": str(data),
                                        "got_message_id": str(
                                            cbmsg.message_id if cbmsg is not None else None
                                        ),
                                        "expected_message_id": str(prompt_message_id),
                                        "chat_match": str(chat_match),
                                    }
                                },
                            )
                            await self._ack(bot, query.id, _ACK_STALE, alert=True)
                        else:
                            # Foreign chat: never acknowledge another chat's callback.
                            _LOG.warning(
                                "telegram callback from a foreign chat ignored",
                                extra={"logfmt_fields": {"interaction_id": interaction_id}},
                            )

        with _suppress_transport_request_logs():
            return _run_sync(poll)


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
