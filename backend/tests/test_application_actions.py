from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.api.router import (
    _allowed_application_actions,
    application_detail,
    delete_application,
    pause_application,
    reset_application,
    resume_application,
    retry_application,
    select_application_cv,
    start_application,
)
from app.applications.state_machine import transition_application
from app.applications.orchestrator import orchestrator
from app.browser.manager import browser_manager
from app.models import (
    Application, ApplicationAnswer, ApplicationQuestionRequest, ApplicationStep,
    CandidateDocument, CandidateProfile, FormSnapshot, Job,
)
from app.schemas.api import ApplicationCVSelectionRequest


@pytest.fixture(autouse=True)
def clear_orchestrator_mutation_locks():
    orchestrator._mutation_locks.clear()
    yield
    orchestrator._mutation_locks.clear()


def objects(db):
    candidate = CandidateProfile(name="Candidate", data={})
    job = Job(
        source="pracuj", source_job_id="actions", canonical_url="https://example.test/jobs/actions",
        fingerprint="f" * 64, content_hash="c" * 64, title="Support", company="Example",
        requirements=[], nice_to_have=[], responsibilities=[], compact_json={}, raw_metadata={},
    )
    db.add_all([candidate, job]); db.flush()
    document = CandidateDocument(
        profile_id=candidate.id, kind="CV", version=1, original_name="candidate.pdf",
        storage_path="C:/safe/candidate.pdf", sha256="d" * 64, extracted_text="Candidate",
    )
    application = Application(candidate_id=candidate.id, job_id=job.id)
    db.add_all([document, application]); db.commit()
    return candidate, job, document, application


@pytest.mark.asyncio
async def test_create_without_cv_cannot_start_then_selection_makes_it_start_capable(db, monkeypatch):
    candidate, _job, document, application = objects(db)
    started = []
    monkeypatch.setattr(orchestrator, "start", lambda application_id: started.append(application_id))

    assert _allowed_application_actions(application) == ["select_cv", "delete"]
    with pytest.raises(HTTPException, match="valid candidate CV"):
        await start_application(application.id, db)

    selected = await select_application_cv(
        application.id, ApplicationCVSelectionRequest(selected_cv_document_id=document.id), db,
    )
    fetched = application_detail(application.id, db)
    assert selected["selected_cv_document_id"] == document.id
    assert fetched["selected_cv_document_id"] == document.id
    assert "start" in fetched["allowed_actions"]

    result = await start_application(application.id, db)
    assert result["accepted"] is True
    assert result["status"] == "OPENING"
    assert started == [application.id]


@pytest.mark.asyncio
async def test_double_start_returns_existing_active_run_without_duplicate_workflow(db, monkeypatch):
    _candidate, _job, document, application = objects(db)
    application.selected_cv_document_id = document.id; db.commit()
    started = []
    monkeypatch.setattr(orchestrator, "start", lambda application_id: started.append(application_id))

    first = await start_application(application.id, db)
    second = await start_application(application.id, db)
    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["already_running"] is True
    assert started == [application.id]


@pytest.mark.asyncio
async def test_cv_selection_rejects_document_from_another_candidate(db):
    candidate, _job, _document, application = objects(db)

    other = CandidateProfile(name="Other", data={}); db.add(other); db.flush()
    wrong = CandidateDocument(
        profile_id=other.id, kind="CV", version=1, original_name="other.pdf",
        storage_path="C:/safe/other.pdf", sha256="e" * 64, extracted_text="Other",
    )
    db.add(wrong); db.commit()
    with pytest.raises(HTTPException, match="selected document"):
        await select_application_cv(
            application.id, ApplicationCVSelectionRequest(selected_cv_document_id=wrong.id), db,
        )


@pytest.mark.asyncio
async def test_pause_persists_paused_state_and_resume_targets_same_application(db, monkeypatch):
    _candidate, _job, document, application = objects(db)
    application.selected_cv_document_id = document.id
    application.browser_page_id = "page-owned-by-application"
    transition_application(db, application, "OPENING", "started")
    db.commit()
    paused = []
    resumed = []

    async def fake_pause(application_id):
        paused.append(application_id)

    async def fake_browser_resume():
        return None

    monkeypatch.setattr(orchestrator, "pause", fake_pause)
    monkeypatch.setattr(orchestrator, "resume", lambda application_id: resumed.append(application_id))
    monkeypatch.setattr(browser_manager, "resume", fake_browser_resume)

    result = await pause_application(application.id, db)
    assert result["status"] == "PAUSED"
    assert db.get(Application, application.id).status == "PAUSED"
    assert db.get(Application, application.id).browser_page_id == "page-owned-by-application"
    assert paused == [application.id]
    assert "resume" in _allowed_application_actions(application)

    await resume_application(application.id, db)
    assert resumed == [application.id]
    assert db.get(Application, application.id).selected_cv_document_id == document.id


