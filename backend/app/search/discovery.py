"""One persisted run, bounded source work and progressively committed results."""
import asyncio
from datetime import datetime
import logging
from sqlalchemy import select
from app.db.session import SessionLocal
from app.browser.manager import browser_manager
from app.browser.challenge import ManualVerificationPending
from app.models import CandidateProfile, JobScan
from app.matching.service import MatchService
from app.scanner.justjoin import JustJoinSource, justjoin_session, RECOMMENDATIONS
from app.scanner.justjoin_contracts import JustJoinSearchRequest, JustJoinRunOut
from app.services.diagnostics import emit, scope
from app.services.events import event_hub

logger = logging.getLogger(__name__)


def run_output(scan):
    return JustJoinRunOut(id=scan.id, status=scan.status, seen=scan.seen_count,
                         errors=scan.error_count, details=scan.details, completed_at=scan.completed_at)


class DiscoveryRunner:
    def __init__(self): self.tasks = {}

    def start(self, run_id):
        if run_id in self.tasks and not self.tasks[run_id].done(): return False
        task = asyncio.create_task(self.run(run_id))
        self.tasks[run_id] = task
        task.add_done_callback(lambda t: self.tasks.pop(run_id, None) if self.tasks.get(run_id) is t else None)
        return True

    async def run(self, run_id):
        token = scope.set({'search_run_id':run_id})
        detail_page = None
        with SessionLocal() as db:
            scan = db.get(JobScan, run_id)
            if scan is None or scan.completed_at:
                scope.reset(token)
                return
            request = JustJoinSearchRequest.model_validate(scan.details['request'])
            def persist(status, **progress):
                scan.status = status
                scan.details = {**scan.details, **progress}
                db.commit()
                event_hub.publish('applications', {'type':'search_progress', 'search_run_id':run_id, 'status':status})
            try:
                async with (justjoin_session.lock if request.mode == 'recommendations' else justjoin_session.search_lock):
                    persist('CONNECTING_BROWSER', error=None, wait_expired=False)
                    source = JustJoinSource(db)
                    page = await (justjoin_session.ensure_page() if request.mode == 'recommendations' else justjoin_session.ensure_search_page())
                    offers = scan.details.get('offers')
                    if not offers and request.mode == 'recommendations':
                        persist('CHECKING_LOGIN')
                        # Never recreate recommendation data from a public search.
                        if '/profile/recommendations' not in page.url:
                            await page.goto(RECOMMENDATIONS, wait_until='domcontentloaded', timeout=45000)
                        async def waiting(): persist('WAITING_FOR_LOGIN')
                        if not await justjoin_session.wait_for_login(page, waiting):
                            persist('WAITING_FOR_LOGIN', wait_expired=True, message='Login wait expired. Resume this run after signing in.')
                            return
                    elif not offers:
                        persist('APPLYING_FILTERS')
                        await source.apply_filters(page, request.filters)
                        if justjoin_session.status.state != 'WAITING_FOR_LOGIN':
                            await justjoin_session.check(page)
                    async def progress(count):
                        scan.seen_count = count; persist('SEARCHING', target=request.target_count)
                    if not offers:
                        persist('SEARCHING', query_url=page.url, wait_expired=False)
                        offers = await source.discover(page, request.target_count, progress)
                    if not offers:
                        body = (await page.locator('body').inner_text()).casefold()
                        if not any(x in body for x in ('0 offers', 'no offers', 'no job', 'no matching', 'no recommendations', 'no results')):
                            raise RuntimeError('No offers or explicit empty-result state could be verified')
                    persist('SCANNING_DETAILS', offers=offers)
                    # Exactly one reusable detail tab; never one tab per vacancy.
                    binding = scan.details.get('detail_target') or {}
                    if binding.get('browser_id') == browser_manager.chrome.browser_id:
                        detail_page = browser_manager.pages.get(binding.get('id'))
                    if detail_page is None or detail_page.is_closed():
                        detail_page = await browser_manager.context.new_page()
                    persist('SCANNING_DETAILS', detail_target={'id':await browser_manager._bind_page(detail_page),
                                                              'browser_id':browser_manager.chrome.browser_id})
                    completed = list(scan.details.get('job_ids') or [])
                    start_index = int(scan.details.get('processed') or 0)
                    for index in range(start_index, len(offers)):
                        offer = offers[index]
                        persist('SCANNING_DETAILS', cursor=index, processed=index)
                        try:
                            result = await source.details(detail_page, offer['url'], 'recommendations' if request.mode == 'recommendations' else 'search',
                                                          loaded=detail_page.url == offer['url'])
                            job_id = result.job.id
                            if job_id not in completed: completed.append(job_id)
                            setattr(scan, result.disposition+'_count', getattr(scan, result.disposition+'_count')+1)
                            persist('AI_ANALYZING' if request.mode != 'manual' else 'SCANNING_DETAILS', job_ids=completed)
                            if request.mode != 'manual':
                                candidate = db.get(CandidateProfile, request.candidate_id)
                                outcome = await MatchService(db).run(candidate, [job_id])
                                if outcome.get('ai_unavailable'): raise RuntimeError('AI provider unavailable')
                        except ManualVerificationPending:
                            persist('WAITING_FOR_CHALLENGE', cursor=index)
                            return
                        except Exception as exc:
                            db.rollback(); scan = db.get(JobScan, run_id)
                            scan.error_count += 1
                            errors = list(scan.details.get('failures') or [])
                            errors.append({'url':offer['url'], 'type':getattr(exc,'error_type',type(exc).__name__),
                                           'stage':getattr(exc,'stage','justjoin.details'),
                                           'message':getattr(exc,'safe_message','Vacancy processing failed. See the search log.')})
                            persist('SCANNING_DETAILS', failures=errors)
                            logger.exception('Independent vacancy failed; continuing', extra={'context':{'search_run_id':run_id,'index':index}})
                        persist('SCANNING_DETAILS', processed=index+1)
                    scan.completed_at = datetime.utcnow()
                    persist('COMPLETED_WITH_ERRORS' if scan.error_count else 'COMPLETED', job_ids=completed)
            except asyncio.CancelledError:
                persist('INTERRUPTED'); raise
            except Exception as exc:
                db.rollback(); scan = db.get(JobScan, run_id)
                emit('search.error', 'ERROR', error_type=type(exc).__name__)
                persist('ERROR', error={'type':getattr(exc,'error_type',type(exc).__name__),
                    'stage':getattr(exc,'stage','justjoin.search'),
                    'message':getattr(exc,'safe_message','Search failed. See the search log for details.')})
                logger.exception('Just Join discovery failed', extra={'context':{'search_run_id':run_id}})
            finally:
                # Preserve a manual challenge; only ordinary detail work is disposable.
                if detail_page and not detail_page.is_closed() and scan.status != 'WAITING_FOR_CHALLENGE':
                    await detail_page.close()
                scope.reset(token)

    async def stop(self):
        tasks = list(self.tasks.values())
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)


discovery_runner = DiscoveryRunner()
