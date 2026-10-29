# Contact values in these local HTML fixtures are synthetic.
from dataclasses import replace
from pathlib import Path
import hashlib

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from app.browser.executor import BrowserActionExecutor
from app.browser.challenge import ChallengeDetector
from app.browser.form_scanner import BrowserFormScanner
from app.adapters.generic import GenericAdapter
from app.models import Job
from app.browser.runtime import connect_chrome
from app.browser.chrome import ChromeProcessManager
from app.config import settings


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_challenge_strings_offscreen_and_hidden_stale_elements_do_not_block(page):
    await page.set_content('<h1>Normal application</h1><p>FAQ: verify you are human may be requested later.</p><script src="https://example.test/captcha.js"></script><iframe style="display:none" src="about:blank#recaptcha"></iframe><iframe style="position:absolute;top:3000px" src="about:blank#hcaptcha"></iframe><div id="captcha-old" style="display:none">Verify you are human</div>')
    state=await ChallengeDetector().detect(page)
    assert not state.detected and state.integration_detected


@pytest.mark.asyncio
async def test_form_only_spa_is_not_a_job_description(page):
    from app.jobs.page_extractor import GenericJobPageExtractor
    await page.set_content('<h1>Apply for Support</h1><label>Name<input></label><label>Email<input></label><label>Country<select><option>Country data ' + ('Option ' * 100) + '</option></select></label><input type="file">')
    extracted=await GenericJobPageExtractor().extract(page)
    assert extracted.description == ''


@pytest.mark.asyncio
async def test_unstarted_visible_checkbox_is_passive_until_final_validation(page):
    await page.set_content('<iframe src="about:blank#hcaptcha&frame=checkbox" title="hCaptcha checkbox" style="width:300px;height:80px"></iframe><form><input name="name"></form>')
    detector=ChallengeDetector()
    assert not (await detector.detect(page)).detected
    assert (await detector.detect(page, include_pending=True)).detected


@pytest.mark.asyncio
async def test_visible_challenge_is_bounded_and_resumes_without_reload(page):
    await page.set_content('<div id="challenge-form"><p>Verify you are human</p><button type="button">Manual verification</button></div>')
    detector=ChallengeDetector()
    state=await detector.detect(page)
    assert state.detected and state.state == 'DETECTED_VISIBLE'
    result=await detector.wait_until_clear(page, 1, .02)
    assert result.timed_out and result.final.state == 'TIMEOUT'
    import asyncio
    async def manual_completion():
        await asyncio.sleep(.5)
        await page.evaluate("document.querySelector('#challenge-form').style.display='none'")
    completion = asyncio.create_task(manual_completion())
    result=await detector.wait_until_clear(page, 3, .02)
    await completion
    assert result.cleared and not result.final.detected and result.final.state == 'SOLVED'


@pytest.mark.asyncio
async def test_generated_letter_upload_is_owned_and_never_used_as_cv(page, tmp_path, monkeypatch):
    from app.applications import cover_letters
    monkeypatch.setattr(cover_letters, 'ROOT', tmp_path)
    letter = cover_letters.generated_path(901)
    letter.write_text('Relevant Python project — UTF-8', encoding='utf-8')
    cv = tmp_path / 'pinned.pdf'; cv.write_bytes(b'fixture cv')
    await page.set_content('<form><input type="file" aria-label="CV" accept=".pdf"><input type="file" aria-label="Cover letter" accept=".txt"></form>')
    form, registry = await BrowserFormScanner().scan(page)
    cv_field, letter_field = form.fields
    executor = BrowserActionExecutor(page, registry, application_id=901, selected_document={'id':1,'name':cv.name,'path':str(cv),'sha256':hashlib.sha256(cv.read_bytes()).hexdigest()},
        document_bindings={letter_field.id: {'path': str(letter)}})
    result = await executor.execute('upload', letter_field, 'C:/untrusted.txt')
    assert result.success and result.actual_value == [letter.name]
    result = await executor.execute('upload', cv_field, str(letter))
    assert result.success and result.actual_value == [cv.name]
    executor.document_bindings[letter_field.id] = {'path': str(cv)}
    result = await executor.execute('upload', letter_field, str(cv))
    assert not result.success


