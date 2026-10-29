import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from pydantic import ValidationError
from app.scanner.justjoin import canonical_offer_url, offer_identity, parse_posting, classify_login, search_entry
from app.scanner.justjoin_contracts import JustJoinFilters, JustJoinSearchRequest
from app.repositories.jobs import JobRepository
from app.browser import action_safety
from app.browser.action_safety import FinalSubmitBlocked, guard_action
from app.models import CandidateProfile, Job
from app.matching.service import MatchService
from app.ai.provider import MatchDecision
from app.ai.contracts import JobEvaluation


@pytest.mark.parametrize('suffix',['','?promo=lal','/?utm_source=search','#apply'])
def test_canonical_identity_ignores_promotion(suffix):
    url='https://justjoin.it/job-offer/company-soc-warszawa-security'+suffix
    assert canonical_offer_url(url)=='https://justjoin.it/job-offer/company-soc-warszawa-security'
    assert offer_identity(url)=='company-soc-warszawa-security'


@pytest.mark.parametrize('url',['https://evil.test/job-offer/a','https://justjoin.it/job-offers/all-locations','https://justjoin.it.evil.test/job-offer/a'])
def test_identity_rejects_wrong_source(url):
    with pytest.raises(ValueError): canonical_offer_url(url)


def posting_payload(title='SOC Analyst'):
    return {'@type':'JobPosting','title':title,'description':'Monitor SIEM alerts and respond to incidents. '*10,
            'hiringOrganization':{'name':'Example'},'jobLocation':{'address':{'addressLocality':'Warszawa'}},
            'baseSalary':{'currency':'PLN','value':{'minValue':5000,'maxValue':8000,'unitText':'MONTH'}},
            'jobLocationType':'TELECOMMUTE','employmentType':'FULL_TIME'}


def normalized(title='SOC Analyst',slug='soc'):
    return parse_posting('<script type="application/ld+json">'+json.dumps(posting_payload(title))+'</script>',
                         'https://justjoin.it/job-offer/'+slug,
                         {'stack':[{'name':'SIEM','level':'Junior'},{'name':'Python','level':'Nice To Have'}]}).persistence_dict()


def test_rendered_posting_and_preferred_skills():
    payload=normalized()
    assert payload['company']=='Example'
    assert payload['location']=='Warszawa'
    assert payload['work_mode']=='remote'
    assert payload['requirements']==['SIEM: Junior']
    assert payload['nice_to_have']==['Python']
    assert payload['raw_metadata']['salary']['currency']=='PLN'
    assert len(payload['description_normalized'])>160


def test_dedup_merges_source_origins_even_when_unchanged(db):
    repo=JobRepository(db)
    payload=normalized();payload['raw_metadata']['sources_seen']=['search']
    first=repo.upsert(payload);db.commit()
    payload['raw_metadata']['sources_seen']=['recommendations']
    second=repo.upsert(payload);db.commit()
    assert first.job.id==second.job.id
    assert second.disposition=='unchanged'
    assert second.job.raw_metadata['sources_seen']==['search','recommendations']
    third=repo.upsert(normalized(slug='soc-other-city'))
    assert third.job.id!=first.job.id


@pytest.mark.parametrize('url,signals,state',[
    ('https://justjoin.it/',{},'UNKNOWN'),
    ('https://justjoin.it/',{'login':True},'LOGGED_OUT'),
    ('https://sso.rocketjobs.pl/?redirectTo=private',{},'LOGGED_OUT'),
    ('https://justjoin.it/profile/recommendations',{'recommendations':True},'LOGGED_IN'),
    ('https://justjoin.it/profile/recommendations',{'recommendations':True,'password':True},'LOGGED_OUT'),
    ('https://justjoin.it/',{'account':True},'LOGGED_IN'),
])
def test_login_uses_positive_authenticated_evidence(url,signals,state):
    assert classify_login(url,signals)==state


def test_filters_are_typed_and_bounded():
    filters=JustJoinFilters(work_modes=['remote','hybrid'],experience=['intern','junior'],languages=['en'])
    assert search_entry(filters)=='https://justjoin.it/job-offers/all-locations/security'
    with pytest.raises(ValidationError): JustJoinFilters(experience=['expert'])
    with pytest.raises(ValidationError): JustJoinSearchRequest(target_count=1000)
    with pytest.raises(ValidationError): JustJoinFilters(category='../../evil')


@pytest.mark.asyncio
@pytest.mark.parametrize('label',['Submit','Send application','Apply','Finish','Complete','Confirm','Send my CV'])
async def test_dry_run_blocks_unusual_final_actions(label,monkeypatch):
    monkeypatch.setattr(action_safety,'settings',replace(action_safety.settings,live_application_test=True,allow_final_submit=False))
    locator=SimpleNamespace(evaluate=AsyncMock(return_value={'text':label,'in_form':True,'type':'submit','final':False}))
    with pytest.raises(FinalSubmitBlocked): await guard_action(locator,1)


@pytest.mark.asyncio
@pytest.mark.parametrize('label,kind',[('Next','submit'),('Continue','submit'),('Add language','button'),('Write it here','button')])
async def test_dry_run_allows_verified_intermediate_actions(label,kind,monkeypatch):
    monkeypatch.setattr(action_safety,'settings',replace(action_safety.settings,live_application_test=True,allow_final_submit=False))
    locator=SimpleNamespace(evaluate=AsyncMock(return_value={'text':label,'in_form':True,'type':kind,'final':False}))
    assert await guard_action(locator,1) is False


@pytest.mark.asyncio
async def test_explicit_approval_cannot_override_dry_run(monkeypatch):
    monkeypatch.setattr(action_safety,'settings',replace(action_safety.settings,live_application_test=True,allow_final_submit=True))
    with pytest.raises(FinalSubmitBlocked): await guard_action(SimpleNamespace(),1,approved_final=True)


@pytest.mark.asyncio
async def test_matching_sends_singleton_requests_and_preserves_absolute_scores(db):
    candidate=CandidateProfile(name='Fixture',data={'skills':['SIEM']});db.add(candidate)
    repo=JobRepository(db);jobs=[repo.upsert(normalized(slug='soc-'+str(i))).job for i in range(3)];db.commit()
    calls=[]
    class Provider:
        name='fixture';model='fixture';last_request={}
        async def match_jobs(self,profile,batch):
            calls.append(batch)
            evaluation=JobEvaluation(suitability_score=60,confidence=.9,decision='APPLY',reasons=['Fixed rubric'],matched_skills=['SIEM'],missing_skills=[],hard_blockers=[],language_fit='UNKNOWN',experience_fit='MATCH',location_fit='UNKNOWN',work_mode_fit='UNKNOWN',contract_fit='UNKNOWN',seniority_fit='MATCH',recommended_cover_letter=False)
            return [MatchDecision(id=batch[0]['id'],ok=True,score=.6,reason='Fixed rubric',blockers=[],evaluation=evaluation)]
    service=MatchService(db,provider_factory=lambda **kw:Provider())
    await service.run(candidate,[j.id for j in jobs])
    assert len(calls)==3 and all(len(batch)==1 for batch in calls)
    assert [j.evaluation['suitability_score'] for j in jobs]==[60,60,60]
    await MatchService(db,provider_factory=lambda **kw:Provider()).run(candidate,[jobs[0].id])
    assert len(calls)==3
