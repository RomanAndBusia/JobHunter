import pytest
import importlib
from types import SimpleNamespace
from pathlib import Path
from test_playwright_forms import page

from app.applications.orchestrator import ApplicationOrchestrator
from app.browser.manager import browser_manager
from app.forms.answer_engine import ProposedAnswer
from app.models import Application, ApplicationQuestionRequest, CandidateProfile, Job


@pytest.mark.asyncio
@pytest.mark.parametrize('fact_path,question_count', [('location.city', 1), ('preferences.salary_expectations', 2)])
async def test_unresolved_fact_does_not_block_other_fields_and_duplicate_fact_is_one_question(db, page, monkeypatch, fact_path, question_count):
    await page.set_content('<form><label>City<input name="city"></label><label>City again<input name="city_confirm"></label><label>Full name<input name="name"></label></form>')
    candidate = CandidateProfile(name='Test', data={})
    job = Job(source='fixture', source_job_id='1', canonical_url='https://example.test', fingerprint='f', content_hash='h', title='Role')
    db.add_all([candidate, job]); db.flush()
    application = Application(candidate_id=candidate.id, job_id=job.id, status='FORM_DISCOVERY', browser_page_id='fixture')
    db.add(application); db.commit()
    class Planner:
        repeat_groups = []
        async def plan(self, candidate, job, fields, **kwargs):
            return [ProposedAnswer(f.id, f.question, 'Ada Example' if f.question == 'Full name' else None,
                                   'USER_ENTERED', 1, needs_user=f.question != 'Full name',
                                   requested_fact_path=fact_path if f.question != 'Full name' else None,
                                   action='fill', execution_confidence='EXACT') for f in fields]
    monkeypatch.setattr(importlib.import_module('app.applications.orchestrator'), 'AnswerEngine', lambda *args, **kwargs: Planner())
    from app.browser.manager import BrowserManager
    manager = BrowserManager(); manager.context = page.context
    application.browser_page_id = await manager.bind_application_page(application.id, page)
    monkeypatch.setattr(importlib.import_module('app.applications.orchestrator'), 'browser_manager', manager)
    async def clear(*args, **kwargs): return True
    workflow = ApplicationOrchestrator()
    monkeypatch.setattr(workflow, '_wait_for_challenge', clear)
    await workflow._process_steps(db, application, page)
    assert await page.locator('[name=name]').input_value() == 'Ada Example'
    assert application.status == 'NEEDS_USER'
    assert db.query(ApplicationQuestionRequest).filter_by(status='WAITING_FOR_USER').count() == question_count
    assert application.error.startswith(str(question_count) + ' answer')


@pytest.mark.asyncio
async def test_progressive_rescan_plans_a_newly_revealed_field(db, page, monkeypatch):
    await page.set_content('''<form><label>Name<input id="name" oninput="if(!document.querySelector('#new')){const l=document.createElement('label');l.innerHTML='City<input id=new>';this.form.append(l)}"></label></form>''')
    candidate = CandidateProfile(name='Test', data={})
    job = Job(source='fixture', source_job_id='2', canonical_url='https://example.test', fingerprint='f', content_hash='h', title='Role')
    db.add_all([candidate, job]); db.flush()
    application = Application(candidate_id=candidate.id, job_id=job.id, status='FORM_DISCOVERY', browser_page_id='fixture')
    db.add(application); db.commit()
    planned = []
    class Planner:
        repeat_groups = []
        async def plan(self, candidate, job, fields, **kwargs):
            planned.append([f.question for f in fields])
            return [ProposedAnswer(f.id, f.question, 'Ada' if f.question == 'Name' else 'Wrocław', 'USER_ENTERED', 1,
                                  action='fill', execution_confidence='EXACT') for f in fields]
    monkeypatch.setattr(importlib.import_module('app.applications.orchestrator'), 'AnswerEngine', lambda *a, **kw: Planner())
    from app.browser.manager import BrowserManager
    manager = BrowserManager(); manager.context = page.context
    application.browser_page_id = await manager.bind_application_page(application.id, page)
    monkeypatch.setattr(importlib.import_module('app.applications.orchestrator'), 'browser_manager', manager)
    async def clear(*args, **kwargs): return True
    workflow = ApplicationOrchestrator(); monkeypatch.setattr(workflow, '_wait_for_challenge', clear)
    await workflow._process_steps(db, application, page)
    assert planned == [['Name'], ['Name', 'City']]
    assert await page.locator('#new').input_value() == 'Wrocław'
    assert application.status == 'REVIEW_REQUIRED'