@pytest.mark.asyncio
@pytest.mark.parametrize('quick,popup', [(True, False), (False, False), (False, True)])
async def test_pracuj_entry_native_redirect_and_popup_reuse_generic(page, quick, popup):
    from app.adapters.pracuj import PracujAdapter
    from app.browser.manager import BrowserManager
    manager = BrowserManager(); manager.context = page.context
    path = 'https://www.pracuj.pl/praca/fixture,oferta,1'
    destination = 'https://www.pracuj.pl/application/1' if quick else 'https://ats.example.test/apply/1'
    async def route(request):
        if request.request.url == path:
            text = 'Aplikuj szybko' if quick else 'Aplikuj'
            target = ' target="_blank"' if popup else ''
            await request.fulfill(content_type='text/html', body='<header><form><input type="search"></form></header><a href="%s"%s>%s</a>' % (destination, target, text))
        else:
            await request.fulfill(content_type='text/html', body='<form><input aria-label="Email" type="email"><input aria-label="CV" type="file"><button type="submit">Submit</button></form>')
    await page.context.route('**/*', route)
    await page.goto(path)
    job = Job(title='Support', company='Fixture', canonical_url=path, raw_metadata={})
    target = await PracujAdapter().open_loaded_application(manager, page, job)
    assert target.page.url == destination
    assert job.raw_metadata['application_type'] == ('pracuj_quick' if quick else 'external_ats')
    assert job.raw_metadata['final_ats_url'] == (None if quick else destination)
    assert target.page is page if not popup else target.page is not page
    if popup: await target.page.close()


@pytest.mark.asyncio
async def test_generic_composite_phone_rich_text_and_selectize_verify_backing_values(page):
    await page.set_content((FIXTURES / 'universal_widgets.html').read_text(encoding='utf-8'))
    form, registry = await BrowserFormScanner().scan(page)
    executor = BrowserActionExecutor(page, registry)
    phone = next(f for f in form.fields if f.control_kind == 'PHONE')
    result = await executor.execute('fill', phone, '+48 123 456 789')
    assert result.success, result.error
    assert await page.locator('#canonical').input_value() == '+48123456789'
    editor = next(f for f in form.fields if f.control_kind == 'RICH_TEXT')
    result = await executor.execute('fill', editor, 'My Python project is relevant to this role.')
    assert result.success, result.error
    assert await page.locator('#letter').input_value()
    languages = next(f for f in form.fields if f.control_kind == 'MULTI_SELECT')
    assert registry.resolve(languages.id).value == '#lang-query'
    result = await executor.execute('select_option', languages, [o.id for o in languages.options])
    assert result.success, result.error
    assert result.actual_value == ['en', 'pl']
    assert not (await ChallengeDetector().detect(page)).detected


@pytest.mark.asyncio
async def test_semantic_application_entry_uses_unknown_text_and_rejects_submit(page):
    from app.ai.contracts import PagePlan
    from app.browser.page_planner import SemanticPagePlanner
    await page.set_content('<main><h1>Engineer at Example</h1><a href="https://example.test/applications/jobs/42/create">Zainteresowany?</a><form><button type="submit">Final send</button></form></main>')
    class Planner:
        async def plan_page(self, context):
            choice = next(c for c in context['candidates'] if c['href'])
            assert choice['context'].startswith('Engineer')
            return PagePlan(page_kind='VACANCY', action='NAVIGATE_APPLICATION_ENTRY', element_id=choice['id'], confidence='HIGH', reason='Same vacancy destination')
    job = Job(title='Engineer', company='Example', canonical_url='https://example.test/jobs/42')
    selected = await SemanticPagePlanner(Planner()).inspect(page, job)
    assert await selected.inner_text() == 'Zainteresowany?'
    class UnsafePlanner:
        async def plan_page(self, context):
            choice = next(c for c in context['candidates'] if c['submits'])
            return PagePlan(page_kind='FORM', action='NAVIGATE_APPLICATION_ENTRY', element_id=choice['id'], confidence='HIGH', reason='Unsafe')
    with pytest.raises(ValueError, match='unsafe'):
        await SemanticPagePlanner(UnsafePlanner()).inspect(page, job)


