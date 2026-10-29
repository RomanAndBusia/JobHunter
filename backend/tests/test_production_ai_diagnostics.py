import pytest
from sqlalchemy import select

from app.ai.provider import MatchDecision
from app.matching.diagnostics import DIAGNOSTIC_JOB_ID, production_ai_smoke
from app.models import AIRequestLog


class DiagnosticProvider:
    name = "openai"
    model = "diagnostic-model"
    last_request = {
        "endpoint": "https://api.openai.com/v1/responses",
        "http_status": 200,
        "request_id": "req_diagnostic",
        "response_parsed": True,
    }

    async def match_jobs(self, _candidate, jobs):
        assert jobs[0]["id"] == DIAGNOSTIC_JOB_ID
        return [MatchDecision(
            id=DIAGNOSTIC_JOB_ID,
            ok=True,
            reason="diagnostic structured result",
            blockers=[],
            score=1.0,
        )]


@pytest.mark.asyncio
async def test_production_smoke_uses_matching_service_and_reads_back_persistence(db):
    result = await production_ai_smoke(db, provider_factory=lambda **_kwargs: DiagnosticProvider())

    assert result["status"] == "healthy"
    assert result["api_authentication"] is True
    assert result["openai_inference"] is True
    assert result["production_ai_service"] is True
    assert result["structured_output"] is True
    assert result["database_persistence"] is True
    assert result["response"]["id"] == DIAGNOSTIC_JOB_ID
    db.expire_all()
    persisted = db.execute(select(AIRequestLog).where(
        AIRequestLog.id == result["persistence_record_id"]
    )).scalar_one()
    assert persisted.operation == "production_ai_smoke"
