import logging
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.provider import AIError, get_ai_provider, sanitized_ai_error
from app.config import settings
from app.logging import event
from app.matching.service import MatchService, ProviderFactory
from app.models import AIRequestLog
from app.utils.hashing import stable_hash


logger = logging.getLogger(__name__)
DIAGNOSTIC_JOB_ID = "diagnostic:test"


async def production_ai_smoke(
    db: Session,
    provider_factory: ProviderFactory = get_ai_provider,
) -> Dict[str, Any]:
    """Exercise structured matching and a durable write through production code."""
    result: Dict[str, Any] = {
        "runtime": settings.ai_runtime,
        "model": settings.ai_model if settings.ai_runtime == "api" else "codex-cli-default",
        "status": "failed",
        "api_authentication": False,
        "openai_inference": False,
        "production_ai_service": False,
        "structured_output": False,
        "database_persistence": False,
        "stage": "matching.prompt",
        "request_id": None,
        "http_status": None,
        "response": None,
        "error_type": None,
        "error_message": None,
        "last_check": datetime.utcnow().isoformat() + "Z",
    }
    service = MatchService(db, provider_factory=provider_factory)
    result["model"] = getattr(service.provider, "model", result["model"])
    try:
        result["stage"] = "matching.inference"
        decisions = await service.infer(
            {
                "career_goal": {"primary": "Production diagnostic only"},
                "skills": ["diagnostic"],
            },
            [{
                "id": DIAGNOSTIC_JOB_ID,
                "t": "Diagnostic entry-level IT role",
                "req": ["diagnostic"],
            }],
        )
        request = getattr(service.provider, "last_request", {})
        result.update({
            "api_authentication": request.get("http_status") == 200 if settings.ai_runtime == "api" else True,
            "openai_inference": True,
            "production_ai_service": True,
            "request_id": request.get("request_id"),
            "http_status": request.get("http_status"),
        })
        result["stage"] = "matching.parse"
        if len(decisions) != 1 or decisions[0].id != DIAGNOSTIC_JOB_ID:
            raise ValueError("Diagnostic matching response did not preserve the requested job identity")
        result["structured_output"] = True
        result["response"] = {
            "id": decisions[0].id,
            "ok": decisions[0].ok,
            "reason": decisions[0].reason,
        }

        result["stage"] = "matching.persist"
        diagnostic_key = stable_hash({"operation": "production_ai_smoke", "at": result["last_check"]})
        record = AIRequestLog(
            operation="production_ai_smoke",
            provider=service.provider.name,
            model=getattr(service.provider, "model", result["model"]),
            cache_key=diagnostic_key,
            input_chars=0,
            output_chars=len(str(result["response"])),
            success=True,
        )
        db.add(record)
        db.commit()
        record_id = record.id
        db.expire_all()
        persisted = db.execute(select(func.count()).select_from(AIRequestLog).where(
            AIRequestLog.id == record_id,
            AIRequestLog.cache_key == diagnostic_key,
            AIRequestLog.success.is_(True),
        )).scalar_one()
        if persisted != 1:
            raise RuntimeError("Diagnostic persistence readback failed")
        result.update({
            "database_persistence": True,
            "status": "healthy",
            "stage": "complete",
            "persistence_record_id": record_id,
        })
        event(
            logger, "production_ai_smoke_completed", runtime=settings.ai_runtime,
            model=result["model"], request_id=result["request_id"],
            http_status=result["http_status"], persistence_record_id=record_id,
        )
    except Exception as exc:
        db.rollback()
        error = sanitized_ai_error(exc)
        if not isinstance(exc, AIError):
            error = {
                "type": "production_diagnostic_failed",
                "message": "The production AI smoke test failed. Check the reported stage and backend traceback.",
            }
        result["error_type"] = error["type"]
        result["error_message"] = error["message"]
        result["stage"] = error.get("stage", result["stage"])
        logger.exception(
            "Production AI smoke test failed",
            extra={"context": {
                "runtime": settings.ai_runtime,
                "model": result["model"],
                "endpoint": getattr(service.provider, "last_request", {}).get("endpoint"),
                "module": "app.matching.diagnostics",
                "stage": result["stage"],
                "request_id": getattr(service.provider, "last_request", {}).get("request_id"),
                "exception_class": type(exc).__name__,
            }},
        )
    return result
