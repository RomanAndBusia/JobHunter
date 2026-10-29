from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.ai.contracts import JobEvaluation, CoverLetterPlan, CoverLetterClaim
from app.ai.provider import MatchDecision
from app.applications import cover_letters
from app.applications.cover_letters import CoverLetterService, letter_field_mode
from app.config import settings
from app.matching.service import MatchService
from app.models import Application, CandidateProfile, CandidateFact, CandidateDocument, JobMatch, SearchCampaign, SearchPlan, SearchQuery
from app.schemas.api import SemanticField
from app.search.runner import CampaignRunner
from test_matching_and_state import make_job


def test_production_matching_requires_complete_non_null_evaluation():
    from app.ai.provider import ProductionMatchEnvelope
    from pydantic import ValidationError
    row = {'id': 'p:1', 'ok': True, 'reason': 'fit', 'blockers': [], 'score': .8, 'evaluation': None}
    with pytest.raises(ValidationError):
        ProductionMatchEnvelope.model_validate({'decisions': [row]})
    row['evaluation'] = evaluation().model_dump()
    assert ProductionMatchEnvelope.model_validate({'decisions': [row]}).decisions[0].evaluation.decision == 'APPLY'


def test_cover_letter_native_schema_restricts_source_paths_and_field_ids():
    from app.ai.contracts import cover_letter_contract
    from pydantic import ValidationError
    model = cover_letter_contract({'facts': {'skills.0': 'Python'}, 'fields': [{'id': 'letter'}]})
    good = dict(mode='textarea', field_id='letter', opening='', closing='', reason='fit', claims=[{'text': 'Python', 'source_path': 'skills.0'}])
    assert model.model_validate(good).claims[0].source_path == 'skills.0'
    with pytest.raises(ValidationError):
        model.model_validate(dict(good, claims=[{'text': 'made up', 'source_path': 'unknown'}]))
    with pytest.raises(ValidationError):
        model.model_validate(dict(good, field_id='CV'))


