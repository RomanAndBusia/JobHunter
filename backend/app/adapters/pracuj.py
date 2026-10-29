from urllib.parse import urlsplit
import re

from app.adapters.generic import GenericAdapter
from app.jobs.page_extractor import GenericJobPageExtractor


def is_pracuj(url):
    host = (urlsplit(url).hostname or '').casefold()
    return host == 'pracuj.pl' or host.endswith('.pracuj.pl')


class PracujAdapter(GenericAdapter):
    name = 'pracuj'

    def matches(self, url):
        return is_pracuj(url)

    async def _find_apply(self, page, job):
        # Site-specific entry recognition is deterministic and cannot choose a
        # form's final submit control. The generic engine owns every later step.
        if is_pracuj(page.url):
            for text in ('Aplikuj szybko', 'Aplikuj'):
                for role in ('link', 'button'):
                    controls = page.get_by_role(role, name=re.compile(r'^\s*' + re.escape(text) + r'\s*$', re.I))
                    for index in range(await controls.count()):
                        control = controls.nth(index)
                        if await control.is_visible() and not await control.evaluate("el => !!el.closest('form')"):
                            job.raw_metadata = {**(job.raw_metadata or {}),
                                'application_type': 'pracuj_quick' if text == 'Aplikuj szybko' else 'external',
                                'original_job_url': job.canonical_url,
                                'application_source': 'pracuj.pl',
                                'external_application_url': await control.get_attribute('href')}
                            return control
        return await super()._find_apply(page, job)

    async def open_loaded_application(self, manager, page, job, challenge_callback=None, entry_depth=0):
        target = await super().open_loaded_application(manager, page, job, challenge_callback, entry_depth)
        native = is_pracuj(target.target_url)
        target.target_type = 'pracuj_quick' if native else 'external_ats'
        job.raw_metadata = {**(job.raw_metadata or {}), 'application_type': target.target_type,
                            'original_job_url': job.canonical_url, 'application_source': 'pracuj.pl',
                            'final_ats_url': None if native else target.target_url,
                            'provider': 'pracuj' if native else GenericJobPageExtractor._provider(target.target_host)}
        return target
