import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError

from app.applications.direct import DirectApplicationService
from app.browser.manager import BrowserManager, BrowserSessionLostError
from app.browser.form_scanner import BrowserFormScanner
from app.browser.executor import BrowserActionExecutor
from app.forms.answer_engine import AnswerEngine
from app.forms.identity import field_key, structure
from app.forms.language_collection import plan_collections
from app.jobs.context import acquire
from app.models import CandidateProfile, CandidateFact, ApplicationQuestionRequest
from test_playwright_forms import page
from test_answer_engine import add_fact, make_engine, decision, job

FIXTURE = Path(__file__).parent / 'fixtures/server_rendered_application.html'


@pytest.mark.asyncio
async def test_server_rendered_controls_required_validators_and_dependencies(page):
    await page.set_content(FIXTURE.read_text(encoding='utf-8'))
    form, registry = await BrowserFormScanner().scan(page)
    by_label = {f.question:f for f in form.fields}
    assert {'Salutation', 'First name *', 'Last name', 'Email', 'Phone', 'Region', 'Language',
            'Language level', 'Dostępność', 'CV', 'Portfolio attachment'}.issubset(by_label)
    assert by_label['First name *'].required
    assert by_label['Language level'].required
    fraction = next(f for f in form.fields if f.control_kind == 'CHECKBOX_GROUP')
    assert fraction.required and fraction.type == 'multi_select'
    assert [o.label for o in fraction.options] == ['pełny etat','3/4 etatu','1/2 etatu','1/4 etatu','1/5 etatu']
    assert by_label['Dostępność'].required and len(by_label['Dostępność'].options) == 5
    assert len([f for f in form.fields if f.control_kind == 'CONSENT']) == 2
    assert not by_label['Portfolio attachment'].required
    result = await BrowserActionExecutor(page, registry).execute('select_option', by_label['Region'], by_label['Region'].options[1].id)
    assert result.success
    fresh, _ = await BrowserFormScanner().scan(page)
    assert any(f.question == 'District' for f in fresh.fields)
    assert structure(fresh) != structure(form)
    assert field_key(next(f for f in fresh.fields if f.question == 'Dostępność')) == field_key(by_label['Dostępność'])
    assert await page.evaluate('window.submissions') == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('level', [None, 'Fluent', 'B2', 'None'])
async def test_compound_language_requires_exact_level_and_commits_through_ui(db, page, level):
    await page.set_content(FIXTURE.read_text(encoding='utf-8'))
    candidate=CandidateProfile(name='Test',data={'languages':[{'language':'German','level':level}]})
    db.add(candidate);db.flush()
    add_fact(db,candidate,'languages.0.language','German')
    if level is not None: add_fact(db,candidate,'languages.0.level',level,source='USER_CONFIRMED')
    db.commit()
    engine=AnswerEngine(db,provider_factory=lambda **kw:None)
    form, registry=await BrowserFormScanner().scan(page)
    executor=BrowserActionExecutor(page,registry)
    overrides,commits=await plan_collections(engine,candidate,form.fields,executor)
    level_field=next(f for f in form.fields if f.metadata.get('compound_role')=='level')
    assert overrides[level_field.id].needs_user == (level in (None,'Fluent'))
    if commits:
        args=commits[level_field.id]
        for f, option in ((args[0],args[1]),(args[2],args[3])):
            assert (await executor.execute('select_option',f,option)).success
        assert (await executor.commit_language_collection(*args)).success
        assert await page.evaluate('window.adds') == 1
        assert (await executor.commit_language_collection(*args)).success
        assert await page.evaluate('window.adds') == 1
        fresh, fresh_registry=await BrowserFormScanner().scan(page)
        _,commits=await plan_collections(engine,candidate,fresh.fields,BrowserActionExecutor(page,fresh_registry))
        assert not commits
    else:
        assert overrides[level_field.id].requested_fact_path == 'languages.0.level'
        assert await page.evaluate('window.adds') == 0
    assert await page.evaluate('window.submissions') == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('dropdown', [True, False])
