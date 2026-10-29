import pytest
import inspect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.ai.provider import AIProviderError, MatchDecision, MatchEnvelope
from app.api.router import create_direct_application, start_application
from app.applications.state_machine import transition_application
from app.matching.local_filter import filter_job
from app.matching.service import MatchService
from app.models import Application, CandidateProfile, Job, JobMatch


def make_job(**changes):
    values = dict(
        source="pracuj", source_job_id="1", canonical_url="https://example.test/job",
        fingerprint="f" * 64, content_hash="c" * 64, title="Junior IT Support",
        company="Example", location="Warsaw", description_normalized="Provide Windows and LAN support, resolve service desk tickets, diagnose hardware and software issues, and document incidents. Communicate clearly with colleagues and maintain support records.",
        requirements=[], nice_to_have=[], responsibilities=[], compact_json={}, raw_metadata={},
    )
    values.update(changes)
    return Job(**values)


def test_local_filter_rejects_obvious_senior_but_preserves_unusual_it_title():
    assert filter_job(make_job(title="Senior Java Architect", description_normalized="minimum 8 years Java")).rejected
    assert not filter_job(make_job(title="Workplace Support Specialist", description_normalized="Windows LAN hardware tickets")).rejected


def test_application_state_machine_and_double_submit_constraint(db):
    candidate = CandidateProfile(name="A", data={})
    job = make_job()
    db.add_all([candidate, job]); db.flush()
    app = Application(candidate_id=candidate.id, job_id=job.id)
    db.add(app); db.flush()
    transition_application(db, app, "OPENING", "start")
    transition_application(db, app, "FORM_DISCOVERY", "opened")
    assert app.status == "FORM_DISCOVERY"
    with pytest.raises(ValueError):
        transition_application(db, app, "SUBMITTED", "unsafe")
    db.add(Application(candidate_id=candidate.id, job_id=job.id))
    with pytest.raises(IntegrityError):
        db.commit()


def test_ai_match_schema_rejects_unstructured_data():
    valid = MatchEnvelope.model_validate({"decisions": [{"id": "p:1", "ok": True, "reason": "entry IT", "blockers": [], "score": 0.9}]})
    assert valid.decisions[0].ok
    with pytest.raises(Exception):
        MatchEnvelope.model_validate({"decisions": [{"id": "p:1", "ok": "maybe"}]})
    schema = MatchEnvelope.model_json_schema()
    decision = schema["$defs"]["MatchDecision"]
    assert decision["additionalProperties"] is False
    assert set(decision["required"]) == {"id", "ok", "reason", "blockers", "score"}


class MatchProvider:
    name = "test"
    model = "test-model"
    last_request = {"endpoint": "https://api.openai.com/v1/responses", "request_id": "req_match"}

    def __init__(self, decision_id=None):
        self.decision_id = decision_id
        self.jobs = []

    async def match_jobs(self, _candidate, jobs):
        self.jobs = jobs
        decision_id = self.decision_id or jobs[0]["id"]
        return [MatchDecision(id=decision_id, ok=True, reason="entry IT fit", blockers=[], score=0.8)]


@pytest.mark.asyncio
async def test_matching_injects_missing_job_identity_and_persists_result(db):
    candidate = CandidateProfile(name="Candidate", data={"skills": ["support"]})
    job = make_job(source="direct", compact_json={"title": "Support role"})
    db.add_all([candidate, job])
    db.commit()
    provider = MatchProvider()

    result = await MatchService(db, provider_factory=lambda **_kwargs: provider).run(candidate, [job.id])

    assert provider.jobs[0]["id"] == "job:%d" % job.id
    assert result["ai_accepted"] == 1
    db.expire_all()
    persisted = db.execute(select(JobMatch).where(JobMatch.job_id == job.id)).scalar_one()
    assert persisted.accepted is True
    assert db.get(Job, job.id).matching_status == "ACCEPTED"


@pytest.mark.asyncio
async def test_matching_rejects_response_with_wrong_job_identity(db):
    candidate = CandidateProfile(name="Candidate", data={'skills': ['Support']})
    job = make_job(source="direct", compact_json={"title": "Support role"})
    db.add_all([candidate, job])
    db.commit()

    with pytest.raises(AIProviderError) as raised:
        await MatchService(
            db,
            provider_factory=lambda **_kwargs: MatchProvider("wrong-id"),
        ).run(candidate, [job.id])

    assert raised.value.error_type == "ai_response_validation_failed"
    assert raised.value.stage == "matching.parse"
    assert db.execute(select(JobMatch)).scalars().first() is None


def test_application_start_routes_run_on_the_async_event_loop():
    assert inspect.iscoroutinefunction(create_direct_application)
    assert inspect.iscoroutinefunction(start_application)
