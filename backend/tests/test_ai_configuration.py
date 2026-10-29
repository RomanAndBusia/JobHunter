from pathlib import Path

import httpx
import pytest

from app.ai.diagnostics import ai_health
from app.ai.provider import (
    AIConfigurationError,
    AIProvider,
    AIStructuredOutputError,
    OpenAIResponsesProvider,
    get_ai_provider,
)
from app.config import ENV_FILE, REPOSITORY_ROOT, load_settings, validate_ai_configuration


def write_env(tmp_path: Path, **overrides: str) -> Path:
    values = {
        "AI_RUNTIME": "api",
        "AI_RUNTIME_FALLBACK": "none",
        "JOBHUNTER_AI_PROVIDER": "openai",
        "JOBHUNTER_AI_MODEL": "gpt-6-luna",
        "OPENAI_API_KEY": "test-key",
        "JOBHUNTER_AI_REASONING_EFFORT": "low",
        "JOBHUNTER_AI_MAX_OUTPUT_TOKENS": "1500",
        "JOBHUNTER_BROWSER_AUTOSTART": "false",
    }
    values.update(overrides)
    path = tmp_path / ".env"
    path.write_text("\n".join("%s=%s" % item for item in values.items()) + "\n", encoding="utf-8")
    return path


def test_repository_env_path_is_deterministic():
    assert ENV_FILE == REPOSITORY_ROOT / ".env"
    assert ENV_FILE == Path(__file__).resolve().parents[2] / ".env"


