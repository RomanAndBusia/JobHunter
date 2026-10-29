"""Just Join is a source. Shared browser, matching and form owners do the rest."""
import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit, parse_qs

from bs4 import BeautifulSoup
from app.browser.manager import browser_manager
from app.browser.challenge import ManualVerificationPending
from app.errors import JobHunterError
from app.jobs.normalizer import normalize_job
from app.models import JobScan
from app.repositories.jobs import JobRepository
from app.scanner.justjoin_contracts import JustJoinFilters, JustJoinSessionOut
from app.services.diagnostics import emit

HOST = 'https://justjoin.it'
RECOMMENDATIONS = HOST + '/profile/recommendations'


def canonical_offer_url(url):
    parsed = urlsplit(urljoin(HOST, url))
    if parsed.hostname not in {'justjoin.it', 'www.justjoin.it'} or not re.fullmatch(r'/job-offer/[^/]+/?', parsed.path):
        raise ValueError('Not a Just Join IT vacancy URL')
    return urlunsplit(('https', 'justjoin.it', parsed.path.rstrip('/'), '', ''))


def offer_identity(url):
    return urlsplit(canonical_offer_url(url)).path.split('/')[-1]


def search_entry(filters):
    # Advanced choices are applied through inspected source UI and verified,
    # rather than maintaining guessed query-parameter translations.
    return HOST + '/job-offers/all-locations' + ('/' + quote(filters.category, safe='-') if filters.category else '')


def parse_posting(html, url, visible=None):
    """Parse JobPosting evidence from the rendered document, not list snippets."""
    soup = BeautifulSoup(html, 'html.parser')
    postings = []
    def walk(value):
        if isinstance(value, list):
            for item in value: walk(item)
        elif isinstance(value, dict):
            kinds = value.get('@type', [])
            if kinds == 'JobPosting' or isinstance(kinds, list) and 'JobPosting' in kinds:
                postings.append(value)
            if '@graph' in value: walk(value['@graph'])
    for script in soup.select('script[type="application/ld+json"]'):
        try: walk(json.loads(script.get_text()))
        except (ValueError, TypeError): pass
    visible = visible or {}
    posting = postings[0] if postings else {}
    org = posting.get('hiringOrganization') or {}
    locations = posting.get('jobLocation') or []
    if isinstance(locations, dict): locations = [locations]
    cities = [x.get('address', {}).get('addressLocality', '') for x in locations if isinstance(x, dict)]
    title = visible.get('title') or posting.get('title')
    description = BeautifulSoup(str(posting.get('description') or visible.get('description') or ''), 'html.parser').get_text('\n', strip=True)
    if not title or len(description) < 160:
        raise JobHunterError('SOURCE_EXTRACTION_FAILED', 'The rendered vacancy has no complete job description.', 502, 'justjoin.details')
    stack = visible.get('stack') or []
    requirements = [x['name'] + ': ' + x['level'] for x in stack if 'nice' not in x['level'].casefold()]
    nice = [x['name'] for x in stack if 'nice' in x['level'].casefold()]
    data = {'title': title, 'company': org.get('name') if isinstance(org, dict) else str(org),
            'location': ', '.join(filter(None, cities)), 'description': description,
            'requirements': requirements, 'nice_to_have': nice,
            'seniority': visible.get('seniority'), 'work_mode': visible.get('work_mode') or ('remote' if posting.get('jobLocationType') == 'TELECOMMUTE' else None),
            'employment_type': visible.get('employment_type') or posting.get('employmentType'),
            'raw_metadata': {'salary': posting.get('baseSalary'), 'published_at': posting.get('datePosted'),
                'expires_at': posting.get('validThrough'), 'technologies': stack,
                'applicant_locations': posting.get('applicantLocationRequirements'), 'work_type': visible.get('work_type') or posting.get('employmentType'),
                'original_job_url': canonical_offer_url(url), 'application_source': 'justjoin.it'}}
    return normalize_job('justjoin', offer_identity(url), canonical_offer_url(url), data)


def classify_login(url, signals):
    """Multiple explicit DOM signals; absence of a login button is insufficient."""
    authenticated = bool(signals.get('logout') or signals.get('account'))
    recommendations = '/profile/recommendations' in urlsplit(url).path and signals.get('recommendations') and not signals.get('login')
    login = signals.get('login') or signals.get('password') or urlsplit(url).hostname in {'sso.rocketjobs.pl', 'profile.justjoin.it'}
    if (authenticated or recommendations) and not login:
        return 'LOGGED_IN'
    if login: return 'LOGGED_OUT'
    return 'UNKNOWN'


