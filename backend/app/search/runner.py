"""CV campaign discovery -> canonical AI matching -> existing application owner."""
import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal
from app.models import Application, CandidateDocument, CandidateProfile, Job, SearchCampaign, SearchPlan, SearchProfile, SearchQuery
from app.scanner.justjoin import JustJoinSource
from app.matching.service import MatchService
from app.applications.orchestrator import orchestrator
from app.services.events import event_hub
from app.utils.hashing import stable_hash

logger = logging.getLogger(__name__)


class CampaignRunner:
    def __init__(self):
        self.tasks = {}

    def start(self, campaign_id):
        task = self.tasks.get(campaign_id)
        if task and not task.done():
            return False
        task = asyncio.create_task(self._run(campaign_id))
        self.tasks[campaign_id] = task
        task.add_done_callback(lambda completed: self.tasks.pop(campaign_id, None) if self.tasks.get(campaign_id) is completed else None)
        return True

    async def _run(self, campaign_id):
        with SessionLocal() as db:
            campaign = db.get(SearchCampaign, campaign_id)
            if campaign is None:
                return
            try:
                await self.run(db, campaign)
            except asyncio.CancelledError:
                campaign.status = 'INTERRUPTED'; db.commit()
                raise
            except Exception as exc:
                db.rollback()
                campaign.status = 'FAILED'; db.commit()
                logger.exception('Campaign failed', extra={'context': {'campaign_id': campaign_id, 'error_type': type(exc).__name__}})

    async def run(self, db, campaign, scanner_factory=JustJoinSource, match_factory=MatchService, start_application=None):
        if start_application is None:
            # Reuse the canonical atomic start/CV validation path, not a parallel
            # scheduler with different prerequisites.
            from app.api.router import start_application
        candidate = db.get(CandidateProfile, campaign.candidate_id)
        plan = db.execute(select(SearchPlan).where(SearchPlan.campaign_id == campaign.id)).scalar_one()
        queries = list(db.execute(select(SearchQuery).where(SearchQuery.campaign_id == campaign.id, SearchQuery.enabled.is_(True)).order_by(SearchQuery.id)).scalars())
        prior = dict((plan.rationale or {}).get('run') or {})
        completed = set(prior.get('completed_queries', [])) if campaign.status not in {'COMPLETED', 'COMPLETED_WITH_ERRORS'} else set()
        resuming = campaign.status not in {'DRAFT', 'COMPLETED', 'COMPLETED_WITH_ERRORS'}
        progress = {'completed_queries': sorted(completed), 'queued': int(prior.get('queued', 0)) if resuming else 0,
                    'errors': int(prior.get('errors', 0)) if resuming else 0, 'current_query': None}
        def persist(status):
            campaign.status = status
            plan.rationale = {**(plan.rationale or {}), 'run': dict(progress)}
            db.commit()
            event_hub.publish('applications', {'type': 'search_progress', 'campaign_id': campaign.id, 'status': status, **progress})
        persist('SEARCHING')
        for query in queries:
            if query.id in completed:
                continue
            progress['current_query'] = query.query
            persist('SEARCHING')
            name = 'campaign-%d-query-%d' % (campaign.id, query.id)
            profile = db.execute(select(SearchProfile).where(SearchProfile.name == name)).scalar_one_or_none()
            if profile is None:
                profile = SearchProfile(name=name, query=query.query, location=query.location, max_pages=2)
                db.add(profile); db.commit()
            from app.browser.challenge import ManualVerificationPending
            try:
                scan = await scanner_factory(db).scan(profile)
            except ManualVerificationPending:
                persist('WAITING_FOR_MANUAL_VERIFICATION')
                return progress
            if scan.status in {'WAITING_FOR_MANUAL_VERIFICATION', 'WAITING_FOR_CHALLENGE'}:
                persist('WAITING_FOR_MANUAL_VERIFICATION')
                return progress
            job_ids = list((scan.details or {}).get('job_ids', []))
            progress['errors'] += int(scan.error_count)
            persist('EVALUATING')
            if job_ids:
                try:
                    matched = await match_factory(db).run(candidate, job_ids)
                    if matched and matched.get('ai_unavailable'):
                        progress['errors'] += len(job_ids)
                except Exception:
                    db.rollback()
                    logger.exception('Campaign batch evaluation failed; isolating vacancies', extra={'context': {'campaign_id': campaign.id}})
                    for job_id in job_ids:
                        try:
                            await match_factory(db).run(candidate, [job_id])
                        except Exception:
                            db.rollback()
                            progress['errors'] += 1
                            failed_job = db.get(Job, job_id)
                            if failed_job:
                                failed_job.matching_status = 'AI_REQUIRED'
                                db.commit()
                            logger.exception('Campaign vacancy evaluation failed; continuing', extra={'context': {'job_id': job_id}})
            document = db.execute(select(CandidateDocument).where(CandidateDocument.profile_id == candidate.id, CandidateDocument.kind == 'CV', CandidateDocument.active.is_(True)).order_by(CandidateDocument.version.desc())).scalars().first()
            for job_id in job_ids:
                job = db.get(Job, job_id)
                evaluation = MatchService.evaluation_for(db, candidate, job) if job else {}
                if job is None or job.matching_status != 'ACCEPTED' or evaluation.get('decision') != 'APPLY':
                    continue
                if (job.raw_metadata or {}).get('operator_skip'):
                    continue
                existing = db.execute(select(Application).where(Application.candidate_id == candidate.id, Application.job_id == job.id)).scalar_one_or_none()
                if existing:
                    # Paused, failed, submitted and aborted sessions are never
                    # silently restarted. Explicit retry/reset owns those.
                    continue
                application = Application(candidate_id=candidate.id, job_id=job.id, url_fingerprint=stable_hash(job.canonical_url),
                                          selected_cv_document_id=document.id if document else None,
                                          review_data={'origin': 'campaign', 'campaign_id': campaign.id, 'evaluation': evaluation})
                db.add(application); job.application_status = 'READY'
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    # Another campaign/interactive request won the unique slot.
                    if db.execute(select(Application).where(Application.candidate_id == candidate.id, Application.job_id == job.id)).scalar_one_or_none():
                        continue
                    raise
                progress['queued'] += 1
                persist('PREPARING_APPLICATIONS')
                if document is None:
                    continue
                try:
                    await start_application(application.id, db)
                    task = orchestrator.tasks.get(application.id)
                    if task is not None:
                        await asyncio.shield(task)
                    db.refresh(application)
                    if application.status == 'FAILED':
                        progress['errors'] += 1
                    # The owner stops at review, a question, or manual challenge.
                    # Those states never block the next independent job.
                except Exception as exc:
                    db.rollback()
                    progress['errors'] += 1
                    logger.exception('Campaign application failed; continuing', extra={'context': {'job_id': job_id, 'application_id': application.id, 'error_type': type(exc).__name__}})
            completed.add(query.id)
            progress['completed_queries'] = sorted(completed)
            persist('SEARCHING')
        progress['current_query'] = None
        persist('COMPLETED_WITH_ERRORS' if progress['errors'] else 'COMPLETED')
        return progress

    async def stop(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


campaign_runner = CampaignRunner()