@pytest.mark.asyncio
async def test_nested_phone_groups_hidden_tel_and_excludes_global_locale(page):
    await page.set_content('''<form><div><label for="canonical">Phone number</label><div><div><div>
      <button type="button" aria-haspopup="listbox" id="country" onclick="document.querySelector('#opts').hidden=false">Country</button>
      <div><div><div><input type="tel" id="visible" oninput="document.querySelector('#canonical').value='+48'+this.value"></div></div></div>
      </div><input type="tel" id="canonical" style="display:none"></div></div></div>
      <div id="opts" role="listbox" hidden><div role="option" onclick="this.parentElement.hidden=true">Poland +48</div></div>
      <input aria-label="Email"></form><select name="locale"><option>English</option></select>''')
    form, registry = await BrowserFormScanner().scan(page)
    assert len(form.fields) == 2
    phone = next(f for f in form.fields if f.control_kind == 'PHONE')
    assert phone.question == 'Phone number'
    assert registry.resolve(phone.id).metadata['canonical_selector']
    result = await BrowserActionExecutor(page, registry).execute('fill', phone, '+48123456789')
    assert result.success, result.error


@pytest.mark.asyncio
async def test_phone_keeps_already_selected_matching_country_without_reopening(page):
    await page.set_content('''<form><div>
      <label for="phone">Phone number</label>
      <button type="button" aria-label="Country calling code" onclick="window.countryOpened=true">Selected</button>
      <input id="phone" type="tel" name="phone" value="+48">
    </div></form><script>window.countryOpened=false</script>''')
    form, registry = await BrowserFormScanner().scan(page)
    phone = next(field for field in form.fields if field.control_kind == 'PHONE')
    result = await BrowserActionExecutor(page, registry).execute('fill', phone, '+48123456789')
    assert result.success, result.error
    assert await page.evaluate('window.countryOpened') is False
    assert await page.locator('#phone').input_value() == '123456789'


@pytest.mark.asyncio
async def test_phone_deduplicates_equivalent_country_option_nodes(page):
    await page.set_content('''<form><div>
      <label for="phone">Phone number</label>
      <button type="button" id="country" onclick="document.querySelector('#menu').hidden=false">Country</button>
      <div id="menu" hidden><div role="option">Poland +48</div><div role="option">Poland +48</div></div>
      <input id="phone" type="tel" name="phone">
    </div></form><script>
      document.querySelector('#menu').onclick=e=>{if(e.target.matches('[role=option]'))document.querySelector('#menu').hidden=true}
    </script>''')
    form, registry = await BrowserFormScanner().scan(page)
    phone = next(field for field in form.fields if field.control_kind == 'PHONE')
    result = await BrowserActionExecutor(page, registry).execute('fill', phone, '+48123456789')
    assert result.success, result.error
    assert await page.locator('#phone').input_value() == '123456789'


@pytest.mark.asyncio
async def test_visible_active_captcha_is_different_from_passive_badge(page):
    await page.set_content('<form><input aria-label="Name"></form><div class="grecaptcha-badge" style="width:256px;height:60px"><iframe src="about:blank?recaptcha&size=invisible"></iframe></div>')
    passive = await ChallengeDetector().detect(page)
    assert passive.integration_detected and not passive.detected
    await page.set_content('<div id="captcha" style="width:300px;height:300px">Verify you are human</div>')
    assert (await ChallengeDetector().detect(page)).detected


@pytest.mark.asyncio
async def test_unstarted_checkbox_captcha_defers_until_normal_fields_are_filled(page):
    await page.set_content('<form><label>Name<input name="name"></label></form><iframe title="reCAPTCHA" src="about:blank?recaptcha/api2/anchor" style="width:300px;height:80px"></iframe>')
    detector = ChallengeDetector()
    assert not (await detector.detect(page)).detected
    form, registry = await BrowserFormScanner().scan(page)
    assert len(form.fields) == 1
    result = await BrowserActionExecutor(page, registry).execute('fill', form.fields[0], 'Fixture Name')
    assert result.success
    assert (await detector.detect(page, include_pending=True)).detected
    assert await page.locator('[name=name]').input_value() == 'Fixture Name'


@pytest.mark.asyncio
@pytest.mark.parametrize('model_answer', ['CV', 'C:/untrusted/model-selected-file.docx'])
async def test_application_upload_ignores_model_path_and_uses_selected_document(db, page, tmp_path, model_answer):
    from app.models import Application, CandidateDocument, CandidateProfile
    from app.applications.orchestrator import ApplicationOrchestrator
    selected = tmp_path / 'selected-cv-A.pdf'
    selected.write_bytes(b'local fixture document A')
    await page.set_content('<form><input type="file" aria-label="CV"></form>')
    form, registry = await BrowserFormScanner().scan(page)
    candidate = CandidateProfile(name='Fixture', data={}); db.add(candidate); db.flush()
    document = CandidateDocument(profile_id=candidate.id, kind='CV', version=1,
        original_name=selected.name, storage_path=str(selected), sha256=hashlib.sha256(selected.read_bytes()).hexdigest())
    db.add(document); db.flush()
    application = Application(id=123, candidate_id=candidate.id, selected_cv_document_id=document.id)
    binding = await ApplicationOrchestrator()._document_for(db, application)
    executor = BrowserActionExecutor(page, registry, application_id=application.id, selected_document=binding)
    result = await executor.execute('upload', form.fields[0], model_answer)
    assert result.success, result.error
    assert result.actual_value == ['selected-cv-A.pdf']


