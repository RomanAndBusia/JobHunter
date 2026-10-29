"""Narrow Just Join entry/session behavior; all forms use GenericAdapter."""
from urllib.parse import urlsplit
import re
from app.adapters.generic import GenericAdapter
from app.scanner.justjoin import JustJoinSource, justjoin_session
from app.errors import JobHunterError


class LoginPending(JobHunterError):
    def __init__(self):
        super().__init__('LOGIN_REQUIRED', 'Waiting for manual Just Join IT login.', 409, 'justjoin.login')


class JustJoinAdapter(GenericAdapter):
    name = 'justjoin'

    def __init__(self, provider=None, login_callback=None):
        super().__init__(provider)
        self.login_callback = login_callback

    def matches(self, url):
        return urlsplit(url).hostname in {'justjoin.it', 'www.justjoin.it'}

    async def navigate_job_page(self, manager, page, job, challenge_callback=None):
        await super().navigate_job_page(manager, page, job, challenge_callback)
        await JustJoinSource(None).dismiss_notices(page)

    async def _wait_login(self, page):
        if urlsplit(page.url).hostname not in {'sso.rocketjobs.pl','profile.justjoin.it'}:
            return
        async def waiting():
            if self.login_callback: await self.login_callback('waiting', page)
        if not await justjoin_session.wait_for_login(page, waiting): raise LoginPending()
        if self.login_callback: await self.login_callback('completed', page)

    async def open_loaded_application(self, manager, page, job, challenge_callback=None, entry_depth=0):
        await self._wait_login(page)
        if self.matches(page.url):
            await page.get_by_role('button', name=re.compile(r'^1.click Apply$', re.I)).evaluate_all("nodes => nodes.forEach(e=>e.setAttribute('data-final-action','true'))")
        if self.matches(page.url) and not await self._has_visible_form(page):
            one_click = page.get_by_role('button', name=re.compile(r'^1.click Apply$', re.I))
            if await one_click.count():
                # This action sends immediately, even outside an HTML form.
                # Its Edit action opens the reversible application form.
                await one_click.evaluate_all("nodes => nodes.forEach(e=>e.setAttribute('data-final-action','true'))")
                editors = page.get_by_role('button', name='Edit', exact=True)
                for editor in await editors.all():
                    if not await editor.is_visible(): continue
                    owns_application = await editor.evaluate('''e => {for(let p=e.parentElement,depth=0;p&&depth<6;p=p.parentElement,depth++){
                      if(/apply with/i.test(p.innerText)&&Array.from(p.querySelectorAll('button')).some(b=>/^1.click Apply$/i.test(b.innerText.trim())))return true;
                    }return false;}''')
                    if not owns_application: continue
                    await editor.click()
                    await page.locator('form input[name="attachment"]').wait_for(state='visible', timeout=10000)
                    break
                if not await self._has_visible_form(page):
                    raise JobHunterError('APPLICATION_EDITOR_UNAVAILABLE', 'Open the application editor manually before preparing this one-click application.', 409, 'application.entry')
        target = await super().open_loaded_application(manager, page, job, challenge_callback, entry_depth)
        internal = urlsplit(target.target_url).hostname in {'justjoin.it','www.justjoin.it'}
        target.target_type = 'justjoin_internal' if internal else 'external_ats'
        job.raw_metadata = {**(job.raw_metadata or {}), 'original_job_url':job.canonical_url,
                            'application_source':'justjoin.it', 'application_type':target.target_type,
                            'external_application_url':None if internal else target.target_url}
        return target
