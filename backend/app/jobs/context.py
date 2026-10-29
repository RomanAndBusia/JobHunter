"""Acquire rendered vacancy evidence before inference and preserve source metadata."""
import asyncio
import time

from sqlalchemy import select

from app.errors import JobHunterError
from app.jobs.page_extractor import GenericJobPageExtractor
from app.models import Job
from app.utils.hashing import canonicalize_url, stable_hash
from app.services.diagnostics import emit

INPUT_VERSION = 'vacancy-context-v5-absolute-rubric-v1'


def fallback_title(title):
    return not title or title.casefold().startswith(('job at ', 'direct job at ', 'loading', 'please wait'))


def completeness(job):
    description = (job.description_normalized or '').strip()
    return {'title_known': not fallback_title(job.title), 'description_characters': len(description),
            'requirements_count': len(job.requirements or []),
            'sufficient': not fallback_title(job.title) and (len(description) >= 160 or bool(job.requirements))}


class JobDataPending(JobHunterError):
    def __init__(self):
        super().__init__('vacancy_data_incomplete', 'Waiting for vacancy details before suitability analysis. The rendered page does not yet provide a role description or requirements.', 409, 'vacancy.extraction')


def origin_job(db, job):
    destination = canonicalize_url(job.canonical_url)
    matches = []
    for other in db.execute(select(Job).where(Job.id != job.id)).scalars():
        url = (other.compact_json or {}).get('application_url') or (other.raw_metadata or {}).get('external_application_url') or (other.raw_metadata or {}).get('final_ats_url')
        if url and canonicalize_url(url) == destination:
            matches.append(other)
    return matches[0] if len(matches) == 1 else None


def merge_extraction(job, extracted, origin=None):
    updates = extracted.job_updates()
    prior = dict(job.raw_metadata or {})
    for key in ('title', 'company', 'location', 'description_normalized', 'requirements', 'responsibilities', 'work_mode', 'employment_type', 'seniority'):
        incoming = updates.get(key)
        existing = getattr(job, key)
        source = getattr(origin, key) if origin is not None else None
        if key == 'title':
            value = source if source and not fallback_title(source) else existing if not fallback_title(existing) else incoming
        elif key == 'company':
            value = source or ((existing or incoming) if extracted.raw_metadata.get('company_fallback') else incoming or existing)
        elif key == 'description_normalized':
            value = max((existing or '', source or '', incoming or ''), key=len)
        else:
            value = incoming or existing or source
        if value is not None:
            setattr(job, key, value)
    if origin:
        prior.update(original_job_id=origin.id, original_job_url=origin.canonical_url, original_source=origin.source)
        prior['external_application_url'] = job.canonical_url
    job.raw_metadata = {**prior, **updates['raw_metadata'], 'extraction': {'title_fallback': fallback_title(extracted.title)}}
    job.compact_json = {**(job.compact_json or {}), **updates['compact_json'], 'title': job.title,
                        'company': job.company, 'location': job.location, 'requirements': job.requirements,
                        'responsibilities': job.responsibilities, 'description': job.description_normalized[:24000]}
    if origin:
        job.compact_json['application_url'] = job.canonical_url
    job.content_hash = stable_hash({'version': INPUT_VERSION, 'payload': job.compact_json})


async def acquire(db, job, page, manager, callback=None, timeout=12, application_id=None, require_complete=True):
    original = origin_job(db, job)
    deadline = time.monotonic() + timeout
    extractor = GenericJobPageExtractor()
    recovery_attempts = 0
    while True:
        try:
            if application_id is not None:
                page = await manager.resolve_active_application_page(application_id)
            extracted = await asyncio.wait_for(extractor.extract(page), timeout=max(1, min(10, deadline - time.monotonic())))
            if application_id is not None:
                current = await manager.resolve_active_application_page(application_id)
                if current is not page:
                    from app.browser.manager import BrowserSessionLostError
                    raise BrowserSessionLostError('extraction_target_changed')
        except Exception as exc:
            from playwright.async_api import Error as PlaywrightError
            from app.browser.manager import BrowserSessionLostError
            target_lost = (isinstance(exc, BrowserSessionLostError) or type(exc).__name__ == 'TargetClosedError'
                           or isinstance(exc, PlaywrightError) and page.is_closed())
            if target_lost:
                # Cancelled extraction has finished before attempting one
                # idempotent retry. Never apply this policy to submission.
                if application_id is not None and recovery_attempts == 0:
                    recovery_attempts += 1
                    page = await manager.resolve_active_application_page(application_id)
                    continue
                raise BrowserSessionLostError('extraction_target_lost') from exc
            if not isinstance(exc, asyncio.TimeoutError):
                raise
            job.matching_status = 'WAITING_FOR_JOB_DATA'
            emit('vacancy.extraction_timeout', 'WARNING', vacancy_id=job.id)
            db.commit()
            raise JobDataPending()
        merge_extraction(job, extracted, original)
        info = completeness(job)
        emit('vacancy.extracted', vacancy_id=job.id, source=job.source, source_url=job.canonical_url,
             title=job.title, company=job.company, metadata_completeness=info, external_url=page.url)
        if info['sufficient'] or not require_complete:
            db.commit()
            return
        if original and page.url != original.canonical_url and not completeness(original)['sufficient']:
            # Inspect the known source in a separate owned page; never alter an ATS form.
            source_page = await manager.new_application_page()
            await source_page.goto(original.canonical_url, wait_until='domcontentloaded', timeout=30000)
            verification = await manager.wait_for_manual_verification(source_page, callback)
            if verification.timed_out:
                from app.browser.challenge import ManualVerificationPending
                raise ManualVerificationPending('Complete verification on the source vacancy page')
            source_data = await asyncio.wait_for(extractor.extract(source_page), timeout=10)
            merge_extraction(original, source_data)
            merge_extraction(job, extracted, original)
            original = None  # One bounded source recovery attempt.
            if completeness(job)['sufficient']:
                db.commit()
                return
        if time.monotonic() >= deadline:
            job.matching_status = 'WAITING_FOR_JOB_DATA'
            db.commit()
            raise JobDataPending()
        await asyncio.sleep(.5)