@pytest.mark.asyncio
async def test_checkbox_choices_are_one_semantic_group_and_verify_the_set(page):
    await page.set_content('<form><fieldset aria-required="true"><legend>Preferred arrangements</legend><label><input type="checkbox" name="arrangement" value="a">Option A</label><label><input type="checkbox" name="arrangement" value="b">Option B</label></fieldset></form>')
    form, registry = await BrowserFormScanner().scan(page)
    assert len(form.fields) == 1
    group = form.fields[0]
    assert group.control_kind == 'CHECKBOX_GROUP'
    result = await BrowserActionExecutor(page, registry).execute('select_option', group, [group.options[1].id])
    assert result.success, result.error
    assert not await page.locator('[value=a]').is_checked()
    assert await page.locator('[value=b]').is_checked()


@pytest.mark.asyncio
async def test_checkbox_group_handles_visible_label_covering_native_input(page):
    await page.set_content('''<form><fieldset><legend>Preferred work model</legend>
      <div style="position:relative"><input id="remote" type="checkbox" name="model" value="remote">
      <label for="remote" style="position:absolute;inset:0">Remote</label></div>
      <div><input id="office" type="checkbox" name="model" value="office"><label for="office">Office</label></div>
    </fieldset></form>''')
    form, registry = await BrowserFormScanner().scan(page)
    group = next(field for field in form.fields if field.control_kind == 'CHECKBOX_GROUP')
    result = await BrowserActionExecutor(page, registry).execute('select_option', group, [group.options[0].id])
    assert result.success, result.error
    assert await page.locator('#remote').is_checked()


@pytest_asyncio.fixture()
async def page(tmp_path):
    manager = ChromeProcessManager(replace(settings, browser_profile=tmp_path / 'chrome-profile'))
    async with async_playwright() as playwright:
        browser, context = await connect_chrome(playwright, manager)
        page = await context.new_page()
        try:
            yield page
        finally:
            await page.close()
            await browser.close()
            await manager.shutdown()


@pytest.mark.asyncio
async def test_autocomplete_selects_semantic_option_and_verifies_real_value(page):
    await page.set_content((FIXTURES / "autocomplete_city.html").read_text(encoding="utf-8"))
    form, registry = await BrowserFormScanner().scan(page)
    city = next(field for field in form.fields if field.question == "City")
    option = next(option for option in city.options if option.label == "Wrocław, dolnośląskie")
    result = await BrowserActionExecutor(page, registry).execute("choose", city, option.id)
    assert result.success
    assert await page.locator("#city").input_value() == "Wrocław, dolnośląskie"
    assert await page.locator("#city").get_attribute("data-selected") == "true"


@pytest.mark.asyncio
async def test_scanner_tracks_iframe_owner_and_visible_step_only(page):
    await page.set_content((FIXTURES / "iframe_form.html").read_text(encoding="utf-8"))
    await page.wait_for_timeout(100)
    iframe_form, _ = await BrowserFormScanner().scan(page)
    assert any(field.question == "Full name" and field.frame.startswith("frame:") for field in iframe_form.fields)

    await page.set_content((FIXTURES / "multi_step.html").read_text(encoding="utf-8"))
    first, registry = await BrowserFormScanner().scan(page)
    assert [field.question for field in first.fields] == ["Email"]
    next_action = next(action for action in first.actions if action["type"] == "next")
    changed = await BrowserActionExecutor(page, registry).click_action(next_action["id"], expected_change=True)
    assert changed.success
    second, _ = await BrowserFormScanner().scan(page)
    assert [field.question for field in second.fields] == ["Motivation"]
    assert any(action["type"] == "submit" for action in second.actions)


