from datetime import datetime
import logging
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.provider import AIError, AIProvider, AIProviderError, ProviderUnavailable, get_ai_provider
from app.config import settings
from app.errors import JobHunterError
from app.models import AIRequestLog, CandidateProfile, CandidateDocument, Job, JobMatch
from app.utils.hashing import stable_hash
from app.utils.hashing import profile_hash
from app.jobs.context import INPUT_VERSION, completeness
from app.services.diagnostics import emit, redact


logger = logging.getLogger(__name__)
ProviderFactory = Callable[..., AIProvider]


class MatchService:
    def __init__(self, db: Session, provider_factory: ProviderFactory = get_ai_provider, application_id=None):
        self.db = db
        self.provider = provider_factory(db=db)
        self.cache_hits = 0
        self.application_id = application_id

    def diagnostic(self, job, **values):
        diagnostic = {**((job.raw_metadata or {}).get('suitability') or {}), **redact(values)}
        job.raw_metadata = {**(job.raw_metadata or {}), 'suitability': diagnostic}
        emit('suitability.' + str(values.get('state', 'progress')).lower(), vacancy_id=job.id,
             application_id=self.application_id, **values)

    @staticmethod
    def evaluation_for(db, candidate, job):
        cached = db.execute(select(JobMatch).where(
            JobMatch.job_id == job.id, JobMatch.content_hash == job.content_hash,
            JobMatch.profile_hash == profile_hash(candidate.data),
        )).scalar_one_or_none()
        return dict(cached.evaluation or {}) if cached else {}

    def _request_context(self, stage: str, exc: Exception) -> Dict[str, Any]:
        request = getattr(self.provider, "last_request", {})
        return {
            "runtime": settings.ai_runtime,
            "model": getattr(self.provider, "model", settings.ai_model),
            "endpoint": request.get("endpoint"),
            "module": "app.matching.service",
            "stage": stage,
            "request_id": request.get("request_id"),
            "exception_class": type(exc).__name__,
        }

    async def infer(self, candidate: Dict[str, Any], jobs: List[Dict[str, Any]]) -> List[Any]:
        """Run the exact structured production inference used by matching and diagnostics."""
        try:
            return await self.provider.match_jobs(candidate, jobs)
        except AIError as exc:
            stage = "matching.parse" if exc.error_type in {"response_parse_error", "ai_structured_output_error"} else "matching.inference"
            exc.stage = stage
            logger.exception("Production matching AI call failed", extra={"context": self._request_context(stage, exc)})
            raise
        except Exception as exc:
            stage = "matching.inference"
            logger.exception("Production matching AI call failed", extra={"context": self._request_context(stage, exc)})
            raise JobHunterError(
                "jobhunter_backend_error",
                "The production matching AI service failed unexpectedly.",
                500,
                stage,
                {key: value for key, value in self._request_context(stage, exc).items() if key in {"model", "endpoint", "request_id"}},
            ) from exc

    @staticmethod
    def _ai_batch(batch: List[Job]) -> tuple:
        payloads: List[Dict[str, Any]] = []
        jobs_by_id: Dict[str, Job] = {}
        for job in batch:
            payload = dict(job.compact_json or {})
            payload.update(title=job.title, company=job.company, location=job.location,
                           description=(job.description_normalized or '')[:24000], requirements=job.requirements,
                           responsibilities=job.responsibilities, employment_type=job.employment_type,
                           nice_to_have=job.nice_to_have, seniority=job.seniority, work_mode=job.work_mode,
                           source_constraints={key: (job.raw_metadata or {}).get(key) for key in ('salary', 'technologies', 'applicant_locations', 'work_type')},
                           source_url=job.canonical_url, original_source_url=(job.raw_metadata or {}).get('original_job_url'),
                           external_url=(job.raw_metadata or {}).get('external_application_url'))
            decision_id = str(payload.get("id") or "job:%d" % job.id)
            payload["id"] = decision_id
            payloads.append(payload)
            jobs_by_id[decision_id] = job
        return payloads, jobs_by_id

    def _pending(self, candidate: CandidateProfile, job_ids: Optional[List[int]]) -> List[Job]:
        query = select(Job).where(Job.is_active.is_(True))
        if job_ids:
            query = query.where(Job.id.in_(job_ids))
        jobs = list(self.db.execute(query.order_by(Job.id)).scalars())
        candidate_hash = profile_hash(candidate.data)
        result = []
        for job in jobs:
            if job.application_status == "SUBMITTED" or (job.raw_metadata or {}).get("operator_skip") or (job.raw_metadata or {}).get("search_filter_skip"):
                continue
            cached = self.db.execute(select(JobMatch).where(
                JobMatch.job_id == job.id,
                JobMatch.content_hash == job.content_hash,
                JobMatch.profile_hash == candidate_hash,
            )).scalar_one_or_none()
            if cached and (not cached.evaluation or (job.raw_metadata or {}).get('match_input_version') != INPUT_VERSION):
                # Pre-migration summaries cannot authorize autonomous preparation.
                self.db.delete(cached)
                self.db.flush()
                cached = None
            if cached:
                job.matching_status = "AI_REVIEW" if (cached.evaluation or {}).get("decision") == "NEEDS_REVIEW" else "ACCEPTED" if cached.accepted else "REJECTED"
                self.cache_hits += 1
                self.db.add(AIRequestLog(
                    operation="job_matching", provider=cached.provider, model=cached.model,
                    cache_key=stable_hash({"job": job.content_hash, "profile": candidate_hash}),
                    cache_hit=True, input_chars=0, output_chars=0, success=True,
                ))
            else:
                result.append(job)
        return result

    async def run(self, candidate: CandidateProfile, job_ids: Optional[List[int]] = None) -> dict:
        candidate_hash = profile_hash(candidate.data)
        jobs = self._pending(candidate, job_ids)
        if not candidate.data:
            for job in jobs:
                job.matching_status = 'AI_ERROR'
                self.diagnostic(job, state='ERROR', ai_call_started=False, ai_call_completed=False,
                                candidate_profile_loaded=False, error_type='candidate_profile_missing',
                                schema_status='not_received')
            self.db.commit()
            raise JobHunterError('candidate_profile_missing', 'Import a CV and load the candidate profile before suitability analysis.', 409, 'matching.input')
        local_rejected = 0
        # Evaluate against this candidate, not the retired universal entry-IT filter.
        ai_candidates = []
        for job in jobs:
            if completeness(job)['sufficient']:
                ai_candidates.append(job)
            else:
                job.matching_status = 'WAITING_FOR_JOB_DATA'
                self.diagnostic(job, state='WAITING_FOR_JOB_DATA', ai_call_started=False,
                                ai_call_completed=False, input_summary=completeness(job))
        self.db.flush()

        matched = 0
        rejected = 0
        unavailable = False
        # Singleton inference prevents peer vacancies from influencing an absolute
        # score, even if a provider ignores the rubric's no-ranking instruction.
        for offset in range(0, len(ai_candidates)):
            batch = ai_candidates[offset:offset + 1]
            payloads, jobs_by_id = self._ai_batch(batch)
            for job in batch:
                self.diagnostic(job, state='EVALUATING', ai_call_started=True, ai_call_completed=False,
                                model=self.provider.model, input_summary={**completeness(job), 'source': job.source,
                                    'source_url': job.canonical_url, 'external_url': (job.raw_metadata or {}).get('external_application_url') or (job.compact_json or {}).get('application_url'),
                                    'title': job.title, 'company': job.company},
                                cv_loaded=bool(self.db.execute(select(CandidateDocument.id).where(CandidateDocument.profile_id == candidate.id, CandidateDocument.kind == 'CV').limit(1)).first()),
                                candidate_profile_loaded=bool(candidate.data),
                                schema_status='pending', persistence_status='pending')
            self.db.commit()
            try:
                decisions = await self.infer(candidate.data, payloads)
            except ProviderUnavailable:
                unavailable = True
                for job in batch:
                    job.matching_status = "AI_REQUIRED"
                    self.diagnostic(job, state='ERROR', error_type='ProviderUnavailable', schema_status='not_received')
                continue
            except Exception as exc:
                self.db.rollback()
                for job in batch:
                    job.matching_status = 'AI_ERROR'
                    self.diagnostic(job, state='ERROR', error_type=type(exc).__name__,
                                    raw_response=getattr(self.provider, '_last_output_text', None),
                                    schema_status='failed' if getattr(exc, 'stage', '') == 'matching.parse' else 'not_received')
                self.db.commit()
                raise
            indexed = {decision.id: decision for decision in decisions}
            missing = sorted(set(jobs_by_id) - set(indexed))
            unexpected = sorted(set(indexed) - set(jobs_by_id))
            if missing or unexpected or len(indexed) != len(decisions):
                request = getattr(self.provider, "last_request", {})
                error = AIProviderError(
                    "ai_response_validation_failed",
                    "The job-matching response did not map exactly to the requested jobs.",
                    502,
                    {
                        "stage": "matching.parse",
                        "request_id": request.get("request_id"),
                        "missing_decision_count": len(missing),
                        "unexpected_decision_count": len(unexpected),
                    },
                )
                logger.error(
                    "Production matching response identity mismatch",
                    extra={"context": self._request_context("matching.parse", error)},
                )
                self.db.rollback()
                for job in batch:
                    job.matching_status = 'AI_ERROR'
                    self.diagnostic(job, state='ERROR', ai_call_completed=True,
                                    error_type=error.error_type, schema_status='identity_failed')
                self.db.commit()
                raise error
            for decision_id, job in jobs_by_id.items():
                decision = indexed[decision_id]
                evaluation = decision.evaluation.model_dump() if decision.evaluation else {}
                raw_decision = evaluation.get('decision', 'APPLY' if decision.ok else 'SKIP')
                accepted = decision.ok
                if evaluation:
                    if evaluation["hard_blockers"] or decision.blockers:
                        evaluation["decision"] = "SKIP"
                    elif evaluation["decision"] == "APPLY" and evaluation["confidence"] < 0.75:
                        evaluation["decision"] = "NEEDS_REVIEW"
                    accepted = evaluation["decision"] == "APPLY"
                self.db.add(JobMatch(
                    job_id=job.id, profile_hash=candidate_hash, content_hash=job.content_hash,
                    accepted=accepted, reason=decision.reason, blockers=decision.blockers,
                    evaluation=evaluation,
                    score=evaluation.get('suitability_score', (decision.score or 0) * 100) / 100, provider=self.provider.name, model=self.provider.model,
                ))
                job.matching_status = "AI_REVIEW" if evaluation.get("decision") == "NEEDS_REVIEW" else "ACCEPTED" if accepted else "REJECTED"
                job.ai_checked_at = datetime.utcnow()
                job.raw_metadata = {**(job.raw_metadata or {}), 'match_input_version': INPUT_VERSION}
                self.diagnostic(job, state=evaluation.get('decision', raw_decision), ai_call_completed=True,
                                raw_decision=raw_decision, normalized_decision=evaluation.get('decision', raw_decision),
                                score=evaluation.get('suitability_score', decision.score), reason=decision.reason,
                                normalization_reason='hard_blocker' if evaluation.get('hard_blockers') or decision.blockers else 'confidence_below_0.75' if raw_decision == 'APPLY' and evaluation.get('confidence', 1) < .75 else 'explicit_model_decision',
                                parsed_response=decision.model_dump(), raw_response=getattr(self.provider, '_last_output_text', None),
                                schema_status='validated', persistence_status='pending')
                matched += int(accepted)
                rejected += int(not accepted)
        try:
            self.db.commit()
            for job in ai_candidates:
                diagnostic = (job.raw_metadata or {}).get('suitability') or {}
                if diagnostic.get('schema_status') == 'validated':
                    stored = self.db.execute(select(JobMatch).where(JobMatch.job_id == job.id,
                        JobMatch.content_hash == job.content_hash, JobMatch.profile_hash == candidate_hash)).scalar_one()
                    stored_decision = stored.evaluation.get('decision') or ('APPLY' if stored.accepted else 'SKIP')
                    if stored_decision != diagnostic.get('normalized_decision'):
                        raise RuntimeError('Suitability persistence readback mismatch')
                    self.diagnostic(job, persistence_status='committed_readback')
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            stage = "matching.persist"
            logger.exception("Matching result persistence failed", extra={"context": self._request_context(stage, exc)})
            for job in ai_candidates:
                emit('suitability.ERROR', 'ERROR', vacancy_id=job.id, application_id=self.application_id,
                     persistence_status='failed', error_type=type(exc).__name__, state='ERROR')
            raise JobHunterError(
                "database_persistence_failed",
                "AI matching succeeded, but saving the result failed.",
                500,
                stage,
                {"model": getattr(self.provider, "model", settings.ai_model)},
            ) from exc
        return {
            "considered": len(jobs), "local_rejected": local_rejected,
            "ai_accepted": matched, "ai_rejected": rejected,
            "ai_unavailable": unavailable, "cache_hits": self.cache_hits,
            "waiting_for_job_data": len(jobs) - len(ai_candidates),
        }