@pytest.mark.parametrize("launch_folder", [REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"])
def test_repository_env_load_is_independent_of_launch_working_directory(monkeypatch, launch_folder):
    monkeypatch.chdir(launch_folder)
    loaded = load_settings(ENV_FILE, environ={})
    assert loaded.env_file == ENV_FILE
    assert loaded.ai_runtime == "api"


def test_api_runtime_uses_openai_provider(tmp_path):
    configured = load_settings(write_env(tmp_path), environ={})
    provider = get_ai_provider(config=configured)
    assert isinstance(provider, OpenAIResponsesProvider)
    assert provider.model == "gpt-6-luna"
    assert provider.endpoint == "https://api.openai.com/v1/responses"
    assert "chatgpt.com" not in provider.endpoint


def test_api_runtime_rejects_chatgpt_codex_backend(tmp_path):
    configured = load_settings(write_env(
        tmp_path,
        JOBHUNTER_OPENAI_BASE_URL="https://chatgpt.com/backend-api/codex",
    ), environ={})
    diagnostic = validate_ai_configuration(configured)
    assert diagnostic.valid is False
    assert diagnostic.error_type == "InvalidOpenAIEndpoint"


def test_codex_runtime_uses_codex_provider(tmp_path):
    from app.ai.codex import CodexCliProvider

    configured = load_settings(write_env(tmp_path, AI_RUNTIME="codex", OPENAI_API_KEY=""), environ={})
    provider = get_ai_provider(config=configured)
    assert isinstance(provider, CodexCliProvider)


def test_missing_api_key_is_precise_and_secret_free(tmp_path):
    configured = load_settings(write_env(tmp_path, OPENAI_API_KEY="  "), environ={})
    diagnostic = validate_ai_configuration(configured)
    assert not diagnostic.valid
    assert diagnostic.error_type == "MissingAPIKey"
    assert diagnostic.error_message == "OPENAI_API_KEY is not configured."
    with pytest.raises(AIConfigurationError, match="OPENAI_API_KEY is not configured"):
        get_ai_provider(config=configured)


def test_malformed_runtime_with_trailing_backslash_is_rejected(tmp_path):
    configured = load_settings(write_env(tmp_path, AI_RUNTIME="api\\"), environ={})
    diagnostic = validate_ai_configuration(configured)
    assert configured.ai_runtime == "api\\"
    assert not diagnostic.valid
    assert diagnostic.error_message == "Invalid AI_RUNTIME='api\\\\'. Expected 'api' or 'codex'."


def test_startup_rejects_malformed_runtime(monkeypatch, tmp_path):
    from app import main
    from app.config import SettingsError

    configured = load_settings(write_env(tmp_path, AI_RUNTIME="api\\"), environ={})
    monkeypatch.setattr(main, "settings", configured)
    with pytest.raises(SettingsError, match="Invalid AI_RUNTIME"):
        main.validate_startup_configuration()


class CapturingOpenAIProvider(OpenAIResponsesProvider):
    def __init__(self, configured):
        super().__init__(config=configured)
        self.body = None

    async def _post_json(self, body):
        self.body = body
        return {"output_text": '{"ok":true}'}


@pytest.mark.asyncio
async def test_openai_self_test_passes_model_reasoning_and_token_limit(tmp_path):
    configured = load_settings(write_env(tmp_path), environ={})
    provider = CapturingOpenAIProvider(configured)
    assert await provider.self_test()
    assert provider.body["model"] == "gpt-6-luna"
    assert provider.body["reasoning"] == {"effort": "low"}
    assert provider.body["max_output_tokens"] == 128
    assert "Authorization" not in provider.body


class BrokenProvider(AIProvider):
    name = "broken"
    model = "broken-model"

    async def match_jobs(self, candidate, jobs):
        raise RuntimeError("not used")

    async def generate_text(self, task, facts, context, max_length):
        raise RuntimeError("not used")

    async def interpret_form_fragment(self, semantic_context):
        raise RuntimeError("not used")

    async def extract_candidate_profile(self, cv_text):
        raise RuntimeError("not used")

    async def plan_form(self, context):
        raise RuntimeError("not used")

    async def self_test(self):
        raise RuntimeError("secret provider details")


@pytest.mark.asyncio
async def test_provider_exception_is_sanitized_in_health(tmp_path):
    configured = load_settings(write_env(tmp_path), environ={})

    def factory(**_kwargs):
        return BrokenProvider()

    result = await ai_health(config=configured, provider_factory=factory)
    assert result["configuration_valid"] is True
    assert result["provider_reachable"] is False
    assert result["error_type"] == "AIProviderError"
    assert "secret provider details" not in result["error_message"]
    assert "test-key" not in str(result)


@pytest.mark.parametrize("status,code,expected", [
    (401, "invalid_api_key", "invalid_api_key"),
    (403, "permission_denied", "authentication_failed"),
    (429, "insufficient_quota", "insufficient_quota"),
    (429, "rate_limit_exceeded", "rate_limited"),
    (400, "model_not_found", "model_not_available"),
    (400, "invalid_request_error", "bad_request"),
    (500, "server_error", "openai_server_error"),
])
def test_openai_http_error_classification(tmp_path, status, code, expected):
    configured = load_settings(write_env(tmp_path), environ={})
    provider = OpenAIResponsesProvider(config=configured)
    request = httpx.Request("POST", provider.endpoint)
    response = httpx.Response(status, request=request, json={"error": {"code": code}})
    provider.last_request.update({"http_status": status, "request_id": "req_test", "retry_count": 1})
    classified = provider._provider_error(httpx.HTTPStatusError("failed", request=request, response=response))
    assert classified.error_type == expected
    assert classified.details["http_status"] == status
    assert classified.details["request_id"] == "req_test"


@pytest.mark.asyncio
async def test_health_reports_real_inference_metadata(tmp_path):
    configured = load_settings(write_env(tmp_path), environ={})

    class HealthyProvider(CapturingOpenAIProvider):
        async def _post_json(self, body):
            self.body = body
            self.last_request.update({
                "http_status": 200,
                "request_id": "req_health",
                "retry_count": 0,
            })
            return {"output_text": '{"ok":true}'}

    def factory(**_kwargs):
        return HealthyProvider(configured)

    result = await ai_health(config=configured, provider_factory=factory)
    assert result["status"] == "healthy"
    assert result["authenticated"] is True
    assert result["inference"] is True
    assert result["http_status"] == 200
    assert result["request_id"] == "req_health"
    assert result["endpoint"] == "https://api.openai.com/v1/responses"


class SequencedOpenAIProvider(OpenAIResponsesProvider):
    def __init__(self, configured, outputs):
        super().__init__(config=configured)
        self.outputs = list(outputs)
        self.bodies = []

    async def _post_json(self, body):
        self.bodies.append(body)
        self.last_request.update({"http_status": 200, "request_id": "req_structured"})
        return {"output_text": self.outputs.pop(0)}


@pytest.mark.asyncio
async def test_structured_output_gets_exactly_one_repair_attempt(tmp_path):
    configured = load_settings(write_env(tmp_path), environ={})
    provider = SequencedOpenAIProvider(configured, [
        '{"profile":',
        '{"profile":{"languages":[{"language":"English","level":"B2"}]},"facts":[]}',
    ])
    result = await provider.extract_candidate_profile("English B2")
    assert result.profile.languages[0].level == "B2"
    assert len(provider.bodies) == 2
    assert provider.bodies[1]["text"]["format"]["type"] == "json_schema"
    assert provider.bodies[1]["input"].find("validation_errors") >= 0


@pytest.mark.asyncio
async def test_structured_output_failure_is_typed_after_one_repair(tmp_path):
    configured = load_settings(write_env(tmp_path), environ={})
    provider = SequencedOpenAIProvider(configured, ["not json", "still not json"])
    with pytest.raises(AIStructuredOutputError) as raised:
        await provider.extract_candidate_profile("synthetic")
    assert len(provider.bodies) == 2
    assert raised.value.error_type == "ai_structured_output_error"
    assert raised.value.stage == "candidate_profile_extraction.structured_parse"
    assert "synthetic" not in str(raised.value.safe_payload())


@pytest.mark.asyncio
async def test_candidate_extraction_accepts_missing_optional_sections(tmp_path):
    configured = load_settings(write_env(tmp_path), environ={})
    provider = SequencedOpenAIProvider(configured, ['{"profile":{"professional_summary":"Junior engineer"}}'])
    result = await provider.extract_candidate_profile("synthetic")
    assert result.profile.professional_summary == "Junior engineer"
    assert result.profile.employment == []
    assert result.facts == []
    assert len(provider.bodies) == 1


@pytest.mark.asyncio
async def test_candidate_extraction_preserves_valid_sections_when_one_project_is_malformed(tmp_path):
    configured = load_settings(write_env(tmp_path), environ={})
    malformed = (
        '{"profile":{"identity":{"full_name":"Candidate"},'
        '"languages":[{"language":"English","level":"B2"}],'
        '"projects":[{"name":42}]},"facts":[]}'
    )
    provider = SequencedOpenAIProvider(configured, [malformed, malformed])
    result = await provider.extract_candidate_profile("synthetic")
    assert result.profile.identity.full_name == "Candidate"
    assert result.profile.languages[0].level == "B2"
    assert result.profile.projects == []
    assert len(provider.bodies) == 2
