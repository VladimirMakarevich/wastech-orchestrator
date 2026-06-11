"""Контракт провайдера агента.

Реализация спеки orchestrator_final_plan.md §4.3 (контракт) и §7.1 (классы ошибок).
Core зависит только от этого модуля, не от конкретных адаптеров.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

# --- Канонические перечисления (не дублировать строковыми литералами по коду) ---


class Stage(str, Enum):
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"
    REVIEW = "review"
    FIXING = "fixing"
    PUBLISHING = "publishing"


class RunStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ErrorClass(str, Enum):
    """Нормализованные классы ошибок провайдера (спек §7.1)."""

    BINARY_NOT_FOUND = "binary_not_found"
    UNSUPPORTED_VERSION = "unsupported_version"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_FAILED = "authorization_failed"
    RATE_LIMITED = "rate_limited"
    NETWORK_UNAVAILABLE = "network_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    PROCESS_CRASHED = "process_crashed"
    INVALID_OUTPUT = "invalid_output"
    PERMISSION_DENIED = "permission_denied"
    CONFIGURATION_ERROR = "configuration_error"
    TASK_FAILURE = "task_failure"


# Классы ошибок, безусловно допускающие fallback (спек §7.2).
# authorization_failed / permission_denied — условный fallback, решается Router'ом,
# а не здесь, поэтому в этот набор не входят.
FALLBACK_ELIGIBLE: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.BINARY_NOT_FOUND,
        ErrorClass.UNSUPPORTED_VERSION,
        ErrorClass.AUTHENTICATION_FAILED,
        ErrorClass.RATE_LIMITED,
        ErrorClass.NETWORK_UNAVAILABLE,
        ErrorClass.PROVIDER_UNAVAILABLE,
        ErrorClass.TIMEOUT,
        ErrorClass.PROCESS_CRASHED,
        ErrorClass.INVALID_OUTPUT,
    }
)


# --- Структуры данных контракта (спек §4.3) ---


@dataclass(frozen=True)
class ProviderHealth:
    provider_id: str
    executable_found: bool
    version: str | None
    authenticated: bool
    supports_required_features: bool
    message: str  # диагностика без секретов


@dataclass(frozen=True)
class AgentRunRequest:
    task_id: str
    stage: Stage
    working_directory: str
    prompt: str
    permission_profile: str
    timeout_seconds: int
    attempt: int
    # Пути к артефактам контекста (см. спек §6, §10).
    task_path: str | None = None
    plan_path: str | None = None
    diff_path: str | None = None
    check_artifacts_path: str | None = None
    review_artifacts_path: str | None = None
    output_schema: dict[str, Any] | None = None
    model: str | None = None
    extra_args: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NormalizedError:
    error_class: ErrorClass
    message: str  # без секретов


@dataclass(frozen=True)
class AgentRunResult:
    status: RunStatus
    provider: str
    stage: Stage
    attempt: int
    exit_code: int | None
    started_at: str
    finished_at: str
    final_message: str | None = None
    structured_output: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    session_id: str | None = None  # только для аудита
    stdout_path: str | None = None
    stderr_path: str | None = None
    event_log_path: str | None = None
    error: NormalizedError | None = None


class ProviderError(Exception):
    """Исключение провайдера с нормализованным классом ошибки."""

    def __init__(self, error_class: ErrorClass, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class

    @property
    def is_fallback_eligible(self) -> bool:
        return self.error_class in FALLBACK_ELIGIBLE


# --- Контракт провайдера ---


@runtime_checkable
class AgentProvider(Protocol):
    """Общий интерфейс для Codex и Claude Code.

    Адаптеры реализуют этот протокол. Они НЕ выполняют fallback и НЕ меняют
    state machine — это ответственность Router'а и Core (см. docs/rules/architecture.md).
    """

    id: str

    def preflight(self) -> ProviderHealth:
        """Проверить доступность executable, версию, авторизацию, нужные возможности."""
        ...

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Выполнить один запуск стадии. Инфраструктурные сбои → ProviderError."""
        ...