async def test_cefr_cannot_be_invented_from_a_language_name(db, dropdown):
    candidate=CandidateProfile(name='Test',data={'languages':[{'language':'German'}]})
    vacancy=job();db.add_all([candidate,vacancy]);db.flush()
    add_fact(db,candidate,'languages.0.language','German');db.commit()
    from app.schemas.api import SemanticField, SemanticOption
    field=SemanticField(id='f1',question='German language level',type='select' if dropdown else 'text',required=True,
                        options=[SemanticOption(id='f1o1',label='B2')] if dropdown else [])
    engine,_=make_engine(db,lambda ctx:[decision('f1','B2',action='select_option' if dropdown else 'fill',paths=['languages.0.language'],option_id='f1o1' if dropdown else None)])
    answer=(await engine.plan(candidate,vacancy,[field]))[0]
    assert answer.needs_user and answer.value is None


class OwnedPage:
    def __init__(self,url,opener=None):
        self.url=url;self.parent=opener;self.closed=False;self.extracts=0;self.handlers={}
    def is_closed(self):return self.closed
    def on(self,event,callback):self.handlers[event]=callback
    async def opener(self):return self.parent
    async def close(self):self.closed=True
    async def evaluate(self,script):
        self.extracts+=1
        return {'title':'Role','description':'Vacancy requirements and responsibilities. '*10,'body':''}


def owned_manager(pages):
    manager=BrowserManager();manager.context=SimpleNamespace(pages=pages)
    return manager


@pytest.mark.asyncio
async def test_closed_extractor_reacquires_owned_popup_and_never_unrelated_active_tab(db):
    candidate=CandidateProfile(name='Test',data={});db.add(candidate);db.commit()
    vacancy,application,_=DirectApplicationService().create(db,candidate,'https://ats.test/job')
    original=OwnedPage(vacancy.canonical_url)
    unrelated=OwnedPage('https://successfactors.example/unrelated')
    popup=OwnedPage('https://ats.test/form',original)
    manager=owned_manager([original,unrelated])
    await manager.bind_application_page(application.id,original)
    manager.active_application_page_id=manager._register(unrelated)
    async def replaced(script):
        original.extracts+=1;original.closed=True;manager.context.pages.append(popup)
        manager.page_openers[id(popup)]=original
        raise PlaywrightError('TargetClosedError: target closed')
    original.evaluate=replaced
    await acquire(db,vacancy,original,manager,application_id=application.id)
    assert original.extracts==1 and popup.extracts==1 and unrelated.extracts==0
    assert await manager.resolve_active_application_page(application.id) is popup


@pytest.mark.asyncio
async def test_missing_owned_target_fails_without_borrowing_same_url_or_global_active():
    original=OwnedPage('https://ats.test/form');other=OwnedPage(original.url)
    manager=owned_manager([original,other]);await manager.bind_application_page(1,original)
    await manager.bind_application_page(2,other)
    original.closed=True
    manager.active_application_page_id=manager._register(other)
    with pytest.raises(BrowserSessionLostError):await manager.resolve_active_application_page(1)
    await manager.release_application_page(1,manager._register(other))
    assert not other.closed
    assert await manager.resolve_active_application_page(2) is other


@pytest.mark.asyncio
async def test_owned_page_unexpected_navigation_is_rejected():
    original=OwnedPage('https://ats.test/form');manager=owned_manager([original])
    await manager.bind_application_page(1,original)
    original.url='https://successfactors.example/unrelated'
    with pytest.raises(BrowserSessionLostError):await manager.resolve_active_application_page(1)


@pytest.mark.asyncio
async def test_direct_incomplete_vacancy_does_not_require_matching_inputs(db):
    candidate=CandidateProfile(name='Test',data={});db.add(candidate);db.commit()
    vacancy,_,_=DirectApplicationService().create(db,candidate,'https://ats.test/form')
    page=OwnedPage(vacancy.canonical_url)
    async def empty(script):return {'body':'','description':'','title':''}
    page.evaluate=empty
    await acquire(db,vacancy,page,None,require_complete=False,timeout=0)
    assert vacancy.description_normalized == ''


