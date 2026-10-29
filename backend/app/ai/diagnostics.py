from typing import Any, Callable, Dict, Optional
from datetime import datetime

from app.ai.provider import AIProvider, get_ai_provider, sanitized_ai_error
from app.config import Settings, settings, validate_ai_configuration


ProviderFactory = Callable[..., AIProvider]


async def ai_health(
    probe: bool = True,
    config: Optional[Settings] = None,
    provider_factory: ProviderFactory = get_ai_provider,
) -> Dict[str, Any]:
    value = config or settings
    validation = validate_ai_configuration(value)
    provider_name = value.ai_provider if value.ai_runtime == "api" else "codex"
    model = value.ai_model if value.ai_runtime == "api" else "codex-cli-default"
    result: Dict[str, Any] = {
        "runtime": value.ai_runtime,
        "provider": provider_name,
        "model": model,
        "reasoning_effort": value.ai_reasoning_effort,
        "max_output_tokens": value.ai_max_output_tokens,
        "api_key_present": bool(value.ai_api_key),
        "configured": validation.valid,
        "authenticated": None,
        "inference": None,
        "endpoint": (value.ai_base_url.rstrip("/") + "/responses") if value.ai_runtime == "api" else None,
        "http_status": None,
        "request_id": None,
        "retry_count": 0,
        "response_parsed": None,
        "status": "not_tested" if validation.valid else "not_configured",
        "last_check": None,
        "configuration_valid": validation.valid,
        "provider_reachable": None,
        "error_type": validation.error_type,
        "error_message": validation.error_message,
    }
    if not validation.valid or not probe:
        return result
    try:
        provider = provider_factory(config=value)
        result["provider"] = provider.name
        result["model"] = getattr(provider, "model", model)
        result["provider_reachable"] = bool(await provider.self_test())
        request = getattr(provider, "last_request", {})
        result.update({key: request.get(key) for key in (
            "endpoint", "http_status", "request_id", "retry_count", "response_parsed"
        ) if key in request})
        result["authenticated"] = True if value.ai_runtime == "api" else None
        result["inference"] = result["provider_reachable"] and result.get("response_parsed", True) is not False
        result["status"] = "healthy" if result["inference"] else "failed"
        result["last_check"] = datetime.utcnow().isoformat() + "Z"
    except Exception as exc:
        error = sanitized_ai_error(exc)
        result["provider_reachable"] = False
        result["error_type"] = error["type"]
        result["error_message"] = error["message"]
        for key in ("endpoint", "http_status", "request_id", "retry_count", "response_parsed"):
            if key in error:
                result[key] = error[key]
        result["authenticated"] = False if error["type"] in {"invalid_api_key", "authentication_failed"} else (
            True if result.get("http_status") is not None else None
        )
        result["inference"] = False
        result["status"] = "failed"
        result["last_check"] = datetime.utcnow().isoformat() + "Z"
    return result