def evaluation(**changes):
    values = dict(suitability_score=85, confidence=.9, decision='APPLY', reasons=['Relevant support skills'],
                  matched_skills=['Python'], missing_skills=[], hard_blockers=[], recommended_cover_letter=True)
    values.update({name + '_fit': 'MATCH' for name in ('language', 'experience', 'location', 'work_mode', 'contract', 'seniority')})
    values.update(changes)
    return JobEvaluation(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize('changes,status', [({}, 'ACCEPTED'), ({'hard_blockers': ['Mandatory qualification missing']}, 'REJECTED'),
    ({'confidence': .5}, 'AI_REVIEW'), ({'decision': 'SKIP'}, 'REJECTED')])
async def test_evaluation_enforces_blockers_confidence_and_cache(db, changes, status):
    candidate = CandidateProfile(name='Candidate', data={'skills': ['Python']})
    job = make_job(compact_json={'id': 'p:1'})
    db.add_all([candidate, job]); db.commit()
    class Provider:
        name = 'test'; model = 'strict'; calls = 0
        async def match_jobs(self, candidate, jobs):
            self.calls += 1
            return [MatchDecision(id='p:1', ok=True, reason='Support skills', score=.85, blockers=[], evaluation=evaluation(**changes))]
    provider = Provider()
    service = MatchService(db, lambda **kw: provider)
    await service.run(candidate, [job.id])
    db.expire_all()
    assert db.get(type(job), job.id).matching_status == status
    await service.run(candidate, [job.id])
    assert provider.calls == 1
    assert MatchService.evaluation_for(db, candidate, job)['decision'] == {'ACCEPTED': 'APPLY', 'REJECTED': 'SKIP', 'AI_REVIEW': 'NEEDS_REVIEW'}[status]
    candidate.data = {'skills': ['Rust']}
    assert MatchService.evaluation_for(db, candidate, job) == {}


@pytest.mark.parametrize('question,kind,accept,result', [('CV', 'file', '.txt', None), ('Cover letter', 'file', '.pdf', None),
    ('Cover letter', 'file', '.txt,.pdf', 'txt_attachment'), ('Additional documents', 'file', 'text/plain', 'txt_attachment'),
    ('Cover letter', 'textarea', '', 'textarea')])
def test_letter_upload_policy(question, kind, accept, result):
    field = SemanticField(id='f', question=question, type=kind, metadata={'accept': accept})
    assert letter_field_mode(field) == result


@pytest.mark.asyncio
async def test_cover_letter_is_grounded_cached_utf8_and_prefers_textarea(db, monkeypatch, tmp_path):
    monkeypatch.setattr(cover_letters, 'ROOT', tmp_path)
    monkeypatch.setattr(cover_letters, 'settings', replace(settings, cover_letter_mode='auto'))
    candidate = CandidateProfile(name='Candidate', data={'skills': ['Python — automatyzacja']})
    candidate.facts = [CandidateFact(path='skills.0', value='Python — automatyzacja', source='CV_EXPLICIT', confidence=1)]
    job = make_job()
    db.add_all([candidate, job]); db.flush()
    application = Application(candidate_id=candidate.id, job_id=job.id, candidate=candidate, job=job, review_data={})
    db.add(application); db.commit()
    fields = [SemanticField(id='upload', question='Cover letter', type='file'), SemanticField(id='text', question='Cover letter', type='textarea')]
    class Provider:
        calls = 0
        invalid = False
        async def generate_cover_letter(self, context):
            self.calls += 1
            assert [f['id'] for f in context['fields']] == ['text']
            return CoverLetterPlan(mode='textarea', field_id='text', opening='I have 20 years experience',
                closing='', reason='Relevant project', claims=[CoverLetterClaim(text='Untrusted embellished claim', source_path='skills.missing' if self.invalid else 'skills.0')])
    provider = Provider()
    result = await CoverLetterService().prepare(application, fields, provider)
    assert result['answer'].field_id == 'text'
    assert '20 years' not in result['text']
    assert 'embellished' not in result['text']
    assert 'Python — automatyzacja' in cover_letters.generated_path(application.id).read_text(encoding='utf-8')
    await CoverLetterService().prepare(application, fields, provider)
    assert provider.calls == 1
    application.review_data = {}
    candidate.facts[0].value = 'Different persisted fact'
    candidate.data = {'skills': ['Different persisted fact']}
    provider.invalid = True
    with pytest.raises(ValueError, match='exact persisted fact'):
        await CoverLetterService().prepare(application, fields, provider)
    monkeypatch.setattr(cover_letters, 'settings', replace(settings, cover_letter_mode='off'))
    assert await CoverLetterService().prepare(application, fields, provider) is None


@pytest.mark.asyncio
async def test_campaign_continues_failure_and_deduplicates_on_repeat(db):
    candidate = CandidateProfile(name='Candidate', data={})
    jobs = [make_job(source_job_id=str(i), canonical_url='https://example.test/%d' % i, fingerprint=str(i)*64) for i in (1, 2, 3)]
    db.add_all([candidate] + jobs); db.flush()
    doc = CandidateDocument(profile_id=candidate.id, kind='CV', version=1, active=True, original_name='CV.pdf', storage_path='CV.pdf', sha256='a'*64)
    campaign = SearchCampaign(candidate_id=candidate.id, name='Dry run')
    db.add_all([doc, campaign]); db.flush()
    plan = SearchPlan(campaign_id=campaign.id, rationale={})
    query = SearchQuery(campaign_id=campaign.id, role_family='support', query='IT Support', normalized_query='it support')
    db.add_all([plan, query]); db.commit()
    class Scanner:
        def __init__(self, db): pass
        async def scan(self, profile): return SimpleNamespace(status='COMPLETED', error_count=0, details={'job_ids': [j.id for j in jobs]})
    class Matcher:
        def __init__(self, db): self.db=db
        async def run(self, candidate, ids):
            from app.utils.hashing import profile_hash
            for job in jobs:
                job.matching_status='ACCEPTED'
                if not MatchService.evaluation_for(self.db, candidate, job):
                    self.db.add(JobMatch(job_id=job.id, content_hash=job.content_hash, profile_hash=profile_hash(candidate.data), accepted=True,
                        reason='fit', blockers=[], provider='test', model='test', evaluation=evaluation().model_dump()))
            self.db.commit()
    started=[]
    async def start(application_id, session):
        app = session.get(Application, application_id)
        started.append(application_id)
        assert app.selected_cv_document_id == doc.id
        if len(started)==1: raise RuntimeError('Controlled job failure')
        app.status='REVIEW_REQUIRED'; session.commit()
    runner = CampaignRunner()
    progress = await runner.run(db, campaign, Scanner, Matcher, start)
    assert len(started)==3 and progress['errors']==1
    assert campaign.status=='COMPLETED_WITH_ERRORS'
    assert db.query(Application).count()==3
    await runner.run(db, campaign, Scanner, Matcher, start)
    assert len(started)==3 and db.query(Application).count()==3
