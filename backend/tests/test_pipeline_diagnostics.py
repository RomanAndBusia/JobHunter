import json
from types import SimpleNamespace

import pytest

from app.ai.contracts import JobEvaluation
from app.ai.provider import AIStructuredOutputError
from app.applications.direct import DirectApplicationService
from app.applications.orchestrator import ApplicationOrchestrator, ApplicationFitReviewRequired
from app.jobs.context import acquire, merge_extraction, completeness, JobDataPending
from app.jobs.page_extractor import GenericJobPageExtractor
from app.matching.service import MatchService
from app.models import Application, CandidateProfile, JobMatch
from app.services.diagnostics import redact, records
from test_matching_and_state import make_job
from test_production_cdp_migration import evaluation


def test_matching_receives_description_and_source_context():
    job = make_job(id=42, compact_json={'title': 'Stale compact title'}, description_normalized='Full rendered requirements ' * 20)
    payload, ids = MatchService._ai_batch([job])
    assert payload[0]['title'] == job.title
    assert payload[0]['description'] == job.description_normalized
    assert payload[0]['source_url'] == job.canonical_url


@pytest.mark.parametrize('value,decision', [('apply', 'APPLY'), ('needs_review', 'NEEDS_REVIEW'), ('SKIP', 'SKIP')])
def test_enum_normalization(value, decision):
    assert evaluation(decision=value).decision == decision
    with pytest.raises(ValueError):
        evaluation(decision=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('transport unavailable'), AIStructuredOutputError('job_matching')])
async def test_ai_failure_is_error_never_review(db, error):
    candidate = CandidateProfile(name='Test', data={'skills': ['Python']}); job = make_job()
    db.add_all([candidate, job]); db.commit()
    class Provider:
        model='test'; name='test'
        async def match_jobs(self, candidate, jobs): raise error
    with pytest.raises(Exception):
        await MatchService(db, lambda **kwargs: Provider()).run(candidate, [job.id])
    db.expire_all()
    assert job.matching_status == 'AI_ERROR'
    assert job.raw_metadata['suitability']['state'] == 'ERROR'
    assert db.query(JobMatch).count() == 0


@pytest.mark.asyncio
async def test_missing_job_data_is_waiting_and_does_not_infer(db):
    candidate=CandidateProfile(name='Test', data={}); db.add(candidate); db.commit()
    job, application, _ = DirectApplicationService().create(db, candidate, 'https://ats.test/apply/42')
    class Page:
        url=job.canonical_url
        async def evaluate(self, script): return {'body': 'Loading', 'title': '', 'description': ''}
    with pytest.raises(JobDataPending):
        await acquire(db, job, Page(), None, timeout=0)
    assert job.matching_status == 'WAITING_FOR_JOB_DATA'
    assert db.query(JobMatch).count() == 0


@pytest.mark.asyncio
async def test_ats_recovers_known_source_without_hostname_overwrite(db):
    candidate=CandidateProfile(name='Test', data={}); db.add(candidate); db.commit()
    source=make_job(compact_json={'application_url': 'https://ats.test/form?req=42'}, description_normalized='Real source vacancy responsibilities and requirements. ' * 10)
    db.add(source); db.commit()
    target, application, _=DirectApplicationService().create(db, candidate, 'https://ats.test/form?req=42')
    class Page:
        url=target.canonical_url
        async def evaluate(self, script): return {'body': 'Loading', 'description': '', 'title': ''}
    await acquire(db, target, Page(), None, timeout=0)
    assert target.title == source.title and target.company == source.company
    assert target.description_normalized == source.description_normalized
    assert target.raw_metadata['original_job_url'] == source.canonical_url
    assert completeness(target)['sufficient']


@pytest.mark.asyncio
async def test_known_company_is_preserved_when_destination_has_only_hostname(db):
    job=make_job(company='Known company')
    class Page:
        url='https://unrelated-ats.test/form'
        async def evaluate(self,script): return {'body':'Loading','title':'','description':''}
    extracted=await GenericJobPageExtractor().extract(Page())
    merge_extraction(job,extracted)
    assert job.company=='Known company' and job.title=='Junior IT Support'