@pytest.mark.asyncio
async def test_validation_error_is_mapped_after_failed_step(page):
    await page.set_content((FIXTURES / "validation_recovery.html").read_text(encoding="utf-8"))
    first, registry = await BrowserFormScanner().scan(page)
    next_action = next(action for action in first.actions if action["type"] == "next")
    await BrowserActionExecutor(page, registry).click_action(next_action["id"], expected_change=True)
    recovered, _ = await BrowserFormScanner().scan(page)
    assert any("select a city" in error["text"] for error in recovered.validation_errors)
    assert any(field.question == "City" for field in recovered.fields)


@pytest.mark.asyncio
async def test_cv_file_upload_is_verified_against_live_file_list(page, tmp_path):
    cv = tmp_path / "candidate.docx"
    cv.write_bytes(b"test candidate document")
    await page.set_content('<form><label for="cv">Upload CV</label><input id="cv" name="cv" type="file" required></form>')
    form, registry = await BrowserFormScanner().scan(page)
    field = next(item for item in form.fields if item.type == "file")
    result = await BrowserActionExecutor(page, registry).execute("upload", field, str(cv))
    assert result.success is True
    assert await page.locator("#cv").evaluate("node => node.files[0].name") == "candidate.docx"


@pytest.mark.asyncio
async def test_hidden_ats_file_input_is_discovered_and_verified(page, tmp_path):
    cv = tmp_path / "candidate.pdf"
    cv.write_bytes(b"synthetic pdf")
    await page.set_content(
        '<form><label for="cv">CV or resume</label>'
        '<input id="cv" name="cv" type="file" style="display:none" required>'
        '<button type="button">Upload your CV or resume file</button>'
        '<button type="submit">Send</button></form>'
    )
    form, registry = await BrowserFormScanner().scan(page)
    field = next(item for item in form.fields if item.type == "file")
    result = await BrowserActionExecutor(page, registry).execute("upload", field, str(cv))
    assert result.success is True
    assert await page.locator("#cv").evaluate("node => node.files[0].name") == "candidate.pdf"


@pytest.mark.asyncio
async def test_radix_radio_uses_visible_role_control_and_survives_rerender(page):
    await page.set_content('''
      <p id="availability-label">Kiedy możesz rozpocząć pracę?</p>
      <div id="group" role="radiogroup" aria-labelledby="availability-label">
        <button type="button" role="radio" aria-label="Od zaraz" aria-checked="false" data-state="unchecked"><input type="radio" tabindex="-1" style="opacity:0;pointer-events:none">Od zaraz</button>
        <button type="button" role="radio" aria-label="Za 2 tygodnie" aria-checked="false" data-state="unchecked"><input type="radio" tabindex="-1" style="opacity:0;pointer-events:none">Za 2 tygodnie</button>
      </div>
      <script>
      document.querySelector('#group').addEventListener('click', event => {
        const label=event.target.closest('[role=radio]')?.getAttribute('aria-label'); if(!label)return;
        document.querySelector('#group').innerHTML=['Od zaraz','Za 2 tygodnie'].map(value => `<button type="button" role="radio" aria-label="${value}" aria-checked="${value===label}" data-state="${value===label?'checked':'unchecked'}">${value}</button>`).join('');
      });
      </script>
    ''')
    form, registry = await BrowserFormScanner().scan(page)
    field = next(item for item in form.fields if item.control_kind == "RADIO_GROUP")
    option = next(item for item in field.options if item.label == "Za 2 tygodnie")
    assert len([item for item in form.fields if item.control_kind == "RADIO_GROUP"]) == 1
    result = await BrowserActionExecutor(page, registry).execute("choose", field, option.id)
    assert result.success
    assert await page.get_by_role("radio", name="Za 2 tygodnie").get_attribute("aria-checked") == "true"
    assert await page.locator('input[type="radio"]:checked').count() == 0


@pytest.mark.asyncio
async def test_lazy_custom_dialog_options_are_discovered_selected_and_verified(page):
    await page.set_content('''
      <div><p>Jakie są Twoje oczekiwania finansowe (brutto)?</p>
      <button id="salary" type="button" aria-haspopup="dialog" aria-expanded="false">Wybierz</button></div>
      <script>
      const trigger=document.querySelector('#salary');
      trigger.addEventListener('click',()=>{trigger.setAttribute('aria-expanded','true');const dialog=document.createElement('div');dialog.setAttribute('role','dialog');dialog.innerHTML='<button type="button" role="option">6000–8000 PLN</button><button type="button" role="option">8000–10000 PLN</button>';document.body.append(dialog);dialog.addEventListener('click',e=>{if(e.target.matches('[role=option]')){trigger.textContent=e.target.textContent;trigger.setAttribute('aria-expanded','false');dialog.remove();}})});
      </script>
    ''')
    form, registry = await BrowserFormScanner().scan(page)
    field = next(item for item in form.fields if item.control_kind == "CUSTOM_SELECT")
    assert field.options == []
    executor = BrowserActionExecutor(page, registry)
    discovered = await executor.discover_options(field)
    assert [item.label for item in discovered.options] == ["6000–8000 PLN", "8000–10000 PLN"]
    result = await executor.execute("choose", discovered, discovered.options[0].id)
    assert result.success
    assert await page.locator("#salary").inner_text() == "6000–8000 PLN"


