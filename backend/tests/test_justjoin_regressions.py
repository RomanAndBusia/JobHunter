import hashlib
import importlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from pypdf import PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
from test_playwright_forms import page
from app.browser.form_scanner import BrowserFormScanner
from app.browser.executor import BrowserActionExecutor
from app.browser import action_safety
from app.browser.action_safety import FinalSubmitBlocked
from app.candidate import document_upload
from app.models import Application, ApplicationQuestionRequest, CandidateProfile, CandidateFact, QuestionMemory, Job
from app.schemas.api import SemanticField, QuestionAnswer
from app.scanner.justjoin import JustJoinSession
from test_justjoin import normalized


def pdf(path, text):
    writer=PdfWriter();sheet=writer.add_blank_page(width=600,height=800)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    sheet[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    content=DecodedStreamObject();content.set_data(('BT /F1 12 Tf 20 700 Td ('+text+') Tj ET').encode('ascii'))
    sheet[NameObject('/Contents')]=writer._add_object(content)
    with path.open('wb') as output:writer.write(output)


@pytest.mark.asyncio
async def test_pinned_format_conversion_verifies_source_and_reuses_derivative(tmp_path,monkeypatch):
    source=tmp_path/'pinned.docx';source.write_bytes(b'immutable source')
    words='Ada Example Python Linux SQL backend project education Polish English evidence'
    document={'id':5,'path':str(source),'name':source.name,'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'extracted_text':words}
    monkeypatch.setattr(document_upload,'ROOT',tmp_path/'derived')
    exports=[]
    def export(original,destination):exports.append(original);pdf(destination,words)
    monkeypatch.setattr(document_upload,'_word_export',export)
    field=SemanticField(id='f1',question='CV',type='file',metadata={'accept':'application/pdf,.pdf'})
    first=await document_upload.resolve_upload_document(document,field)
    second=await document_upload.resolve_upload_document(document,field)
    assert exports==[source] and first==second
    assert first['source_sha256']==document['sha256'] and first['conversion']=='DOCX_TO_PDF'
    assert source.read_bytes()==b'immutable source'
    source.write_bytes(b'changed')
    with pytest.raises(Exception,match='changed on disk'):await document_upload.resolve_upload_document(document,field)


def test_cv_conversion_rejects_missing_or_different_text(tmp_path):
    path=tmp_path/'wrong.pdf';pdf(path,'Entirely different biography missing all source evidence')
    with pytest.raises(Exception,match='verified'):document_upload.verify_pdf(path,'Ada Python Linux SQL networking backend education English Polish projects')
    with pytest.raises(Exception,match='verified'):document_upload.verify_pdf(path,'')


@pytest.mark.asyncio
async def test_modal_excludes_background_and_scopes_duplicate_name(page):
    await page.set_content('''<form><label>Background email<input name="email"></label><button>1-click Apply</button></form>
      <div role="dialog" aria-modal="true"><form><label>Email<input name="email"></label><button type="submit">Apply</button></form></div>''')
    form,registry=await BrowserFormScanner().scan(page)
    assert [f.question for f in form.fields]==['Email'] and [a['label'] for a in form.actions]==['Apply']
    result=await BrowserActionExecutor(page,registry).execute('fill',form.fields[0],'ada@example.test')
    assert result.success
    assert await page.locator('body > form input').input_value()==''
    assert await page.locator('[role=dialog] input').input_value()=='ada@example.test'


@pytest.mark.asyncio
async def test_react_selected_display_survives_empty_query_and_overlay_ownership(page):
    await page.set_content('''<div role="listbox" id="other"><div role="option">No</div></div>
      <form><label for="answer">Privacy</label><div class="select__control"><div class="select__value-container">
      <div class="select__single-value"></div><input id="answer" role="combobox" aria-controls="owned" aria-expanded="false"></div></div>
      <div role="listbox" id="owned" hidden><div role="option">Yes</div><div role="option">No</div></div></form>
      <script>answer.onclick=()=>{owned.hidden=false;answer.setAttribute('aria-expanded','true')};
      answer.onkeydown=e=>{if(e.key==='Escape'){owned.hidden=true;answer.setAttribute('aria-expanded','false')}};
      owned.onclick=e=>{document.querySelector('.select__single-value').textContent=e.target.textContent;owned.hidden=true;answer.setAttribute('aria-expanded','false')};</script>''')
    form,registry=await BrowserFormScanner().scan(page);field=form.fields[0]
    executor=BrowserActionExecutor(page,registry)
    field=await executor.discover_options(field)
    assert [o.label for o in field.options]==['Yes','No']
    result=await executor.execute('select_option',field,field.options[1].id)
    assert result.success and result.actual_value=='No'
    assert await page.locator('#answer').input_value()==''
    fresh,_=await BrowserFormScanner().scan(page)
    assert fresh.fields[0].value=='No'


@pytest.mark.asyncio
async def test_native_one_click_is_never_entry_and_edit_opens_form(page):
    from app.adapters.justjoin import JustJoinAdapter
    from app.browser.manager import BrowserManager
    url='https://justjoin.it/job-offer/fixture'
    await page.route(url,lambda route:route.fulfill(content_type='text/html',body='''<div>Apply with
      <button type="button" onclick="document.querySelector('#editor').hidden=false">Edit</button>
      <button type="button" onclick="window.submits++">1-click Apply</button></div>
      <div role="dialog" aria-modal="true" id="editor" hidden><form><input name="attachment" type="file"><button>Apply</button></form></div><script>window.submits=0</script>'''))
    await page.goto(url)
    manager=BrowserManager();manager.context=page.context
    target=await JustJoinAdapter().open_loaded_application(manager,page,Job(canonical_url=url,raw_metadata={}))
    assert target.target_type=='justjoin_internal' and await page.locator('#editor').is_visible()
    assert await page.evaluate('window.submits')==0
    assert await page.get_by_role('button',name='1-click Apply',exact=True).get_attribute('data-final-action')=='true'


@pytest.mark.asyncio
async def test_login_wait_resumes_by_observation_without_navigation(monkeypatch):
    session=JustJoinSession()
    page=SimpleNamespace(url='https://justjoin.it/profile/recommendations',evaluate=AsyncMock(side_effect=[{'login':True},{'account':True,'recommendations':True}]),bring_to_front=AsyncMock())
    monkeypatch.setattr('app.scanner.justjoin.asyncio.sleep',AsyncMock())
    waiting=AsyncMock()
    assert await session.wait_for_login(page,waiting,timeout=5)
    assert session.status.state=='LOGGED_IN' and waiting.await_count==1 and page.bring_to_front.await_count==1


@pytest.mark.asyncio
@pytest.mark.parametrize('category',['PREFERENCE','JOB_SPECIFIC_TEXT','FACTUAL','SELF_ASSESSMENT'])
async def test_synthetic_answers_are_local_and_never_global_facts(db,monkeypatch,category):
    api=importlib.import_module('app.api.router')
    monkeypatch.setattr(api,'settings',replace(api.settings,live_application_test=True,allow_final_submit=False))
    monkeypatch.setattr(api.orchestrator,'resume',lambda app_id:True)
    candidate=CandidateProfile(name='Ada',data={'preferences':{}});job=Job(**normalized())
    db.add_all([candidate,job]);db.flush()
    application=Application(candidate_id=candidate.id,job_id=job.id,status='NEEDS_USER');db.add(application);db.flush()
    question=ApplicationQuestionRequest(application_id=application.id,candidate_id=candidate.id,field_id='test',question='Availability',fact_path='preferences.availability',question_category=category);db.add(question);db.commit()
    with pytest.raises(HTTPException):await api.answer_question(question.id,QuestionAnswer(value='Test',scope='CANDIDATE_GLOBAL',synthetic_test=True),db)
    await api.answer_question(question.id,QuestionAnswer(value='Test',scope='APPLICATION_ONLY',synthetic_test=True),db)
    assert question.field_context['synthetic_test'] and question.raw_value=='Test'
    assert candidate.data=={'preferences':{}} and db.query(CandidateFact).count()==0 and db.query(QuestionMemory).count()==0
    from app.forms.answer_engine import AnswerEngine
    memories=AnswerEngine(db,provider_factory=lambda **kw:None,application_id=application.id)._memory(candidate.id)
    assert any(m.source=='SYNTHETIC_TEST' for m in memories.values())
    assert not AnswerEngine(db,provider_factory=lambda **kw:None,application_id=application.id+1)._memory(candidate.id)


@pytest.mark.asyncio
@pytest.mark.parametrize('category',['LEGAL_DECLARATION','REQUIRED_RECRUITMENT_CONSENT'])
async def test_synthetic_answers_reject_personal_legal_and_consent_claims(db,monkeypatch,category):
    api=importlib.import_module('app.api.router');monkeypatch.setattr(api,'settings',replace(api.settings,live_application_test=True,allow_final_submit=False))
    candidate=CandidateProfile(name='Ada',data={});job=Job(**normalized());db.add_all([candidate,job]);db.flush()
    app=Application(candidate_id=candidate.id,job_id=job.id,status='NEEDS_USER');db.add(app);db.flush()
    question=ApplicationQuestionRequest(application_id=app.id,candidate_id=candidate.id,field_id='f1',question='Question',question_category=category);db.add(question);db.commit()
    with pytest.raises(HTTPException) as failure:await api.answer_question(question.id,QuestionAnswer(value=True,synthetic_test=True),db)
    assert failure.value.status_code==422 and question.status=='WAITING_FOR_USER'


@pytest.mark.asyncio
async def test_test_origin_remains_unsubmitable_after_flags_change(db,monkeypatch):
    module=importlib.import_module('app.applications.orchestrator')
    monkeypatch.setattr(action_safety,'settings',replace(action_safety.settings,live_application_test=False,allow_final_submit=True))
    monkeypatch.setattr(module,'SessionLocal',lambda:db);monkeypatch.setattr(db,'close',lambda:None)
    candidate=CandidateProfile(name='Ada',data={});job=Job(**normalized());db.add_all([candidate,job]);db.flush()
    app=Application(candidate_id=candidate.id,job_id=job.id,status='REVIEW_REQUIRED',review_data={'live_application_test':True});db.add(app);db.commit()
    with pytest.raises(FinalSubmitBlocked):await module.ApplicationOrchestrator().submit(app.id)
    assert app.status=='REVIEW_REQUIRED' and app.approved_at is None


def test_seniority_title_beats_description_and_international_is_not_internship():
    from app.jobs.normalizer import _infer_level
    assert _infer_level('Junior Security Engineer','International company with senior teams')=='entry'
    assert _infer_level('Security Engineer','International services') is None


@pytest.mark.asyncio
async def test_duplicate_container_id_does_not_own_textarea(page):
    await page.set_content('<div id="message"><label for="message">Message</label><textarea id="message" name="message"></textarea></div>')
    form, registry = await BrowserFormScanner().scan(page)
    result = await BrowserActionExecutor(page, registry).execute('fill', form.fields[0], 'SYNTHETIC_TEST message')
    assert result.success
    assert await page.locator('textarea').input_value() == 'SYNTHETIC_TEST message'


@pytest.mark.asyncio
async def test_custom_option_with_short_selected_presentation_and_multiline_label(page):
    await page.set_content('''<label for="country">Country</label><div><div class="select__single-value"></div>
      <input id="country" role="combobox" aria-controls="countries" aria-expanded="false"></div>
      <div id="countries" role="listbox" hidden><div role="option" aria-selected="false">Poland<br>+48</div></div>
      <script>country.onclick=()=>{countries.hidden=false;country.setAttribute('aria-expanded','true')};
      country.onkeydown=e=>{if(e.key==='Escape'){countries.hidden=true;country.setAttribute('aria-expanded','false')}};
      countries.onclick=e=>{const o=e.target.closest('[role=option]');o.setAttribute('aria-selected','true');
      document.querySelector('.select__single-value').textContent='+48';countries.hidden=true;country.setAttribute('aria-expanded','false')};</script>''')
    form,registry=await BrowserFormScanner().scan(page)
    executor=BrowserActionExecutor(page,registry)
    field=await executor.discover_options(form.fields[0])
    result=await executor.execute('select_option',field,field.options[0].id)
    assert result.success and result.actual_value=='Poland +48'
    assert await page.locator('#country').get_attribute('aria-expanded')=='false'


def test_value_verification_does_not_accept_partial_option_labels():
    assert not BrowserActionExecutor.values_match('No', 'No sponsorship required')
    assert not BrowserActionExecutor.values_match('Polish', 'Polish and English')


def test_lazy_overlay_closing_is_not_a_form_structure_change():
    from app.forms.identity import structure
    from app.schemas.api import SemanticForm, SemanticOption
    field=SemanticField(id='f1',question='Country',type='combobox',control_kind='COMBOBOX',metadata={'html_id':'country'})
    closed=SemanticForm(name='application',fields=[field])
    opened=closed.model_copy(update={'fields':[field.model_copy(update={'options':[SemanticOption(id='o1',label='Poland')]})]})
    assert structure(opened)==structure(closed)
    assert structure(closed)==structure(closed.model_copy(update={'actions':[{'id':'a1','label':'Clear selections','type':'button'}]}))
    assert structure(closed)!=structure(closed.model_copy(update={'actions':[{'id':'a1','label':'Continue','type':'next'}]}))
    assert structure(closed)!=structure(closed.model_copy(update={'fields':[field.model_copy(update={'required':True})]}))


@pytest.mark.asyncio
async def test_upload_replaced_by_owned_filename_and_remove_action(page,tmp_path):
    path=tmp_path/'cv.txt';path.write_text('CV fixture')
    await page.set_content('''<div role="group" aria-labelledby="upload-label"><label id="upload-label" for="resume">Resume</label><input type="file" id="resume"
      onchange="const name=this.files[0].name;const replacement=document.createElement('div');replacement.setAttribute('role','group');replacement.setAttribute('aria-labelledby','upload-label');this.parentElement.replaceWith(replacement);setTimeout(()=>{const p=document.createElement('p');p.textContent=name;replacement.append(p);const b=document.createElement('button');b.type='button';b.setAttribute('aria-label','Remove file');replacement.append(b)},100)"></div>''')
    form,registry=await BrowserFormScanner().scan(page)
    result=await BrowserActionExecutor(page,registry).execute('upload',form.fields[0],str(path))
    assert result.success and result.actual_value==['cv.txt']
    assert await page.locator('#resume').count()==0


@pytest.mark.asyncio
async def test_session_status_restores_owned_page_after_backend_restart(monkeypatch):
    api=importlib.import_module('app.api.router')
    source=importlib.import_module('app.scanner.justjoin')
    session=JustJoinSession()
    owned=SimpleNamespace(url='https://justjoin.it/profile/recommendations',is_closed=lambda:False,
                          evaluate=AsyncMock(return_value={'account':True,'recommendations':True}))
    monkeypatch.setattr(source,'justjoin_session',session)
    monkeypatch.setattr(api.browser_manager,'context',object())
    monkeypatch.setattr(source.browser_manager,'ensure_started',AsyncMock())
    monkeypatch.setattr(session,'_source_page',AsyncMock(return_value=owned))
    status=await api.justjoin_session_status()
    assert status.state=='LOGGED_IN' and session.page is owned
    session._source_page.assert_awaited_once_with('justjoin-login')


@pytest.mark.asyncio
async def test_unlabelled_switch_owns_adjacent_question_and_reveals_textarea(page):
    await page.set_content('''<form><div><span><span><input type="checkbox"
      onchange="document.querySelector('section').hidden=!this.checked"></span></span><div>Attach a message for the employer</div></div>
      <section hidden><label>Message<textarea name="message"></textarea></label></section></form>''')
    form,registry=await BrowserFormScanner().scan(page)
    assert len(form.fields)==1 and form.fields[0].question=='Attach a message for the employer'
    result=await BrowserActionExecutor(page,registry).execute('check',form.fields[0],True)
    assert result.success and result.actual_value is True
    fresh,_=await BrowserFormScanner().scan(page)
    assert [field.question for field in fresh.fields]==['Attach a message for the employer','Message']


@pytest.mark.asyncio
async def test_source_location_reopens_suggestions_for_restored_same_city(page):
    from app.scanner.justjoin import JustJoinSource
    await page.set_content('''<div id="search"><input placeholder="Location" value="Warszawa"><section></section></div>
      <script>let selected='Warszawa';const input=document.querySelector('input');input.oninput=()=>{
        const choices=document.querySelector('section');choices.replaceChildren();
        if(input.value===selected)return;
        selected=null;
        if(input.value){const row=document.createElement('div');const inner=document.createElement('div');const p=document.createElement('p');p.textContent=input.value;inner.append(p);row.append(inner);
          row.onclick=()=>{selected=p.textContent;input.value=selected;choices.replaceChildren();document.body.dataset.selected=selected};choices.append(row)}
      };</script>''')
    await JustJoinSource.select_location(page.locator('#search'), 'Warszawa')
    assert await page.locator('body').get_attribute('data-selected') == 'Warszawa'
