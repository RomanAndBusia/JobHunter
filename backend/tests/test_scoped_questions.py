from datetime import datetime, timedelta

import pytest

from app.api.router import answer_question, pending_questions
from app.applications.orchestrator import orchestrator
from app.forms.answer_engine import AnswerEngine
from app.models import Application, ApplicationQuestionRequest, CandidateFact, CandidateProfile, Job, QuestionMemory
from app.schemas.api import QuestionAnswer


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['APPLICATION_ONLY', 'CANDIDATE_GLOBAL', 'JOB_PREFERENCE_DEFAULT'])
async def test_question_scope_controls_persistence_and_resume(db, monkeypatch, scope):
    candidate = CandidateProfile(name='Test', data={})
    job = Job(source='test', source_job_id='1', canonical_url='https://example.test', fingerprint='f', content_hash='h', title='Role')
    db.add_all([candidate, job]); db.flush()
    application = Application(candidate_id=candidate.id, job_id=job.id, status='NEEDS_USER')
    db.add(application); db.flush()
    question = ApplicationQuestionRequest(application_id=application.id, candidate_id=candidate.id,
        field_id='s1:f1', question='Current city?', fact_path='location.city', status='WAITING_FOR_USER')
    db.add(question); db.commit()
    resumed = []
    monkeypatch.setattr(orchestrator, 'resume', lambda app_id: resumed.append(app_id))
    result = await answer_question(question.id, QuestionAnswer(value='Wrocław', scope=scope), db)
    assert result['status'] == 'ANSWERED'
    assert pending_questions(application.id, db) == []
    assert resumed == [application.id]
    assert db.query(QuestionMemory).count() == (0 if scope == 'APPLICATION_ONLY' else 1)
    assert db.query(CandidateFact).count() == (1 if scope == 'CANDIDATE_GLOBAL' else 0)
    engine = AnswerEngine(db, provider_factory=lambda **kw: None, application_id=application.id)
    own = engine._memory(candidate.id)
    assert own['application:%d' % question.id].value == 'Wrocław'
    other = AnswerEngine(db, provider_factory=lambda **kw: None, application_id=999)._memory(candidate.id)
    assert not any(key.startswith('application:') for key in other)
    if scope == 'CANDIDATE_GLOBAL':
        from app.candidate.fact_resolver import CandidateFactResolver
        reused = CandidateFactResolver(candidate.data, db.query(CandidateFact).all()).resolve('location.city')
        assert reused.exists and reused.value == 'Wrocław'


def test_planner_excludes_expired_and_legacy_application_only_memory(db):
    candidate = CandidateProfile(name='Test', data={}); db.add(candidate); db.flush()
    for concept, scope, expires in [('local', 'APPLICATION_ONLY', None), ('expired', 'GLOBAL', datetime.utcnow()-timedelta(days=1))]:
        db.add(QuestionMemory(candidate_id=candidate.id, concept=concept, question_fingerprint=concept,
                             value='private', source='USER_CONFIRMED', confidence=1,
                             scope=scope, confirmed_at=datetime.utcnow(), expires_at=expires))
    db.commit()
    assert AnswerEngine(db, provider_factory=lambda **kw: None)._memory(candidate.id) == {}


def test_expired_and_future_candidate_facts_cannot_ground_answers(db):
    from app.candidate.fact_resolver import CandidateFactResolver
    candidate = CandidateProfile(name='Test', data={'location': {'city': 'Old city', 'country': 'Future'}})
    db.add(candidate); db.flush()
    db.add_all([
        CandidateFact(profile_id=candidate.id, path='location.city', value='Old city', source='USER_CONFIRMED',
                      expires_at=datetime.utcnow()-timedelta(days=1)),
        CandidateFact(profile_id=candidate.id, path='location.country', value='Future', source='USER_CONFIRMED',
                      valid_from=datetime.utcnow()+timedelta(days=1)),
    ]); db.commit()
    assert AnswerEngine(db, provider_factory=lambda **kw: None)._facts(candidate.id) == {}
    resolver = CandidateFactResolver(candidate.data, db.query(CandidateFact).all())
    assert not resolver.resolve('location.city').exists
    assert not resolver.resolve('location.country').exists