@pytest.mark.asyncio
async def test_same_application_resumes_after_unknown_answers_and_preserves_filled_fields(db, page, monkeypatch):
    from app.applications.orchestrator import ApplicationOrchestrator
    from app.models import Application, QuestionMemory
    from app.api.router import answer_question
    from app.schemas.api import QuestionAnswer
    module=importlib.import_module('app.applications.orchestrator')
    api=importlib.import_module('app.api.router')
    await page.set_content('''<form><label>Name<input id="name"></label>
      <label for="german">German language level</label><select id="german" required><option value="">Choose</option><option>B2</option><option>C1</option></select>
      <label for="availability">Availability</label><select id="availability" required><option value="">Choose</option><option>Immediately</option><option>1 month</option></select>
      <button type="submit">Submit</button></form><script>window.submits=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submits++}</script>''')
    candidate=CandidateProfile(name='Test',data={'identity':{'full_name':'Ada Example'}})
    vacancy=job();db.add_all([candidate,vacancy]);db.flush()
    add_fact(db,candidate,'identity.full_name','Ada Example');db.flush()
    application=Application(candidate_id=candidate.id,job_id=vacancy.id,status='FORM_DISCOVERY',review_data={'origin':'direct_url'})
    db.add(application);db.commit();original_id=application.id
    manager=BrowserManager();manager.context=page.context
    application.browser_page_id=await manager.bind_application_page(application.id,page)
    application.target_url=page.url;db.commit()
    def planner(ctx):
        result=[]
        for f in ctx['form']['fields']:
            if f['question']=='Name':result.append(decision(f['id'],'Ada Example',paths=['identity.full_name']))
            else:result.append(decision(f['id'],needs_user=True,fact_path='languages.German.level' if 'German' in f['question'] else 'preferences.availability'))
        return result
    from test_answer_engine import PlanProvider
    provider=PlanProvider(planner)
    monkeypatch.setattr(module,'AnswerEngine',lambda db,**kw:AnswerEngine(db,provider_factory=lambda **opts:provider,**kw))
    monkeypatch.setattr(module,'browser_manager',manager)
    workflow=ApplicationOrchestrator()
    async def clear(*args,**kw):return True
    monkeypatch.setattr(workflow,'_wait_for_challenge',clear)
    await workflow._process_steps(db,application,page)
    assert application.status=='NEEDS_USER'
    assert await page.locator('#name').input_value()=='Ada Example'
    questions=db.query(ApplicationQuestionRequest).filter_by(application_id=application.id,status='WAITING_FOR_USER').all()
    assert len(questions)==2
    resumed=[];monkeypatch.setattr(api.orchestrator,'resume',lambda app_id:resumed.append(app_id))
    for question in questions:
        assert question.field_context['required'] and question.field_context['step']=='1'
        await answer_question(question.id,QuestionAnswer(value='B2' if 'German' in question.question else 'Immediately',scope='APPLICATION_ONLY'),db)
    monkeypatch.setattr(module,'SessionLocal',lambda:db)
    monkeypatch.setattr(db,'close',lambda:None)
    await workflow._resume(application.id)
    assert application.id==original_id and db.query(Application).count()==1
    assert application.status=='REVIEW_REQUIRED'
    assert db.query(ApplicationQuestionRequest).count()==2
    assert db.query(QuestionMemory).count()==0
    assert await page.locator('#name').input_value()=='Ada Example'
    assert await page.locator('#german').input_value()=='B2'
    assert await page.locator('#availability').input_value()=='Immediately'
    assert await page.evaluate('window.submits')==0
    assert resumed==[original_id,original_id]


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['SKIP', 'NEEDS_REVIEW', 'ERROR'])
async def test_explicit_run_reaches_form_discovery_even_with_negative_or_failed_suitability(db,page,monkeypatch,outcome):
    from app.applications.orchestrator import ApplicationOrchestrator
    from app.models import CandidateDocument, ApplicationStep
    module=importlib.import_module('app.applications.orchestrator')
    candidate=CandidateProfile(name='Test',data={'languages':[{'language':'German','level':'None'}]})
    db.add(candidate);db.commit()
    vacancy,application,_=DirectApplicationService().create(db,candidate,'https://ats.test/form')
    document=CandidateDocument(profile_id=candidate.id,kind='CV',version=1,original_name='test.pdf',storage_path='unused-test.pdf',sha256='test')
    db.add(document);db.flush();application.selected_cv_document_id=document.id;db.commit()
    await page.set_content('<form><input aria-label="Name" required></form>')
    manager=BrowserManager();manager.context=page.context
    async def new_page():return page
    monkeypatch.setattr(manager,'new_application_page',new_page)
    class Adapter:
        async def navigate_job_page(self,*args):pass
        async def open_loaded_application(self,*args):
            return SimpleNamespace(page=page,page_id=manager._register(page),target_type='direct_application_form',target_host='ats.test',target_url=page.url)
    class Matcher:
        def __init__(self,*args,**kw):pass
        async def run(self,*args):
            if outcome=='ERROR':raise RuntimeError('test provider unavailable')
        @staticmethod
        def evaluation_for(*args):return {'decision':outcome,'hard_blockers':['German not evidenced']} if outcome!='ERROR' else None
    monkeypatch.setattr(module,'browser_manager',manager)
    monkeypatch.setattr(module,'GenericAdapter',Adapter)
    monkeypatch.setattr(module,'MatchService',Matcher)
    monkeypatch.setattr(module,'SessionLocal',lambda:db)
    monkeypatch.setattr(db,'close',lambda:None)
    workflow=ApplicationOrchestrator();seen=[]
    async def discovery(db,application,page):seen.append(application.status)
    monkeypatch.setattr(workflow,'_process_steps',discovery)
    await workflow._run(application.id)
    assert seen==['FORM_DISCOVERY']
    assert application.review_data['suitability_advisory'] is True
    if outcome!='ERROR':assert application.review_data['evaluation']['decision']==outcome
    assert not db.query(ApplicationStep).filter_by(application_id=application.id,state_to='ABORTED').count()