@pytest.mark.asyncio
async def test_retry_reuses_current_page_instead_of_restarting_application(db, monkeypatch):
    _candidate, _job, _document, application = objects(db)
    application.browser_page_id = "same-page"
    transition_application(db, application, "OPENING", "started")
    transition_application(db, application, "FAILED", "field_failed")
    db.commit()
    calls = []

    async def fake_browser_resume():
        return None

    monkeypatch.setattr(browser_manager, "resume", fake_browser_resume)
    monkeypatch.setattr(orchestrator, "resume", lambda application_id: calls.append(("resume", application_id)))
    monkeypatch.setattr(orchestrator, "start", lambda application_id: calls.append(("start", application_id)))

    await retry_application(application.id, db)
    assert calls == [("resume", application.id)]
    assert db.get(Application, application.id).browser_page_id == "same-page"


@pytest.mark.asyncio
async def test_delete_route_is_idempotent_and_preserves_candidate_and_cv(db, monkeypatch):
    candidate, _job, document, application = objects(db)
    application_id = application.id

    async def fake_release(_application_id):
        return None

    monkeypatch.setattr(orchestrator, "release", fake_release)
    first = await delete_application(application_id, db)
    second = await delete_application(application_id, db)
    assert first.status_code == 204 and second.status_code == 204
    assert db.get(Application, application_id) is None
    assert db.get(CandidateProfile, candidate.id) is not None
    assert db.get(CandidateDocument, document.id) is not None


@pytest.mark.asyncio
async def test_reset_preserves_selected_cv_and_returns_start_capable_from_needs_user(db, monkeypatch):
    _candidate, job, document, application = objects(db)
    application.selected_cv_document_id = document.id
    transition_application(db, application, "OPENING", "start")
    transition_application(db, application, "FORM_DISCOVERY", "opened")
    transition_application(db, application, "NEEDS_USER", "question")
    db.add(ApplicationAnswer(
        application_id=application.id, field_id="f1", question="Question", value="x", source="TEST",
    ))
    db.add(ApplicationQuestionRequest(
        application_id=application.id, candidate_id=application.candidate_id,
        field_id="f2", question="Missing",
    ))
    db.add(FormSnapshot(
        application_id=application.id, url=job.canonical_url, host="example.test",
        semantic_form={}, fingerprint="s" * 64,
    ))
    db.commit()

    async def fake_release(_application_id):
        return False

    monkeypatch.setattr(orchestrator, "release", fake_release)
    result = await reset_application(application.id, db)
    assert result["status"] == "READY"
    assert result["selected_cv_document_id"] == document.id
    assert "start" in result["allowed_actions"]
    assert db.query(ApplicationAnswer).filter_by(application_id=application.id).count() == 0
    assert db.query(ApplicationQuestionRequest).filter_by(application_id=application.id).count() == 0
    assert db.query(FormSnapshot).filter_by(application_id=application.id).count() == 0