@pytest.mark.asyncio
@pytest.mark.parametrize('direct', [True, False])
@pytest.mark.parametrize('decision', ['APPLY', 'SKIP', 'NEEDS_REVIEW'])
async def test_actual_open_order_and_decision_gate(db, monkeypatch, decision, direct):
    prepares = direct or decision == 'APPLY'
    import importlib
    module = importlib.import_module('app.applications.orchestrator')
    candidate=CandidateProfile(name='Test', data={'skills':['Python']}); db.add(candidate); db.commit()
    job, application, _=DirectApplicationService().create(db, candidate, 'https://example.test/job')
    application.status='OPENING'
    if not direct:
        application.review_data = {'origin': 'campaign'}
    order=[]
    class Page:
        url=job.canonical_url
    page=Page()
    class Manager:
        async def ensure_started(self): pass
        async def new_application_page(self): return page
        def _register(self, page): return 'page-test'
        async def bind_application_page(self, *args): return 'page-test'
        async def resolve_active_application_page(self, *args): return page
    class Adapter:
        async def navigate_job_page(self, *args): order.append('navigate')
        async def open_loaded_application(self, *args): order.append('prepare'); return SimpleNamespace(page=page)
    async def acquired(db, job, page, manager, callback, **kwargs):
        order.append('extract'); job.title='Support'; job.description_normalized='Real requirements ' * 20
    class Matcher:
        def __init__(self, db, **kwargs): pass
        async def run(self, candidate, ids): order.append('infer'); job.matching_status='ACCEPTED' if prepares else 'AI_REVIEW'
        @staticmethod
        def evaluation_for(db, candidate, job): return evaluation(decision=decision).model_dump()
    monkeypatch.setattr(module, 'browser_manager', Manager())
    monkeypatch.setattr(module, 'GenericAdapter', Adapter)
    monkeypatch.setattr(module, 'acquire', acquired)
    monkeypatch.setattr(module, 'MatchService', Matcher)
    owner=ApplicationOrchestrator()
    if prepares: await owner._open(db, application)
    else:
        with pytest.raises(ApplicationFitReviewRequired): await owner._open(db, application)
    assert order == ['navigate','extract','infer'] + (['prepare'] if prepares else [])
    assert application.target_host == 'example.test'
    if direct:
        assert application.review_data['evaluation']['decision'] == decision
        assert application.review_data['suitability_advisory'] is True


def test_redaction_removes_json_secrets_and_form_values():
    value=redact({'cookie':'secret', 'raw_response':json.dumps({'password':'dontlog', 'email':'name@example.test'}),
                  'message':'Bearer sensitive fill("private form value")'})
    text=json.dumps(value)
    assert 'dontlog' not in text and 'name@example' not in text and 'private form value' not in text
    assert 'sensitive' not in text and 'secret' not in value['cookie']
    assert redact({'cookie_count': 12})['cookie_count'] == 12
    assert redact({'cookie_count': 'private-cookie-value'})['cookie_count'] == '[REDACTED]'


@pytest.mark.asyncio
async def test_incomplete_matching_waits_without_calling_provider(db):
    candidate=CandidateProfile(name='Test', data={'skills':['Python']})
    job=make_job(description_normalized='', requirements=[])
    db.add_all([candidate, job]); db.commit()
    class Provider:
        name='test'; model='test'
        async def match_jobs(self, *args): pytest.fail('Incomplete data must not reach inference')
    result=await MatchService(db, lambda **kw: Provider()).run(candidate, [job.id])
    assert result['waiting_for_job_data'] == 1
    assert job.raw_metadata['suitability']['state'] == 'WAITING_FOR_JOB_DATA'


@pytest.mark.asyncio
async def test_empty_candidate_profile_is_an_input_error_not_review(db):
    from app.errors import JobHunterError
    candidate=CandidateProfile(name='Test', data={}); job=make_job()
    db.add_all([candidate, job]); db.commit()
    class Provider:
        name='test'; model='test'
        async def match_jobs(self,*args): pytest.fail('Empty profile must not reach AI')
    with pytest.raises(JobHunterError) as exc:
        await MatchService(db,lambda **kw:Provider()).run(candidate,[job.id])
    assert exc.value.stage == 'matching.input'
    assert job.raw_metadata['suitability']['state'] == 'ERROR'
    assert db.query(JobMatch).count() == 0