@pytest.mark.asyncio
async def test_role_checkbox_driver_clicks_and_verifies_aria_state(page):
    await page.set_content('''
      <button id="consent" type="button" role="checkbox" aria-checked="false" data-state="unchecked" aria-label="Current recruitment consent">Consent</button>
      <script>document.querySelector('#consent').onclick=e=>{e.currentTarget.setAttribute('aria-checked','true');e.currentTarget.dataset.state='checked'}</script>
    ''')
    form, registry = await BrowserFormScanner().scan(page)
    field = next(item for item in form.fields if item.control_kind == "CONSENT")
    result = await BrowserActionExecutor(page, registry).execute("check", field, True)
    assert result.success
    assert await page.locator("#consent").get_attribute("data-state") == "checked"


@pytest.mark.asyncio
async def test_hidden_inactive_hcaptcha_does_not_trigger_manual_gate(page):
    await page.set_content(
        '<iframe src="about:blank?hcaptcha" style="display:none"></iframe>'
        '<form><label>Email<input name="email"></label></form>'
    )
    state = await ChallengeDetector().detect(page)
    assert state.integration_detected is True
    assert state.detected is False
    assert state.active_visible is False
    assert state.blocking is False


@pytest.mark.asyncio
async def test_visibility_hidden_hcaptcha_is_integration_not_active_challenge(page):
    await page.set_content(
        '<div id="input-captchaToken-undefined" style="width:400px;height:300px">'
        '<iframe src="about:blank?hcaptcha" style="visibility:hidden;width:400px;height:300px"></iframe></div>'
        '<form><label>Name<input name="name"></label></form>'
    )
    state = await ChallengeDetector().detect(page)
    assert state.integration_detected is True
    assert state.detected is False
    form, _registry = await BrowserFormScanner().scan(page)
    assert [field.question for field in form.fields] == ['Name']


@pytest.mark.asyncio
async def test_direct_form_is_detected_without_clicking_final_submit(page):
    await page.set_content(
        '<form><label>Email<input name="email"></label>'
        '<button type="submit">Submit application</button></form>'
        '<script>window.submits=0;document.querySelector("form").addEventListener("submit",e=>{e.preventDefault();window.submits++})</script>'
    )

    class Manager:
        active_application_page_id = None

        def _register(self, _page):
            return "page-test"

    vacancy = Job(canonical_url="https://example.test/job", source="direct", source_job_id="x")
    target = await GenericAdapter().open_loaded_application(Manager(), page, vacancy)
    assert target.target_type == "direct_application_form"
    assert await page.evaluate("window.submits") == 0


def test_cover_letter_toggle_is_classified_separately_from_submit():
    from app.forms.semantic import extract_semantic_html

    form, _registry = extract_semantic_html(
        '<form><button type="button">Write it here instead</button><button type="submit">Send</button></form>'
    )
    assert [action["type"] for action in form.actions] == ["reveal_textarea", "submit"]


@pytest.mark.asyncio
async def test_cdp_persistent_profile_preserves_cookie_across_reconnect(tmp_path):
    configured = replace(settings, browser_profile=tmp_path / 'persistent-chrome')
    manager = ChromeProcessManager(configured)
    async with async_playwright() as playwright:
        browser, first = await connect_chrome(playwright, manager)
        await first.add_cookies([{'name': 'jobhunter-session', 'value': 'keep', 'domain': 'example.test', 'path': '/'}])
        pid = manager.pid; endpoint = manager.endpoint
        await browser.close()
        reconnected, second = await connect_chrome(playwright, manager)
        try:
            assert manager.pid == pid and manager.endpoint == endpoint
            assert any(item['name'] == 'jobhunter-session' for item in await second.cookies())
        finally:
            await reconnected.close()
            await manager.shutdown()