@pytest.mark.asyncio
async def test_reset_ready_application_preserves_selected_cv_and_start_action(db, monkeypatch):
    _candidate, _job, document, application = objects(db)
    application.selected_cv_document_id = document.id
    db.commit()

    async def fake_release(_application_id):
        return False

    monkeypatch.setattr(orchestrator, "release", fake_release)
    result = await reset_application(application.id, db)
    assert result["status"] == "READY"
    assert result["selected_cv_document_id"] == document.id
    assert "start" in result["allowed_actions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["READY", "NEEDS_USER", "PAUSED", "FAILED"])
async def test_delete_succeeds_from_recoverable_lifecycle_states(db, monkeypatch, state):
    _candidate, _job, document, application = objects(db)
    application.selected_cv_document_id = document.id
    if state != "READY":
        transition_application(db, application, "OPENING", "start")
        if state == "NEEDS_USER":
            transition_application(db, application, "NEEDS_USER", "question")
        elif state == "PAUSED":
            transition_application(db, application, "PAUSED", "pause")
        else:
            transition_application(db, application, "FAILED", "failure")
    db.commit()

    async def fake_release(_application_id):
        return False

    monkeypatch.setattr(orchestrator, "release", fake_release)
    await delete_application(application.id, db)
    assert db.get(Application, application.id) is None


@pytest.mark.asyncio
async def test_delete_refuses_database_removal_when_worker_shutdown_is_unverified(db, monkeypatch):
    _candidate, _job, _document, application = objects(db)

    async def failed_release(_application_id):
        raise RuntimeError("browser already closed")

    monkeypatch.setattr(orchestrator, "release", failed_release)
    with pytest.raises(HTTPException) as error:
        await delete_application(application.id, db)
    assert error.value.status_code == 409
    assert db.get(Application, application.id) is not None
    assert application.id not in orchestrator._deleting


@pytest.mark.asyncio
async def test_delete_cancels_and_awaits_active_worker_before_database_removal(db):
    _candidate, _job, _document, application = objects(db)
    application_id = application.id
    started = __import__('asyncio').Event()
    stopped = __import__('asyncio').Event()

    async def worker():
        started.set()
        try:
            await __import__('asyncio').Event().wait()
        finally:
            stopped.set()

    task = __import__('asyncio').create_task(worker())
    orchestrator.tasks[application_id] = task
    orchestrator.runtime[application_id] = __import__('types').SimpleNamespace(page_id='already-closed-page')
    await started.wait()
    await delete_application(application_id, db)
    assert stopped.is_set()
    assert task.done()
    assert db.get(Application, application_id) is None
    assert application_id not in orchestrator.tasks
    assert application_id not in orchestrator._deleting


@pytest.mark.asyncio
async def test_delete_removes_owned_rows_with_sqlite_foreign_keys_and_preserves_other_application(db, monkeypatch):
    db.execute(text("PRAGMA foreign_keys = ON"))
    candidate, job, document, application = objects(db)
    second_job = Job(
        source="pracuj", source_job_id="actions-2", canonical_url="https://example.test/jobs/actions-2",
        fingerprint="a" * 64, content_hash="b" * 64, title="QA", company="Example",
        requirements=[], nice_to_have=[], responsibilities=[], compact_json={}, raw_metadata={},
    )
    db.add(second_job); db.flush()
    other = Application(candidate_id=candidate.id, job_id=second_job.id, selected_cv_document_id=document.id)
    db.add(other); db.flush()
    transition_application(db, application, "OPENING", "start")
    db.add(ApplicationAnswer(
        application_id=application.id, field_id="f1", question="Question", value="x", source="TEST",
    ))
    db.add(ApplicationQuestionRequest(
        application_id=application.id, candidate_id=candidate.id, field_id="f2", question="Missing",
    ))
    db.add(FormSnapshot(
        application_id=application.id, url=job.canonical_url, host="example.test",
        semantic_form={}, fingerprint="z" * 64,
    ))
    db.commit()

    async def active_release(application_id):
        assert application_id == application.id
        return True

    monkeypatch.setattr(orchestrator, "release", active_release)
    await delete_application(application.id, db)
    assert db.get(Application, application.id) is None
    assert db.get(Application, other.id) is not None
    assert db.get(CandidateProfile, candidate.id) is not None
    assert db.get(CandidateDocument, document.id) is not None
    assert db.query(ApplicationAnswer).filter_by(application_id=application.id).count() == 0
    assert db.query(ApplicationQuestionRequest).filter_by(application_id=application.id).count() == 0
    assert db.query(FormSnapshot).filter_by(application_id=application.id).count() == 0
    assert db.query(ApplicationStep).filter_by(application_id=application.id).count() == 0


def test_frontend_delete_contract_handles_204_and_refreshes_card():
    root = Path(__file__).parents[2]
    client = (root / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
    queue = (root / "frontend" / "src" / "pages" / "ApplicationQueue.tsx").read_text(encoding="utf-8")
    assert "response.status === 204" in client
    assert "del(`/applications/${id}`)" in queue
    assert 'ConfirmAction label="DELETE APPLICATION"' in queue
    assert 'window.confirm' not in queue
    assert "setQueryData<Application[]>" in queue
    assert "/select-cv" in (root / "frontend" / "src" / "components" / "StartApplicationDialog.tsx").read_text(encoding="utf-8")
    questions = (root / "frontend" / "src" / "components" / "ApplicationQuestions.tsx").read_text(encoding="utf-8")
    assert "singleChoice && choices.length" in questions
    assert 'role="radiogroup"' in questions