class JustJoinSession:
    def __init__(self):
        self.page = None
        self.search_page = None
        self.status = JustJoinSessionOut()
        self.lock = asyncio.Lock()
        self.search_lock = asyncio.Lock()
        self._last_login_signals = None

    async def _source_page(self, kind):
        await browser_manager.ensure_started()
        state_file = browser_manager.config.browser_profile / 'jobhunter-source-pages.json'
        try:
            saved = json.loads(state_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            saved = {}
        binding = saved.get(kind, {})
        page = browser_manager.pages.get(binding.get('target_id')) if binding.get('browser_id') == browser_manager.chrome.browser_id else None
        if page is None or page.is_closed() or binding.get('target_id') in browser_manager.page_owners:
            page = await browser_manager.context.new_page()
        target_id = await browser_manager._bind_page(page)
        saved[kind] = {'browser_id':browser_manager.chrome.browser_id, 'target_id':target_id}
        temporary = state_file.with_suffix('.tmp')
        temporary.write_text(json.dumps(saved), encoding='utf-8')
        temporary.replace(state_file)
        return page

    async def ensure_page(self):
        await browser_manager.ensure_started()
        if self.page is None or self.page.is_closed():
            self.page = await self._source_page('justjoin-login')
        return self.page

    async def check(self, page=None):
        page = page or await self.ensure_page()
        signals = await page.evaluate(r'''() => {
          const visible = e => {const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden'};
          const controls=Array.from(document.querySelectorAll('a,button,[role=button]')).filter(visible);
          const names=controls.map(e=>(e.getAttribute('aria-label')||e.innerText||'').trim().toLowerCase());
          return {login:names.some(t=>/^(log in|sign in|zaloguj się)$/.test(t)),
            password:Array.from(document.querySelectorAll('input[type=password]')).some(visible),
            logout:names.some(t=>/^(log out|logout|sign out|wyloguj)$/.test(t)),
            account:controls.some(e=>e.matches('[data-cy="user-avatar-button"]') || /^(my account|your account|account settings|moje konto|profile settings)$/.test((e.getAttribute('aria-label')||e.innerText||'').trim().toLowerCase())),
            recommendations:Array.from(document.querySelectorAll('h1,h2,h3')).filter(visible).some(e=>/^(recommendations|recommended (jobs|offers)|rekomendacje)/i.test(e.innerText))};
        }''')
        state = classify_login(page.url, signals)
        if state == 'LOGGED_OUT' and self.status.state == 'LOGGED_IN': state = 'SESSION_EXPIRED'
        parsed = urlsplit(page.url)
        safe_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
        self.status = JustJoinSessionOut(state=state, page_url=safe_url,
            signals=[key for key, value in signals.items() if value],
            message='Logged in to Just Join IT.' if state == 'LOGGED_IN' else 'Waiting for manual Just Join IT login.' if state in {'LOGGED_OUT','SESSION_EXPIRED'} else 'Checking Just Join IT session.')
        identity = (state, tuple(self.status.signals))
        if identity != self._last_login_signals:
            emit('justjoin.login.check', state=state, signals=self.status.signals)
            self._last_login_signals = identity
        return self.status

    async def ensure_search_page(self):
        await browser_manager.ensure_started()
        if self.search_page is None or self.search_page.is_closed():
            self.search_page = await self._source_page('justjoin-search')
        return self.search_page

    async def wait_for_login(self, page=None, on_wait=None, timeout=1800):
        page = page or await self.ensure_page()
        started = time.monotonic()
        reported = False
        while time.monotonic() - started < timeout:
            status = await self.check(page)
            if status.state == 'LOGGED_IN':
                emit('justjoin.login.success')
                return True
            self.status = status.model_copy(update={'state':'WAITING_FOR_LOGIN','message':'Waiting for Just Join IT login. Sign in manually in the visible Chrome window; this operation will resume automatically.'})
            if not reported:
                await page.bring_to_front()
                emit('justjoin.login.waiting')
                if on_wait: await on_wait()
                reported = True
            # Observe the person's login without reload, challenge interaction or credentials.
            await asyncio.sleep(3)
        return False


justjoin_session = JustJoinSession()


class JustJoinSource:
    def __init__(self, db):
        self.db = db
        self.repo = JobRepository(db)

    async def dismiss_notices(self, page):
        # Only the site's non-binding cookie choice and account promotion.
        reject = page.locator('#cookiescript_reject')
        if await reject.is_visible(): await reject.click()
        promo = page.get_by_role('heading', name=re.compile('Create an account and search smarter'))
        for i in range(await promo.count()):
            close = promo.nth(i).get_by_role('button', name='Close', exact=True)
            if await close.is_visible(): await close.click(); break

    @staticmethod
    async def select_location(text_scope, value):
        location = text_scope.locator('input[placeholder="Location"]:visible')
        # Clear a restored source selection before querying the same city again.
        # React can otherwise retain its old selection without opening suggestions.
        await location.fill('')
        await location.fill(value)
        option = text_scope.locator('p').filter(has_text=re.compile(r'^\s*' + re.escape(value) + r'\s*$', re.I)).first
        await option.wait_for(state='visible', timeout=10000)
        # The source binds selection to the row, not its animated text fragment.
        await option.locator('xpath=../..').click()
        if (await location.input_value()).casefold() != value.casefold():
            raise JobHunterError('FILTER_VERIFICATION_FAILED', 'The source did not retain the selected location.', 502, 'justjoin.filters')

    async def apply_filters(self, page, filters):
        await page.goto(search_entry(filters), wait_until='domcontentloaded', timeout=45000)
        await page.bring_to_front()
        await page.wait_for_timeout(1000)
        await self.dismiss_notices(page)
        # Opening the real search overlay also proves the client is hydrated;
        # pre-hydration native checkbox clicks can otherwise be silently reset.
        await page.get_by_role('button', name=re.compile('Search: Job title|Szukaj:')).click()
        desktop = bool(await page.locator('#search-bar-form:visible').count())
        panel = page.locator('#more-filters:visible' if desktop else '#filter-more:visible')
        await panel.wait_for(state='visible', timeout=15000)
        text_scope = page.locator('#search-bar-form') if desktop else panel
        if filters.keyword:
            await text_scope.locator('input[placeholder="Search: Job title, company, keyword"]:visible').fill(filters.keyword)
        if filters.location:
            await self.select_location(text_scope, filters.location)
        if filters.radius is not None:
            radius = page.locator('input[placeholder="Radius"]:visible')
            if not await radius.count():
                raise JobHunterError('FILTER_UNAVAILABLE', 'The source does not expose radius for this location.', 409, 'justjoin.filters')
            await radius.fill(str(filters.radius))
            await page.get_by_role('option', name=re.compile(r'^%d\s*km$' % filters.radius)).click()
        if desktop:
            # The expanded header search owns a backdrop. Apply its text/location
            # choices and close it before touching the separate sidebar filters.
            await page.locator('#searchbar-submit').click()
            await page.locator('#search-bar-form').wait_for(state='hidden', timeout=15000)
        groups = {'remoteWorkOptions': filters.work_modes, 'employmentTypes': filters.employment_types,
                  'workingTimes': filters.work_types, 'experienceLevels': filters.experience,
                  'languages': filters.languages, 'publishedDate': [str(filters.published_days)] if filters.published_days else []}
        for prefix, values in groups.items():
            for value in dict.fromkeys(values):
                checkbox = panel.locator('input[name="%s-%s"]' % (prefix, value))
                if not await checkbox.count():
                    raise JobHunterError('FILTER_UNAVAILABLE', 'A selected source filter is no longer available.', 409, 'justjoin.filters')
                # MUI inputs can have zero geometry; use their real visible label.
                label = checkbox.locator('xpath=ancestor::label[1]')
                if not await checkbox.is_checked():
                    before_url = page.url
                    await label.click()
                    if desktop:
                        await page.wait_for_url(lambda current: str(current) != before_url, timeout=10000)
                if not await checkbox.is_checked(): raise RuntimeError('Source filter did not become checked')
            emit('search.filter_applied', filter=prefix, selections=len(values))
        if filters.salary_only:
            checkbox = panel.locator('#withSalary')
            await checkbox.set_checked(True)
            if not await checkbox.is_checked(): raise RuntimeError('Salary filter did not become checked')
        if filters.min_salary is not None: await panel.locator('#minSalary').fill(str(filters.min_salary))
        if not desktop:
            await page.locator('#more-filters-confirm').click()
            await panel.wait_for(state='hidden', timeout=15000)
        await page.wait_for_timeout(800)
        for prefix, values in groups.items():
            for value in values:
                persisted = page.locator('#more-filters:visible input[name="%s-%s"]' % (prefix, value))
                if await persisted.count() and not await persisted.is_checked():
                    raise JobHunterError('FILTER_VERIFICATION_FAILED', 'The source did not preserve a selected filter.', 502, 'justjoin.filters')
        if filters.sort != 'default':
            await page.get_by_role('button', name=re.compile(r'^(Sort by: )?Default$')).click()
            await page.get_by_text('Latest' if filters.sort == 'newest' else 'Highest salary', exact=True).click()
            await page.wait_for_timeout(500)
        await page.get_by_role('button', name='Portal settings', exact=True).click()
        currency = page.get_by_role('button', name='Original currency' if filters.currency == 'original' else filters.currency, exact=True)
        await currency.click()
        if not await currency.count():
            await page.get_by_role('button', name='Portal settings', exact=True).click()
        if await currency.get_attribute('aria-pressed') != 'true':
            raise JobHunterError('FILTER_VERIFICATION_FAILED', 'The source did not preserve the displayed currency.', 502, 'justjoin.filters')
        await page.keyboard.press('Escape')
        emit('search.query_verified', url=page.url)

    async def discover(self, page, target, progress=None, timeout=120):
        discovered = {}
        inspected_cards = set()
        idle = 0
        started = time.monotonic()
        while len(discovered) < target and idle < 4 and time.monotonic() - started < timeout:
            offers = await page.evaluate('''() => Array.from(document.querySelectorAll('a[href*="/job-offer/"]')).filter(e=>{
              const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden';
            }).map(e=>({url:e.href,title:(e.innerText||e.getAttribute('aria-label')||'').trim()}))''')
            before = len(discovered)
            if '/profile/recommendations' in urlsplit(page.url).path:
                # Authenticated recommendations use clickable cards, with the
                # true source offer ID revealed by the verified detail query.
                cards = await page.evaluate('''() => Array.from(document.querySelectorAll('h3[data-cy="title"]')).filter(e=>{
                  const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden';
                }).map((e,i)=>{e.setAttribute('data-jh-offer',String(i));return {id:String(i),title:e.innerText,key:e.parentElement.parentElement.innerText};})''')
                for card in cards:
                    if card['key'] in inspected_cards: continue
                    if len(discovered) >= target: break
                    locator = page.locator('h3[data-jh-offer="%s"]' % card['id'])
                    prior = parse_qs(urlsplit(page.url).query).get('offer', [None])[0]
                    await locator.click()
                    slug = None
                    for _ in range(40):
                        slug = parse_qs(urlsplit(page.url).query).get('offer', [None])[0]
                        if slug and slug != prior: break
                        await asyncio.sleep(.1)
                    if not slug or slug == prior:
                        raise JobHunterError('SOURCE_CARD_NAVIGATION_FAILED', 'A recommendation did not expose its vacancy identity.', 502, 'justjoin.discovery')
                    url = canonical_offer_url(HOST + '/job-offer/' + slug)
                    discovered.setdefault(offer_identity(url), {'url':url, 'title':card['title']})
                    inspected_cards.add(card['key'])
                    emit('vacancy.url_resolved', source='recommendations', discovered=len(discovered))
            for item in offers:
                try:
                    url = canonical_offer_url(item['url'])
                    discovered.setdefault(offer_identity(url), {'url':url, 'title':item['title']})
                except ValueError: continue
                if len(discovered) >= target: break
            idle = idle + 1 if len(discovered) == before else 0
            emit('search.scroll', discovered=len(discovered), new_count=len(discovered)-before, idle_rounds=idle, target=target)
            if progress: await progress(len(discovered))
            if len(discovered) >= target: break
            await page.evaluate('''() => {
              const links=Array.from(document.querySelectorAll('a[href*="/job-offer/"],h3[data-cy="title"]')).filter(e=>e.getBoundingClientRect().height>0);
              let e=links[links.length-1];
              if(e){e.scrollIntoView({block:'end'});for(let p=e.parentElement;p;p=p.parentElement){
                if(p.scrollHeight>p.clientHeight+50&&/(auto|scroll)/.test(getComputedStyle(p).overflowY)){p.scrollBy(0,p.clientHeight);break;}}}
              window.scrollBy(0,window.innerHeight);
            }''')
            await page.wait_for_timeout(800)
        return list(discovered.values())

    async def details(self, page, url, origin='search', loaded=False):
        if not loaded:
            await page.goto(url, wait_until='domcontentloaded', timeout=45000)
        await page.locator('h1').first.wait_for(state='visible', timeout=15000)
        result = await browser_manager.wait_for_manual_verification(page)
        if result.timed_out: raise ManualVerificationPending('Manual verification required')
        await page.wait_for_timeout(500)
        visible = await page.evaluate('''() => {
          const vis=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden'};
          const headings=Array.from(document.querySelectorAll('h1,h3')).filter(vis);
          const title=headings.find(e=>e.tagName==='H1');
          const description=headings.find(e=>/^job description|opis oferty/i.test(e.innerText));
          const stack=Array.from(document.querySelectorAll('h4')).filter(vis).map(e=>({name:e.innerText,level:e.parentElement.innerText.replace(e.innerText,'').trim()}));
          const header=title?.parentElement.parentElement.parentElement.innerText||'';
          const lines=header.split('\\n').map(x=>x.trim());
          return {title:title?.innerText,description:description?.parentElement.innerText,stack,
            seniority:(header.match(/\\b(Intern|Junior|Mid|Senior|Team Leader|C-level)\\b/i)||[])[0],
            work_mode:(header.match(/\\b(Remote|Hybrid|Office)\\b/i)||[])[0]?.toLowerCase(),
            work_type:lines.find(x=>/^(Full-time|Part-time|Freelance|Internship|B2B Contract)$/i.test(x)),
            employment_type:lines.find(x=>/^(B2B|Permanent|Mandate|Contract|Employment|UoP)(,.*)?$/i.test(x))};
        }''')
        normalized = parse_posting(await page.content(), url, visible)
        normalized.raw_metadata['sources_seen'] = [origin]
        payload = normalized.persistence_dict()
        payload['compact_json'].update(salary=normalized.raw_metadata['salary'], technologies=visible.get('stack', []))
        result = self.repo.upsert(payload)
        result.job.details_fetched_at = datetime.utcnow()
        self.db.commit()
        emit('vacancy.normalized', vacancy_id=result.job.id, source='justjoin', disposition=result.disposition)
        return result

    async def scan(self, profile):
        filters = JustJoinFilters(keyword=profile.query, location=profile.location or '', category='',
                                  work_modes=profile.work_modes or [], employment_types=profile.employment_types or [],
                                  experience=[{'entry':'junior','internship':'intern'}.get(v,v) for v in (profile.seniority or ['junior'])])
        scan = JobScan(profile_id=profile.id, details={'source':'justjoin','job_ids':[]})
        self.db.add(scan); self.db.commit()
        async with justjoin_session.search_lock:
            page = await justjoin_session.ensure_search_page()
            await self.apply_filters(page, filters)
            offers = await self.discover(page, min(100, profile.max_pages*20))
            scan.seen_count = len(offers)
            detail_page = await browser_manager.context.new_page()
            try:
                for offer in offers:
                    try:
                        result = await self.details(detail_page, offer['url'])
                        setattr(scan, result.disposition+'_count', getattr(scan, result.disposition+'_count')+1)
                        scan.details = {**scan.details,'job_ids':scan.details['job_ids']+[result.job.id]}
                    except ManualVerificationPending:
                        scan.status = 'WAITING_FOR_CHALLENGE'
                        scan.details = {**scan.details, 'detail_target':await browser_manager._bind_page(detail_page)}
                        self.db.commit()
                        raise
                    except Exception as exc:
                        scan.error_count += 1
                        emit('SOURCE_EXTRACTION_FAILED','ERROR', error_type=type(exc).__name__)
                    self.db.commit()
                scan.status = 'COMPLETED_WITH_ERRORS' if scan.error_count else 'COMPLETED'
                scan.completed_at = datetime.utcnow(); self.db.commit()
            finally:
                if scan.status != 'WAITING_FOR_CHALLENGE' and not detail_page.is_closed(): await detail_page.close()
        return scan