def test_question_context_migration_preserves_owned_rows(tmp_path):
    from sqlalchemy import create_engine,text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import importlib.util
    source=Path(__file__).parents[1]/'alembic/versions/0009_question_context.py'
    spec=importlib.util.spec_from_file_location('question_context_migration',source)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    engine=create_engine('sqlite:///'+str(tmp_path/'migration.db'))
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE application_question_requests (id INTEGER PRIMARY KEY, raw_value JSON)'))
        connection.execute(text("INSERT INTO application_question_requests VALUES (1, '\"keep\"')"))
        migration.op=Operations(MigrationContext.configure(connection));migration.upgrade()
        row=connection.execute(text('SELECT id,raw_value,field_context FROM application_question_requests')).one()
        assert tuple(row)==(1,'"keep"','{}')
    engine.dispose()


def test_global_language_answer_updates_one_list_item_without_destroying_other_facts(db):
    from app.candidate.facts import store_user_fact
    candidate=CandidateProfile(name='Test',data={'languages':[{'language':'German','level':None},{'language':'English','level':'C1'}],
                                               'employment':[{'company':'Keep'}]})
    db.add(candidate);db.commit()
    store_user_fact(db,candidate,'languages.0.level','B2');db.commit();db.expire_all()
    assert candidate.data['languages']==[{'language':'German','level':'B2'},{'language':'English','level':'C1'}]
    assert candidate.data['employment']==[{'company':'Keep'}]
    with pytest.raises(ValueError):store_user_fact(db,candidate,'languages.Unknown.level','C2')
    db.expire_all()
    assert candidate.data['languages'][0]['level']=='B2'


@pytest.mark.asyncio
async def test_derived_cefr_requires_confirmation(db):
    from app.schemas.api import SemanticField
    candidate=CandidateProfile(name='Test',data={'languages':[{'language':'German','level':'B2'}]})
    vacancy=job();db.add_all([candidate,vacancy]);db.flush()
    add_fact(db,candidate,'languages.0.level','B2',source='CV_DERIVED');db.commit()
    engine,_=make_engine(db,lambda ctx:[decision('f1','B2',paths=['languages.0.level'])])
    answer=(await engine.plan(candidate,vacancy,[SemanticField(id='f1',question='German language level',type='text',required=True)]))[0]
    assert answer.needs_user and answer.value is None
