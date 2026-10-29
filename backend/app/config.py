import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlsplit

from dotenv import dotenv_values


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPOSITORY_ROOT / ".env"
_AI_RUNTIMES = {"api", "codex"}
_AI_FALLBACKS = {"none", "api"}
_AI_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


class SettingsError(RuntimeError):
    """A safe, user-facing configuration error that never contains secrets."""


@dataclass(frozen=True)
class AIConfigurationDiagnostic:
    valid: bool
    error_type: Optional[str] = None
    error_message: Optional[str] = None


def _merged_environment(env_file: Path, environ: Mapping[str, str]) -> Mapping[str, str]:
    values = {key: value for key, value in dotenv_values(env_file).items() if value is not None}
    values.update(environ)
    return values


def _text(values: Mapping[str, str], name: str, default: str = "") -> str:
    value = values.get(name, default)
    return str(value).strip()


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    value = values.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safety_boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    if name not in values:
        return default
    raw = str(values[name]).strip().lower()
    if raw not in {'1', 'true', 'yes', 'on', '0', 'false', 'no', 'off'}:
        raise SettingsError('%s must be a boolean.' % name)
    return raw in {'1', 'true', 'yes', 'on'}


def _integer(values: Mapping[str, str], name: str, default: int) -> int:
    raw = _text(values, name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise SettingsError("Invalid %s=%r. Expected an integer." % (name, raw)) from exc


def _floating(values: Mapping[str, str], name: str, default: float) -> float:
    raw = _text(values, name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise SettingsError("Invalid %s=%r. Expected a number." % (name, raw)) from exc


@dataclass(frozen=True)
class Settings:
    database_url: str
    frontend_url: str
    backend_url: str
    log_level: str
    browser_headless: bool
    browser_profile: Path
    browser_slow_mo_ms: int
    browser_autostart: bool
    browser_channel: str
    browser_stealth: bool
    browser_challenge_timeout_seconds: int
    browser_challenge_poll_seconds: float
    ai_provider: str
    ai_runtime: str
    ai_runtime_fallback: str
    ai_model: str
    ai_api_key: str
    ai_reasoning_effort: str
    ai_batch_size: int
    ai_max_output_tokens: int
    ai_base_url: str
    ai_timeout_seconds: float
    ai_max_retries: int
    codex_executable: str
    codex_timeout_seconds: int
    salary_strategy: str
    salary_experience_text_enabled: bool
    salary_fallback_monthly_pln: str
    salary_fallback_hourly_pln: str
    scanner_concurrency: int
    scanner_delay_seconds: float
    http_timeout_seconds: float
    raw_html_retention_hours: int
    auto_accept_required_recruitment_consents: bool
    env_file: Path
    browser_mode: str = "cdp"
    chrome_path: str = ""
    cdp_host: str = "127.0.0.1"
    cover_letter_mode: str = "auto"
    live_application_test: bool = False
    allow_final_submit: bool = True


def load_settings(
    env_file: Path = ENV_FILE,
    environ: Optional[Mapping[str, str]] = None,
) -> Settings:
    """Load the repository-root .env, overlaid by the process environment."""
    source = _merged_environment(Path(env_file), os.environ if environ is None else environ)
    if _text(source, "JOBHUNTER_BROWSER_MODE", "cdp") != "cdp":
        raise SettingsError("JOBHUNTER_BROWSER_MODE must be cdp; direct Chromium startup has been retired.")
    if _text(source, "JOBHUNTER_CDP_HOST", "127.0.0.1") != "127.0.0.1":
        raise SettingsError("JOBHUNTER_CDP_HOST must be 127.0.0.1.")
    letter_mode = _text(source, "JOBHUNTER_COVER_LETTER_MODE", "auto").lower()
    if letter_mode not in {"off", "auto", "always"}:
        raise SettingsError("JOBHUNTER_COVER_LETTER_MODE must be off, auto or always.")
    profile = Path(_text(source, "JOBHUNTER_CHROME_PROFILE", str(REPOSITORY_ROOT / "storage" / "chrome-profile")))
    if not profile.is_absolute():
        profile = REPOSITORY_ROOT / profile
    return Settings(
        database_url=_text(source, "JOBHUNTER_DATABASE_URL", "sqlite:///./jobhunter.db"),
        frontend_url=_text(source, "JOBHUNTER_FRONTEND_URL", "http://127.0.0.1:5173"),
        backend_url=_text(source, "JOBHUNTER_BACKEND_URL", "http://127.0.0.1:8000"),
        log_level=_text(source, "JOBHUNTER_LOG_LEVEL", "INFO"),
        browser_headless=_boolean(source, "JOBHUNTER_BROWSER_HEADLESS", False),
        browser_profile=profile.resolve(),
        browser_slow_mo_ms=_integer(source, "JOBHUNTER_BROWSER_SLOW_MO_MS", 40),
        browser_autostart=_boolean(source, "JOBHUNTER_BROWSER_AUTOSTART", True),
        browser_channel=_text(source, "JOBHUNTER_BROWSER_CHANNEL"),
        browser_stealth=False,
        browser_challenge_timeout_seconds=_integer(source, "JOBHUNTER_CHALLENGE_TIMEOUT_SECONDS", 600),
        browser_challenge_poll_seconds=_floating(source, "JOBHUNTER_CHALLENGE_POLL_SECONDS", 2.0),
        ai_provider=_text(source, "JOBHUNTER_AI_PROVIDER", "openai").lower(),
        ai_runtime=_text(source, "AI_RUNTIME", "api").lower(),
        ai_runtime_fallback=_text(source, "AI_RUNTIME_FALLBACK", "none").lower(),
        ai_model=_text(source, "JOBHUNTER_AI_MODEL", "gpt-5-mini"),
        ai_api_key=_text(source, "OPENAI_API_KEY"),
        ai_reasoning_effort=_text(source, "JOBHUNTER_AI_REASONING_EFFORT", "low").lower(),
        ai_batch_size=_integer(source, "JOBHUNTER_AI_BATCH_SIZE", 15),
        ai_max_output_tokens=_integer(source, "JOBHUNTER_AI_MAX_OUTPUT_TOKENS", 1500),
        ai_base_url=_text(source, "JOBHUNTER_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        ai_timeout_seconds=_floating(source, "JOBHUNTER_OPENAI_TIMEOUT_SECONDS", 90.0),
        ai_max_retries=_integer(source, "JOBHUNTER_OPENAI_MAX_RETRIES", 2),
        codex_executable=_text(source, "JOBHUNTER_CODEX_EXECUTABLE", "codex"),
        codex_timeout_seconds=_integer(source, "JOBHUNTER_CODEX_TIMEOUT_SECONDS", 180),
        salary_strategy=_text(source, "SALARY_STRATEGY", "lowest_available").lower(),
        salary_experience_text_enabled=_boolean(source, "SALARY_EXPERIENCE_TEXT_ENABLED", True),
        salary_fallback_monthly_pln=_text(source, "SALARY_FALLBACK_MONTHLY_PLN"),
        salary_fallback_hourly_pln=_text(source, "SALARY_FALLBACK_HOURLY_PLN"),
        scanner_concurrency=_integer(source, "JOBHUNTER_SCANNER_CONCURRENCY", 2),
        scanner_delay_seconds=_floating(source, "JOBHUNTER_SCANNER_DELAY_SECONDS", 1.2),
        http_timeout_seconds=_floating(source, "JOBHUNTER_HTTP_TIMEOUT_SECONDS", 25.0),
        raw_html_retention_hours=_integer(source, "JOBHUNTER_RAW_HTML_RETENTION_HOURS", 24),
        auto_accept_required_recruitment_consents=_boolean(
            source, "JOBHUNTER_AUTO_ACCEPT_REQUIRED_RECRUITMENT_CONSENTS", False,
        ),
        env_file=Path(env_file).resolve(),
        chrome_path=_text(source, "JOBHUNTER_CHROME_PATH"),
        cover_letter_mode=letter_mode,
        live_application_test=_safety_boolean(source, "LIVE_APPLICATION_TEST", False),
        allow_final_submit=_safety_boolean(source, "ALLOW_FINAL_SUBMIT", True),
    )


def validate_ai_configuration(value: Settings) -> AIConfigurationDiagnostic:
    if value.ai_runtime not in _AI_RUNTIMES:
        return AIConfigurationDiagnostic(
            False,
            "InvalidAIRuntime",
            "Invalid AI_RUNTIME=%r. Expected 'api' or 'codex'." % value.ai_runtime,
        )
    if value.ai_runtime_fallback not in _AI_FALLBACKS:
        return AIConfigurationDiagnostic(
            False,
            "InvalidAIRuntimeFallback",
            "Invalid AI_RUNTIME_FALLBACK=%r. Expected 'none' or 'api'." % value.ai_runtime_fallback,
        )
    if (value.ai_runtime == "api" or value.ai_runtime_fallback == "api") and value.ai_provider != "openai":
        return AIConfigurationDiagnostic(
            False,
            "UnsupportedAIProvider",
            "Invalid JOBHUNTER_AI_PROVIDER=%r. Expected 'openai' for API runtime." % value.ai_provider,
        )
    if not value.ai_model:
        return AIConfigurationDiagnostic(False, "MissingAIModel", "JOBHUNTER_AI_MODEL is empty.")
    if value.ai_reasoning_effort not in _AI_REASONING_EFFORTS:
        return AIConfigurationDiagnostic(
            False,
            "InvalidReasoningEffort",
            "Invalid JOBHUNTER_AI_REASONING_EFFORT=%r." % value.ai_reasoning_effort,
        )
    if value.ai_max_output_tokens <= 0:
        return AIConfigurationDiagnostic(
            False,
            "InvalidMaxOutputTokens",
            "JOBHUNTER_AI_MAX_OUTPUT_TOKENS must be greater than zero.",
        )
    if value.browser_challenge_timeout_seconds <= 0 or value.browser_challenge_poll_seconds <= 0:
        return AIConfigurationDiagnostic(
            False,
            "InvalidBrowserChallengeTiming",
            "Browser challenge timeout and poll interval must be greater than zero.",
        )
    if value.ai_timeout_seconds <= 0 or value.ai_max_retries < 0:
        return AIConfigurationDiagnostic(
            False,
            "InvalidOpenAITransport",
            "OpenAI timeout must be greater than zero and retries cannot be negative.",
        )
    if value.ai_runtime == "api" or value.ai_runtime_fallback == "api":
        endpoint = urlsplit(value.ai_base_url)
        if endpoint.scheme != "https" or endpoint.hostname != "api.openai.com" or endpoint.path.rstrip("/") != "/v1":
            return AIConfigurationDiagnostic(
                False,
                "InvalidOpenAIEndpoint",
                "JOBHUNTER_OPENAI_BASE_URL must be https://api.openai.com/v1 for API runtime.",
            )
    if (value.ai_runtime == "api" or value.ai_runtime_fallback == "api") and not value.ai_api_key:
        return AIConfigurationDiagnostic(False, "MissingAPIKey", "OPENAI_API_KEY is not configured.")
    return AIConfigurationDiagnostic(True)


settings = load_settings()