@pytest.mark.asyncio
async def test_repeat_language_group_expands_rescans_and_verifies_distinct_levels(db, page, monkeypatch):
    await page.set_content((Path(__file__).parent / 'fixtures/repeated_languages.html').read_text(encoding='utf-8'))
    candidate = CandidateProfile(name='Test', data={})
    job = Job(source='fixture', source_job_id='3', canonical_url='https://example.test', fingerprint='f', content_hash='h', title='Role')
    db.add_all([candidate, job]); db.flush()
    application = Application(candidate_id=candidate.id, job_id=job.id, status='FORM_DISCOVERY', browser_page_id='fixture')
    db.add(application); db.commit()
    class Planner:
        repeat_groups = []
        async def plan(self, candidate, job, fields, **kwargs):
            self.repeat_groups = [SimpleNamespace(current_rows=1, desired_rows=2, add_action_id=kwargs['form_actions'][0]['id'])] if len(fields)==2 else []
            result = []
            for index, f in enumerate(fields):
                value = ['English','B2','Polish','Fluent'][index]
                if f.options: value = next(o.id for o in f.options if o.label == value)
                result.append(ProposedAnswer(f.id, f.question, value, 'USER_ENTERED', 1,
                                             action='select_option' if f.options else 'fill', execution_confidence='EXACT'))
            return result
    monkeypatch.setattr(importlib.import_module('app.applications.orchestrator'), 'AnswerEngine', lambda *a, **kw: Planner())
    from app.browser.manager import BrowserManager
    manager = BrowserManager(); manager.context = page.context
    application.browser_page_id = await manager.bind_application_page(application.id, page)
    monkeypatch.setattr(importlib.import_module('app.applications.orchestrator'), 'browser_manager', manager)
    async def clear(*args, **kwargs): return True
    workflow=ApplicationOrchestrator(); monkeypatch.setattr(workflow, '_wait_for_challenge', clear)
    await workflow._process_steps(db, application, page)
    assert await page.locator('[name=language_0]').input_value() == 'English'
    assert await page.locator('[name=level_0]').input_value() == 'B2'
    assert await page.locator('[name=language_1]').input_value() == 'Polish'
    assert await page.locator('[name=level_1]').input_value() == 'Fluent'
    assert application.status == 'REVIEW_REQUIRED'


@pytest.mark.asyncio
async def test_matching_existing_value_keeps_confirmed_synthetic_provenance(db, page, monkeypatch):
    from app.models import ApplicationAnswer
    from app.browser.form_scanner import BrowserFormScanner
    from app.forms.identity import field_key
    from app.browser.manager import BrowserManager
    await page.set_content('<form><label>Attach a message<input type="checkbox" name="message" checked></label></form>')
    candidate=CandidateProfile(name='Test',data={})
    job=Job(source='fixture',source_job_id='test-provenance',canonical_url='https://example.test',fingerprint='p',content_hash='p',title='Role')
    db.add_all([candidate,job]);db.flush()
    application=Application(candidate_id=candidate.id,job_id=job.id,status='FORM_DISCOVERY',review_data={'live_application_test':True})
    db.add(application);db.flush()
    form,_=await BrowserFormScanner().scan(page)
    existing=ApplicationAnswer(application_id=application.id,field_id='s1:'+field_key(form.fields[0]),question='Attach a message',value=True,source='USER_ENTERED',verified=True,status='VERIFIED')
    db.add(existing);db.commit()
    class Planner:
        repeat_groups=[]
        async def plan(self,candidate,job,fields,**kwargs):
            return [ProposedAnswer(f.id,f.question,True,'SYNTHETIC_TEST',1,source_paths=['memory:application:1'],action='check',execution_confidence='EXACT') for f in fields]
    module=importlib.import_module('app.applications.orchestrator')
    monkeypatch.setattr(module,'AnswerEngine',lambda *a,**kw:Planner())
    manager=BrowserManager();manager.context=page.context
    application.browser_page_id=await manager.bind_application_page(application.id,page)
    monkeypatch.setattr(module,'browser_manager',manager)
    async def clear(*a,**kw):return True
    workflow=ApplicationOrchestrator();monkeypatch.setattr(workflow,'_wait_for_challenge',clear)
    await workflow._process_steps(db,application,page)
    db.expire(existing)
    assert application.status=='REVIEW_REQUIRED' and existing.verified
    assert existing.source=='SYNTHETIC_TEST' and existing.source_paths==['memory:application:1']
    assert candidate.data=={}