@pytest.mark.asyncio
async def test_cache_isolated_by_job_and_old_input_version_invalidated(db):
    from app.ai.provider import MatchDecision
    candidate=CandidateProfile(name='Test', data={'skills':['Python']})
    first=make_job(); second=make_job(source_job_id='2', canonical_url='https://example.test/second', fingerprint='2'*64)
    db.add_all([candidate, first, second]); db.commit()
    class Provider:
        name='test'; model='test'; calls=0
        async def match_jobs(self, candidate, jobs):
            self.calls+=1
            return [MatchDecision(id=j['id'], ok=True, reason='Relevant', blockers=[], score=.85, evaluation=evaluation()) for j in jobs]
    provider=Provider(); service=MatchService(db, lambda **kw: provider)
    await service.run(candidate, [first.id])
    assert not service.evaluation_for(db, candidate, second)
    await service.run(candidate, [second.id])
    assert db.query(JobMatch).count() == 2 and provider.calls == 2
    first.raw_metadata={**first.raw_metadata, 'match_input_version':'old'}; db.commit()
    await service.run(candidate, [first.id])
    assert provider.calls == 3 and db.query(JobMatch).count() == 2


@pytest.mark.asyncio
async def test_persistence_failure_has_staged_error_and_no_review(db, monkeypatch):
    from app.ai.provider import MatchDecision
    from app.errors import JobHunterError
    candidate=CandidateProfile(name='Test', data={'skills':['Python']}); job=make_job()
    db.add_all([candidate,job]); db.commit()
    class Provider:
        name='test'; model='test'
        async def match_jobs(self, candidate, jobs):
            return [MatchDecision(id=jobs[0]['id'], ok=True, reason='Relevant', blockers=[], score=.85, evaluation=evaluation())]
    commit=db.commit; calls=[]
    def failing_commit():
        calls.append(1)
        if len(calls)==2: raise RuntimeError('Controlled disk failure')
        commit()
    monkeypatch.setattr(db,'commit', failing_commit)
    with pytest.raises(JobHunterError) as exc:
        await MatchService(db, lambda **kw: Provider()).run(candidate,[job.id])
    assert exc.value.stage == 'matching.persist'
    assert db.query(JobMatch).count() == 0
    assert any(e.get('persistence_status')=='failed' and e.get('state')=='ERROR' for e in records)


def test_log_snapshot_is_scoped_and_never_borrows_unrelated_challenge(db):
    from app.browser.manager import BrowserManager
    from app.browser.challenge import ChallengeState
    from app.services.pipeline_diagnostics import snapshot
    from app.services.diagnostics import emit
    candidate=CandidateProfile(name='Test', data={}); job=make_job()
    db.add_all([candidate, job]); db.flush()
    application=Application(candidate_id=candidate.id, job_id=job.id, review_data={})
    db.add(application); db.commit()
    manager=BrowserManager(); manager.active_challenge=ChallengeState(True)
    emit('own vacancy', vacancy_id=job.id)
    emit('different vacancy', vacancy_id=job.id+1000)
    result=snapshot(db,application,manager)
    assert result['challenge']['state']=='NONE'
    assert any(e['message']=='own vacancy' for e in result['events'])
    assert not any(e['message']=='different vacancy' for e in result['events'])


@pytest.mark.asyncio
async def test_popup_challenge_owns_its_page_not_last_active_tab(db):
    from app.browser.challenge import ChallengeState
    candidate=CandidateProfile(name='Test',data={}); job=make_job()
    db.add_all([candidate,job]); db.flush()
    application=Application(candidate_id=candidate.id,job_id=job.id,status='OPENING',browser_page_id='old-opener')
    db.add(application); db.commit()
    callback=ApplicationOrchestrator()._challenge_callback(db,application,'OPENING')
    state=ChallengeState(True,url='https://ats.test/challenge',page_id='cdp-popup')
    await callback('waiting',state)
    assert application.browser_page_id=='cdp-popup' and application.status=='CAPTCHA'
    assert application.target_host=='ats.test'
    await callback('timeout',state)
    assert application.browser_page_id=='cdp-popup'
