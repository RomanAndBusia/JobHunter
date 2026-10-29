import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.router import (
    active_direct_application, application_detail, create_direct_application,
    delete_application, new_direct_apply, reset_application,
)
from app.applications.orchestrator import orchestrator
from app.models import Application, ApplicationAnswer, CandidateProfile
from app.schemas.api import DirectApplicationCreate


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    orchestrator._mutation_locks.clear()
    async def release(_id):
        return False
    monkeypatch.setattr(orchestrator, "release", release)
    yield
    orchestrator._mutation_locks.clear()


async def create(db, url="https://example.test/jobs/1"):
    candidate = db.query(CandidateProfile).first()
    if candidate is None:
        candidate = CandidateProfile(name="Test", data={})
        db.add(candidate); db.commit()
    return await create_direct_application(DirectApplicationCreate(candidate_id=candidate.id, job_url=url), db)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["READY", "PAUSED", "ABORTED", "FAILED", "SUBMITTED"])
async def test_active_slot_survives_fresh_session_and_status(db, status):
    item = await create(db)
    row = db.get(Application, item["id"])
    row.status = status; db.commit()
    with Session(db.get_bind()) as reopened:
        assert active_direct_application(row.candidate_id, reopened)["id"] == row.id
        assert active_direct_application(row.candidate_id, reopened)["status"] == status


@pytest.mark.asyncio
async def test_reset_keeps_slot_and_clears_transients_after_reload(db):
    item = await create(db)
    row = db.get(Application, item["id"])
    row.status = "ABORTED"; row.error = "failure"; row.current_step = "1"
    row.current_operation = "Filling E-mail"; row.fallback_level = 3
    db.add(ApplicationAnswer(application_id=row.id, field_id="email", question="Email", value="x", source="TEST"))
    db.commit()
    await reset_application(row.id, db)
    with Session(db.get_bind()) as reopened:
        result = active_direct_application(row.candidate_id, reopened)
        assert result["id"] == row.id and result["status"] == "READY"
        assert result["error"] is None and result["current_step"] is None
        assert result["current_operation"] is None and result["fallback_level"] == 0
        assert result["answers"] == []


@pytest.mark.asyncio
async def test_delete_removes_slot_and_owned_rows_after_reload(db):
    item = await create(db)
    await delete_application(item["id"], db)
    with Session(db.get_bind()) as reopened:
        assert active_direct_application(item["candidate_id"], reopened) is None
        assert reopened.get(Application, item["id"]) is None
    await delete_application(item["id"], db)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [("READY", "ABORTED"), ("PAUSED", "ABORTED"), ("ABORTED", "ABORTED"), ("FAILED", "FAILED"), ("SUBMITTED", "SUBMITTED")])
async def test_new_apply_preserves_history_and_opens_new_slot(db, status, expected):
    item = await create(db)
    row = db.get(Application, item["id"]); row.status = status; db.commit()
    await new_direct_apply(row.id, db)
    with Session(db.get_bind()) as reopened:
        assert active_direct_application(row.candidate_id, reopened) is None
        assert application_detail(row.id, reopened)["status"] == expected
    next_item = await create(db, "https://example.test/jobs/2")
    assert next_item["id"] != row.id
    # A stale/repeated detach cannot clear the new application.
    await new_direct_apply(row.id, db)
    assert active_direct_application(row.candidate_id, db)["id"] == next_item["id"]


@pytest.mark.asyncio
async def test_another_url_requires_explicit_new_apply(db):
    first = await create(db)
    with pytest.raises(HTTPException, match="NEW APPLY"):
        await create(db, "https://example.test/jobs/2")
    assert active_direct_application(first["candidate_id"], db)["id"] == first["id"]
    duplicate = await create(db)
    assert duplicate["id"] == first["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [reset_application, new_direct_apply])
async def test_failed_shutdown_preserves_state_and_slot(db, monkeypatch, action):
    item = await create(db)
    row = db.get(Application, item["id"]); row.status = "ABORTED"; db.commit()
    async def failed(_id):
        raise RuntimeError("worker did not stop")
    monkeypatch.setattr(orchestrator, "release", failed)
    with pytest.raises((HTTPException, RuntimeError)):
        await action(row.id, db)
    db.expire_all()
    assert active_direct_application(row.candidate_id, db)["status"] == "ABORTED"
