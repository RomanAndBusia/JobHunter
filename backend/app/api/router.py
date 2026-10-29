import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.applications.orchestrator import orchestrator
from app.applications.direct import DirectApplicationService
from app.applications.lifecycle import ApplicationLifecycleService
from app.applications.state_machine import (
    RUNNING_STATES,
    allowed_application_actions,
    transition_application,
)
from app.browser.form_scanner import BrowserFormScanner
from app.browser.manager import browser_manager
from app.browser.worker_client import run_browser_self_test
from app.candidate.documents import import_document
from app.candidate.facts import store_user_fact
from app.candidate.memory import SemanticMemory, polish_answer
from app.ai.codex import CodexExecutionService
from app.ai.diagnostics import ai_health
from app.ai.tasks import codex_tasks
from app.db.session import get_db
from app.config import REPOSITORY_ROOT, settings
from app.forms.sanitize import sanitize_html
from app.matching.service import MatchService
from app.matching.diagnostics import production_ai_smoke
from app.logging import event
from app.models import (
    AIRequestLog,
    Application,
    ApplicationAnswer,
    ApplicationQuestionRequest,
    ApplicationState,
    ApplicationStep,
    CandidateFact,
    CandidateDocument,
    CandidateProfile,
    CodexRun,
    Job,
    JobMatch,
    JobScan,
    SearchProfile,
    SearchCampaign,
    SearchPlan,
    SearchQuery,
    QuestionMemory,
)
from app.search.planner import build_search_campaign
from app.scanner.justjoin import JustJoinSource
from app.scanner.justjoin_contracts import JustJoinSearchRequest, JustJoinSessionOut, JustJoinRunOut, PrepareJobsRequest
from app.search.discovery import discovery_runner, run_output
from app.schemas.api import (
    AnswerEdit,
    ApplicationCreate,
    ApplicationCVSelectionRequest,
    DirectApplicationCreate,
    ApplicationOut,
    CandidateProfileOut,
    CandidateProfileUpdate,
    JobOut,
    MatchRunRequest,
    SearchProfileCreate,
    SearchProfileOut,
    QuestionAnswer,
    CodexTaskCreate,
    SearchCampaignCreate,
)
from app.services.events import event_hub
from app.utils.hashing import stable_hash


router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


@router.get('/justjoin/session', response_model=JustJoinSessionOut)
async def justjoin_session_status():
    from app.scanner.justjoin import justjoin_session
    if justjoin_session.status.state == 'WAITING_FOR_LOGIN':
        return justjoin_session.status
    if justjoin_session.page is not None and not justjoin_session.page.is_closed():
        return await justjoin_session.check()
    if browser_manager.context is not None:
        page = await justjoin_session.ensure_page()
        return await justjoin_session.check(page)
    return justjoin_session.status


@router.post('/justjoin/session/connect', response_model=JustJoinSessionOut)
async def connect_justjoin_session():
    from app.scanner.justjoin import justjoin_session, RECOMMENDATIONS
    if justjoin_session.lock.locked(): return justjoin_session.status
    async with justjoin_session.lock:
        page = await justjoin_session.ensure_page()
        if page.url == 'about:blank':
            await page.goto(RECOMMENDATIONS, wait_until='domcontentloaded', timeout=45000)
        await page.bring_to_front()
        return await justjoin_session.check(page)


@router.post('/justjoin/search', response_model=JustJoinRunOut, status_code=202)
async def start_justjoin_search(payload: JustJoinSearchRequest, db: Session = Depends(get_db)):
    if payload.mode != 'manual':
        candidate = _get_or_404(db, CandidateProfile, payload.candidate_id or 0)
        if not db.execute(select(CandidateDocument.id).where(CandidateDocument.profile_id == candidate.id, CandidateDocument.kind == 'CV', CandidateDocument.active.is_(True))).first():
            raise HTTPException(409, 'Import your current CV before AI Search or recommendation analysis.')
    scan = JobScan(details={'source':'justjoin','request':payload.model_dump(), 'job_ids':[]})
    db.add(scan); db.commit(); db.refresh(scan)
    discovery_runner.start(scan.id)
    return run_output(scan)


@router.get('/justjoin/search-runs', response_model=List[JustJoinRunOut])
def justjoin_search_runs(db: Session = Depends(get_db)):
    return [run_output(scan) for scan in db.execute(select(JobScan).order_by(JobScan.id.desc()).limit(100)).scalars() if (scan.details or {}).get('source') == 'justjoin']


@router.get('/justjoin/search-runs/{run_id}', response_model=JustJoinRunOut)
def justjoin_search_run(run_id: int, db: Session = Depends(get_db)):
    return run_output(_get_or_404(db, JobScan, run_id))


@router.post('/justjoin/search-runs/{run_id}/resume', response_model=JustJoinRunOut)
async def resume_justjoin_search(run_id: int, db: Session = Depends(get_db)):
    scan = _get_or_404(db, JobScan, run_id)
    if not (scan.details or {}).get('request'): raise HTTPException(409, 'This scan cannot be resumed.')
    discovery_runner.start(scan.id)
    return run_output(scan)


@router.get('/justjoin/search-runs/{run_id}/log')
def justjoin_search_log(run_id: int, db: Session = Depends(get_db)):
    _get_or_404(db, JobScan, run_id)
    from app.services.diagnostics import records
    return {'run_id':run_id, 'events':[r for r in records if r.get('search_run_id') == run_id]}


@router.post('/applications/prepare-selected', status_code=202)
async def prepare_selected_jobs(payload: PrepareJobsRequest, db: Session = Depends(get_db)):
    document = _get_or_404(db, CandidateDocument, payload.selected_cv_document_id)
    if document.profile_id != payload.candidate_id or document.kind != 'CV': raise HTTPException(409, 'Select a candidate-owned CV.')
    results = []
    for job_id in dict.fromkeys(payload.job_ids):
        try:
            created = create_application(ApplicationCreate(candidate_id=payload.candidate_id, job_id=job_id), db)
            application = db.get(Application, created['id'])
            if application.status == 'READY':
                application.review_data = {**(application.review_data or {}), 'origin':'user_selected', 'suitability_advisory':True}
                db.commit()
                await select_application_cv(application.id, ApplicationCVSelectionRequest(selected_cv_document_id=document.id), db)
                result = await start_application(application.id, db)
            else:
                result = _application_payload(db, application)
                result['accepted'] = False
            results.append({'job_id':job_id,'application':result})
        except Exception as exc:
            db.rollback()
            logger.exception('Selected application preparation failed independently', extra={'context':{'job_id':job_id}})
            results.append({'job_id':job_id, 'error':getattr(exc,'safe_message',getattr(exc,'detail','Application could not be started.'))})
    return {'results':results}


def _allowed_application_actions(application: Application) -> List[str]:
    page_available = False
    if application.browser_page_id:
        try:
            browser_manager.get_page(application.browser_page_id)
            page_available = True
        except RuntimeError:
            page_available = False
    actions = allowed_application_actions(application, page_available)
    if settings.live_application_test or not settings.allow_final_submit or (application.review_data or {}).get('live_application_test'):
        actions = [action for action in actions if action != 'submit']
    return actions


def _get_or_404(db: Session, model: Any, item_id: int) -> Any:
    item = db.get(model, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Not found")
    return item


def _flatten(data: Dict[str, Any], prefix: str = "") -> List[tuple]:
    result = []
    for key, value in data.items():
        path = (prefix + "." + key).strip(".")
        if isinstance(value, dict):
            result.extend(_flatten(value, path))
        else:
            result.append((path, value))
    return result


@router.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@router.get("/v1/ai/health")
async def ai_health_endpoint(probe: bool = Query(True)) -> Dict[str, Any]:
    """Return secret-free AI configuration and optional live provider status."""
    return await ai_health(probe=probe)


@router.post("/v1/ai/health/self-test")
async def ai_self_test_endpoint() -> Dict[str, Any]:
    return await ai_health(probe=True)


@router.post("/v1/ai/production-self-test")
async def production_ai_self_test_endpoint(db: Session = Depends(get_db)) -> Dict[str, Any]:
    return await production_ai_smoke(db)


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)) -> Dict[str, Any]:
    def count(model: Any, *conditions: Any) -> int:
        return int(db.execute(select(func.count()).select_from(model).where(*conditions)).scalar_one())
    last_scan = db.execute(select(JobScan).order_by(JobScan.id.desc())).scalars().first()
    return {
        "jobs_discovered": count(Job),
        "new_jobs": count(Job, Job.matching_status == "PENDING"),
        "ai_accepted": count(Job, Job.matching_status == "ACCEPTED"),
        "ai_rejected": count(Job, Job.matching_status == "REJECTED"),
        "applications_ready": count(Application, Application.status == ApplicationState.READY.value),
        "applications_in_progress": count(Application, Application.status.in_([
            ApplicationState.OPENING.value, ApplicationState.FORM_DISCOVERY.value,
            ApplicationState.FILLING.value, ApplicationState.VERIFYING.value,
        ])),
        "review_required": count(Application, Application.status == ApplicationState.REVIEW_REQUIRED.value),
        "submitted": count(Application, Application.status == ApplicationState.SUBMITTED.value),
        "failed": count(Application, Application.status == ApplicationState.FAILED.value),
        "needs_user": count(Application, Application.status.in_([
            ApplicationState.NEEDS_USER.value, ApplicationState.CAPTCHA.value,
            ApplicationState.UNKNOWN_FORM.value,
        ])),
        "last_scan": {
            "id": last_scan.id, "status": last_scan.status, "seen": last_scan.seen_count,
            "new": last_scan.new_count, "changed": last_scan.changed_count,
            "completed_at": last_scan.completed_at,
        } if last_scan else None,
    }


@router.get("/search-profiles", response_model=List[SearchProfileOut])
def search_profiles(db: Session = Depends(get_db)) -> List[SearchProfile]:
    return list(db.execute(select(SearchProfile).order_by(SearchProfile.name)).scalars())


@router.post("/search-profiles", response_model=SearchProfileOut, status_code=201)
def create_search_profile(payload: SearchProfileCreate, db: Session = Depends(get_db)) -> SearchProfile:
    profile = SearchProfile(**payload.model_dump())
    db.add(profile)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A search profile with that name already exists") from exc
    db.refresh(profile)
    return profile


@router.put("/search-profiles/{profile_id}", response_model=SearchProfileOut)
def update_search_profile(profile_id: int, payload: SearchProfileCreate, db: Session = Depends(get_db)) -> SearchProfile:
    profile = _get_or_404(db, SearchProfile, profile_id)
    for key, value in payload.model_dump().items():
        setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/scans/{profile_id}")
async def run_scan(profile_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    profile = _get_or_404(db, SearchProfile, profile_id)
    scan = await JustJoinSource(db).scan(profile)
    return {
        "id": scan.id, "status": scan.status, "seen": scan.seen_count,
        "new": scan.new_count, "changed": scan.changed_count,
        "unchanged": scan.unchanged_count, "errors": scan.error_count,
    }


@router.get("/scans")
def scans(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    rows = db.execute(select(JobScan).order_by(JobScan.id.desc()).limit(50)).scalars()
    return [{
        "id": row.id, "profile_id": row.profile_id, "status": row.status,
        "seen": row.seen_count, "new": row.new_count, "changed": row.changed_count,
        "unchanged": row.unchanged_count, "errors": row.error_count,
        "started_at": row.started_at, "completed_at": row.completed_at,
    } for row in rows]


@router.get("/jobs", response_model=List[JobOut])
def jobs(
    source: Optional[str] = None,
    status: Optional[str] = None,
    company: Optional[str] = None,
    location: Optional[str] = None,
    work_mode: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> List[Job]:
    query = select(Job)
    if source:
        query = query.where(Job.source == source)
    if status:
        query = query.where(Job.matching_status == status.upper())
    if company:
        query = query.where(Job.company.ilike("%%%s%%" % company))
    if location:
        query = query.where(Job.location.ilike("%%%s%%" % location))
    if work_mode:
        query = query.where(Job.work_mode == work_mode)
    if q:
        query = query.where(Job.title.ilike("%%%s%%" % q))
    return list(db.execute(query.order_by(Job.last_seen_at.desc()).limit(limit)).scalars())


@router.get("/jobs/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    job = _get_or_404(db, Job, job_id)
    latest_match = db.execute(select(JobMatch).where(JobMatch.job_id == job.id).order_by(JobMatch.id.desc())).scalars().first()
    result = JobOut.model_validate(job).model_dump()
    result["description_normalized"] = job.description_normalized
    result["match"] = {
        "accepted": latest_match.accepted, "reason": latest_match.reason,
        "blockers": latest_match.blockers, "score": latest_match.score,
    } if latest_match else None
    return result


@router.post("/jobs/{job_id}/reject")
def reject_job(job_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    job = _get_or_404(db, Job, job_id)
    job.matching_status = "REJECTED"
    job.raw_metadata = {**(job.raw_metadata or {}), "operator_skip": True}
    db.commit()
    return {"id": job.id, "matching_status": job.matching_status}


@router.post("/matching/run")
async def run_matching(payload: MatchRunRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    candidate = _get_or_404(db, CandidateProfile, payload.candidate_id)
    return await MatchService(db).run(candidate, payload.job_ids)


@router.get("/candidate-profiles", response_model=List[CandidateProfileOut])
def candidate_profiles(db: Session = Depends(get_db)) -> List[CandidateProfile]:
    return list(db.execute(select(CandidateProfile).order_by(CandidateProfile.id)).scalars())


@router.put("/candidate-profiles/{profile_id}", response_model=CandidateProfileOut)
def update_candidate(profile_id: int, payload: CandidateProfileUpdate, db: Session = Depends(get_db)) -> CandidateProfile:
    profile = _get_or_404(db, CandidateProfile, profile_id)
    profile.name = payload.name
    profile.data = payload.data
    profile.updated_at = datetime.utcnow()
    if payload.confirmed:
        profile.confirmed_at = datetime.utcnow()
    for path, value in _flatten(payload.data):
        fact = db.execute(select(CandidateFact).where(
            CandidateFact.profile_id == profile.id, CandidateFact.path == path,
        )).scalar_one_or_none()
        if fact is None:
            fact = CandidateFact(profile_id=profile.id, path=path, concept=path.replace(".", "_"), value=value, source="USER_CONFIRMED" if payload.confirmed else "USER_ENTERED", user_confirmed=payload.confirmed)
            db.add(fact)
        else:
            fact.value = value
            fact.source = "USER_CONFIRMED" if payload.confirmed else "USER_ENTERED"
            fact.user_confirmed = payload.confirmed or fact.user_confirmed
        if payload.confirmed:
            fact.confirmed_at = datetime.utcnow()
            fact.last_confirmed_at = datetime.utcnow()
            fact.confidence = 1.0
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/candidate-profiles/{profile_id}/documents", status_code=201)
async def upload_document(profile_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)) -> Dict[str, Any]:
    profile = _get_or_404(db, CandidateProfile, profile_id)
    try:
        document = await import_document(db, profile, file.filename or "candidate.pdf", file.file, REPOSITORY_ROOT / "storage")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    campaign = db.execute(select(SearchCampaign).where(SearchCampaign.candidate_id == profile.id).order_by(SearchCampaign.id.desc())).scalars().first()
    if campaign is None:
        campaign = build_search_campaign(db, profile)
    return {
        "id": document.id, "profile_id": document.profile_id, "kind": document.kind, "version": document.version,
        "name": document.original_name, "sha256": document.sha256,
        "active": document.active, "created_at": document.created_at, "campaign_id": campaign.id,
    }


@router.get("/candidate-profiles/{profile_id}/documents")
def documents(profile_id: int, db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    _get_or_404(db, CandidateProfile, profile_id)
    from app.models import CandidateDocument
    rows = db.execute(select(CandidateDocument).where(CandidateDocument.profile_id == profile_id).order_by(CandidateDocument.version.desc())).scalars()
    return [{
        "id": row.id, "profile_id": row.profile_id, "kind": row.kind, "version": row.version,
        "name": row.original_name, "sha256": row.sha256,
        "active": row.active, "created_at": row.created_at,
    } for row in rows]


@router.get("/candidate-profiles/{profile_id}/facts")
def candidate_facts(profile_id: int, db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    _get_or_404(db, CandidateProfile, profile_id)
    rows = db.execute(select(CandidateFact).where(CandidateFact.profile_id == profile_id).order_by(CandidateFact.path)).scalars()
    return [{
        "id": row.id, "path": row.path, "concept": row.concept, "value": row.value,
        "source": row.source, "confidence": row.confidence, "user_confirmed": row.user_confirmed,
        "valid_from": row.valid_from, "expires_at": row.expires_at, "last_confirmed_at": row.last_confirmed_at,
    } for row in rows]


@router.get("/candidate-profiles/{profile_id}/known-answers")
def known_answers(profile_id: int, db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    _get_or_404(db, CandidateProfile, profile_id)
    rows = db.execute(select(QuestionMemory).where(QuestionMemory.candidate_id == profile_id).order_by(QuestionMemory.concept)).scalars()
    return [{
        "id": row.id, "concept": row.concept, "value": row.value,
        "raw_user_value": row.raw_user_value, "presentation_value": row.presentation_value,
        "source": row.source, "scope": row.scope, "aliases": row.aliases,
        "expires_at": row.expires_at, "last_confirmed_at": row.last_confirmed_at,
    } for row in rows]


@router.delete("/candidate-profiles/{profile_id}/known-answers/{memory_id}", status_code=204)
def delete_known_answer(profile_id: int, memory_id: int, db: Session = Depends(get_db)) -> None:
    memory = _get_or_404(db, QuestionMemory, memory_id)
    if memory.candidate_id != profile_id:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(memory)
    db.commit()


@router.post("/search-campaigns", status_code=201)
def create_search_campaign(payload: SearchCampaignCreate, db: Session = Depends(get_db)) -> Dict[str, Any]:
    candidate = _get_or_404(db, CandidateProfile, payload.candidate_id)
    campaign = build_search_campaign(db, candidate, payload.name)
    return _campaign_payload(db, campaign)


def _campaign_payload(db: Session, campaign: SearchCampaign) -> Dict[str, Any]:
    plan = db.execute(select(SearchPlan).where(SearchPlan.campaign_id == campaign.id)).scalar_one_or_none()
    queries = list(db.execute(select(SearchQuery).where(SearchQuery.campaign_id == campaign.id).order_by(SearchQuery.id)).scalars())
    return {"id": campaign.id, "candidate_id": campaign.candidate_id, "name": campaign.name, "goal": campaign.goal, "status": campaign.status, "progress": (plan.rationale or {}).get("run", {}) if plan else {}, "families": plan.families if plan else [], "queries": [{"id": row.id, "family": row.role_family, "query": row.query, "location": row.location, "enabled": row.enabled} for row in queries]}


@router.get("/search-campaigns")
def search_campaigns(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    rows = db.execute(select(SearchCampaign).order_by(SearchCampaign.id.desc())).scalars()
    return [_campaign_payload(db, row) for row in rows]


@router.post("/search-campaigns/{campaign_id}/scan", status_code=202)
async def scan_campaign(campaign_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    from app.search.runner import campaign_runner
    campaign = _get_or_404(db, SearchCampaign, campaign_id)
    started = campaign_runner.start(campaign.id)
    return {"campaign_id": campaign.id, "accepted": started, "already_running": not started}


@router.get("/questions/pending")
def pending_questions(application_id: Optional[int] = None, db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    query = select(ApplicationQuestionRequest).where(ApplicationQuestionRequest.status == "WAITING_FOR_USER")
    if application_id is not None:
        query = query.where(ApplicationQuestionRequest.application_id == application_id)
    rows = db.execute(query.order_by(ApplicationQuestionRequest.created_at)).scalars()
    return [{
        "id": row.id, "application_id": row.application_id, "candidate_id": row.candidate_id,
        "field_id": row.field_id, "question": row.question, "concept": row.concept, "fact_path": row.fact_path,
        "answer_type": row.answer_type, "options": row.options, "scope": row.scope,
        "field_context": row.field_context,
        "question_category": row.question_category,
        "synthetic_test": bool((row.field_context or {}).get('synthetic_test')),
        "suggested_value": row.suggested_value,
        "suggestion_reason": row.suggestion_reason,
        "status": row.status, "conflict": row.conflict, "created_at": row.created_at,
    } for row in rows]


@router.post("/questions/{request_id}/answer")
async def answer_question(request_id: int, payload: QuestionAnswer, db: Session = Depends(get_db)) -> Dict[str, Any]:
    request = _get_or_404(db, ApplicationQuestionRequest, request_id)
    if request.status != "WAITING_FOR_USER":
        raise HTTPException(status_code=409, detail="Question has already been answered")
    presentation = payload.value if request.options else polish_answer(request.question, payload.value)
    scope = payload.scope or 'APPLICATION_ONLY'
    if payload.synthetic_test:
        if not settings.live_application_test or settings.allow_final_submit or scope != 'APPLICATION_ONLY':
            raise HTTPException(409, 'Synthetic answers require a guarded live test and application-only scope.')
        if request.question_category not in {'PREFERENCE', 'JOB_SPECIFIC_TEXT', 'FACTUAL', 'SELF_ASSESSMENT'}:
            raise HTTPException(422, 'Synthetic answers cannot provide legal declarations or recruitment consent.')
        request.field_context = {**(request.field_context or {}), 'synthetic_test':True}
    if scope == 'GLOBAL':
        scope = 'CANDIDATE_GLOBAL'
    if scope not in {'APPLICATION_ONLY', 'CANDIDATE_GLOBAL', 'JOB_PREFERENCE_DEFAULT'}:
        raise HTTPException(status_code=422, detail='Unsupported answer scope')
    if payload.value is None or payload.value == '':
        raise HTTPException(status_code=422, detail='An explicit answer is required')
    if request.options:
        allowed = {option['label'] for option in request.options if not option.get('disabled')}
        multiple = request.answer_type == 'multi_select'
        values = payload.value if multiple and isinstance(payload.value, list) else [payload.value]
        if (multiple != isinstance(payload.value, list) or not values
                or any(not isinstance(value, str) or value not in allowed for value in values)):
            raise HTTPException(status_code=422, detail='Choose actual enabled website options')
    if scope == 'CANDIDATE_GLOBAL' and request.fact_path and not payload.confirm_conflict:
        from app.candidate.fact_resolver import CandidateFactResolver
        profile = _get_or_404(db, CandidateProfile, request.candidate_id)
        facts = db.execute(select(CandidateFact).where(CandidateFact.profile_id == request.candidate_id)).scalars()
        prior = CandidateFactResolver(profile.data or {}, facts).resolve(request.fact_path)
        if prior.exists and prior.source in {'USER_ENTERED', 'USER_CONFIRMED'} and prior.value != payload.value:
            raise HTTPException(status_code=409, detail='The answer conflicts with a confirmed candidate fact; confirm replacement explicitly')
    memory = None
    try:
        if scope != 'APPLICATION_ONLY':
            memory = SemanticMemory(db).remember(request.candidate_id, request.question, payload.value, presentation, scope, payload.confirm_conflict)
    except ValueError as exc:
        existing = SemanticMemory(db).resolve(request.candidate_id, request.question)[1]
        request.conflict = {"stored": existing.value if existing else None, "new": payload.value}
        db.commit()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    request.raw_value = payload.value
    request.presentation_value = presentation
    request.scope = scope
    request.status = "ANSWERED"
    request.answered_at = datetime.utcnow()
    if request.fact_path and scope == 'CANDIDATE_GLOBAL':
        profile = _get_or_404(db, CandidateProfile, request.candidate_id)
        try:
            store_user_fact(db, profile, request.fact_path, payload.value)
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    event(logger, 'application.question_resolved', application_id=request.application_id, question_id=request.id, scope=scope)
    event_hub.publish('applications', {'type': 'question_resolved', 'application_id': request.application_id})
    orchestrator.resume(request.application_id)
    return {"id": request.id, "status": request.status, "concept": memory.concept if memory else request.concept, "presentation_value": presentation, "application_resuming": True}


def _codex_payload(run: CodexRun) -> Dict[str, Any]:
    return {"id": run.id, "task": run.task, "status": run.status, "progress": run.progress, "result": run.result, "error": run.error, "started_at": run.started_at, "completed_at": run.completed_at, "created_at": run.created_at}


@router.post("/codex-runs", status_code=202)
async def create_codex_run(payload: CodexTaskCreate, db: Session = Depends(get_db)) -> Dict[str, Any]:
    run = CodexRun(task=payload.task)
    db.add(run)
    db.commit()
    db.refresh(run)
    codex_tasks.start(run.id)
    return _codex_payload(run)


@router.get("/codex-runs")
def codex_runs(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    return [_codex_payload(row) for row in db.execute(select(CodexRun).order_by(CodexRun.id.desc()).limit(50)).scalars()]


@router.get("/codex-runs/{run_id}")
def codex_run(run_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return _codex_payload(_get_or_404(db, CodexRun, run_id))


def _application_payload(db: Session, application: Application) -> Dict[str, Any]:
    result = ApplicationOut.model_validate(application).model_dump()
    result["job"] = {
        "id": application.job.id, "title": application.job.title,
        "company": application.job.company, "location": application.job.location,
        "source": application.job.source, "url": application.job.canonical_url,
        "provider": (application.job.raw_metadata or {}).get("provider"),
    }
    result["answers"] = [{
        "id": answer.id, "field_id": answer.field_id, "question": answer.question,
        "value": answer.value, "source": answer.source, "verified": answer.verified,
        "manual_override": answer.manual_override, "meaning": answer.semantic_meaning,
        "source_paths": answer.source_paths, "confidence": answer.confidence,
        "execution_confidence": answer.execution_confidence,
        "reason": answer.reason, "status": answer.status,
    } for answer in application.answers]
    result["steps"] = [{
        "sequence": step.sequence, "from": step.state_from, "to": step.state_to,
        "action": step.action, "details": step.details, "success": step.success,
        "created_at": step.created_at,
    } for step in application.steps]
    result["allowed_actions"] = _allowed_application_actions(application)
    result['suitability_state'] = ((application.job.raw_metadata or {}).get('suitability') or {}).get('state') or ((application.review_data or {}).get('evaluation') or {}).get('decision') or 'NOT_STARTED'
    if (application.review_data or {}).get('job_data_pending'):
        result['suitability_state'] = 'WAITING_FOR_JOB_DATA'
    if application.status == 'FAILED' and str(((application.review_data or {}).get('failure') or {}).get('stage', '')).startswith('matching'):
        result['suitability_state'] = 'ERROR'
    if not result['target_host'] and application.browser_page_id:
        try:
            page = browser_manager.get_page(application.browser_page_id)
            from urllib.parse import urlsplit
            result['target_host'] = urlsplit(page.url).netloc or 'Opening page'
            result['target_url'] = page.url
        except RuntimeError:
            pass
    return result


@router.get('/applications/{application_id}/log')
async def application_log(application_id: int, full_run: bool = False, db: Session = Depends(get_db)):
    from app.services.pipeline_diagnostics import snapshot
    application = _get_or_404(db, Application, application_id)
    await browser_manager.inspect_session_state(application.browser_page_id)
    return snapshot(db, application, browser_manager, full_run)


@router.post('/applications/{application_id}/diagnostic-run', status_code=202)
async def application_diagnostic_run(application_id: int, db: Session = Depends(get_db)):
    application = _get_or_404(db, Application, application_id)
    if application.status not in {'READY', 'NEEDS_USER', 'PAUSED', 'FAILED', 'UNKNOWN_FORM', 'CAPTCHA'}:
        raise HTTPException(409, 'Diagnostic runs are available only for idle or paused applications; they cannot approve or submit.')
    application.review_data = {**(application.review_data or {}), 'diagnostic_mode': True}
    db.commit()
    if application.status == 'READY':
        return await start_application(application_id, db)
    if application.status in {'FAILED', 'UNKNOWN_FORM'}:
        return await retry_application(application_id, db)
    return await resume_application(application_id, db)


@router.get("/applications")
def applications(status: Optional[str] = None, db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    query = select(Application)
    if status:
        query = query.where(Application.status == status.upper())
    rows = db.execute(query.order_by(Application.updated_at.desc())).scalars()
    return [_application_payload(db, row) for row in rows]


@router.get("/applications/direct-url/active")
def active_direct_application(candidate_id: int, db: Session = Depends(get_db)):
    _get_or_404(db, CandidateProfile, candidate_id)
    application = DirectApplicationService.active(db, candidate_id)
    return _application_payload(db, application) if application else None


@router.post("/applications/{application_id}/new-apply")
async def new_direct_apply(application_id: int, db: Session = Depends(get_db)):
    async with orchestrator.mutation_lock(application_id):
        application = _get_or_404(db, Application, application_id)
        if not application.direct_slot_active:
            return _application_payload(db, application)
        if application.status in {ApplicationState.APPROVED.value, ApplicationState.SUBMITTING.value}:
            raise HTTPException(status_code=409, detail="Wait for submission to finish before NEW APPLY.")
        try:
            await orchestrator.release(application_id)
        except Exception as exc:
            logger.exception("NEW APPLY could not stop application %s", application_id)
            raise HTTPException(status_code=409, detail="Application workflow could not be stopped safely for NEW APPLY") from exc
        db.refresh(application)
        # Terminal outcomes remain truthful; unfinished work is explicitly stopped.
        if application.status not in {"SUBMITTED", "ABORTED", "FAILED", "EXPIRED"}:
            transition_application(db, application, ApplicationState.ABORTED.value, "new_apply_stopped")
            application.job.application_status = application.status
            application.current_operation = "Stopped by NEW APPLY"
        application.browser_page_id = None
        application.direct_slot_active = None
        db.commit()
        return _application_payload(db, application)


@router.get("/applications/{application_id}")
def application_detail(application_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    return _application_payload(db, _get_or_404(db, Application, application_id))


@router.post("/applications", status_code=201)
def create_application(payload: ApplicationCreate, db: Session = Depends(get_db)) -> Dict[str, Any]:
    _get_or_404(db, CandidateProfile, payload.candidate_id)
    job = _get_or_404(db, Job, payload.job_id)
    existing = db.execute(select(Application).where(
        Application.candidate_id == payload.candidate_id,
        Application.job_id == payload.job_id,
    )).scalar_one_or_none()
    if existing:
        return _application_payload(db, existing)
    application = Application(
        candidate_id=payload.candidate_id,
        job_id=payload.job_id,
        url_fingerprint=stable_hash(job.canonical_url),
        review_data={"origin": job.source, "ai_runtime": settings.ai_runtime},
    )
    job.application_status = "READY"
    db.add(application)
    db.commit()
    db.refresh(application)
    return _application_payload(db, application)


@router.post("/applications/direct-url", status_code=202)
async def create_direct_application(payload: DirectApplicationCreate, db: Session = Depends(get_db)) -> Dict[str, Any]:
    candidate = _get_or_404(db, CandidateProfile, payload.candidate_id)
    try:
        _job, application, created = DirectApplicationService().create(db, candidate, payload.job_url, occupy_slot=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if application.status == ApplicationState.SUBMITTED.value:
        return {**_application_payload(db, application), "accepted": False, "duplicate": True}
    return {**_application_payload(db, application), "accepted": False, "duplicate": not created}


@router.post("/applications/{application_id}/select-cv")
async def select_application_cv(
    application_id: int,
    payload: ApplicationCVSelectionRequest,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    async with orchestrator.mutation_lock(application_id):
        application = _get_or_404(db, Application, application_id)
        before_state = application.status
        before_actions = _allowed_application_actions(application)
        if "select_cv" not in before_actions:
            event(
                logger, "application.cv.selection_rejected", application_id=application.id,
                previous_state=before_state, action="select_cv", new_state=before_state,
                success=False, error_type="InvalidApplicationState",
                allowed_actions=before_actions,
            )
            raise HTTPException(status_code=409, detail="CV selection is not available in the current application state")
        document = _get_or_404(db, CandidateDocument, payload.selected_cv_document_id)
        if document.profile_id != application.candidate_id or document.kind != "CV":
            raise HTTPException(status_code=409, detail="The selected document is not a CV for this candidate")
        application.selected_cv_document_id = document.id
        application.document_versions = []
        transition_application(db, application, ApplicationState.READY.value, "cv_selected", {
            "document_id": document.id,
        })
        db.commit()
        db.refresh(application)
        after_actions = _allowed_application_actions(application)
        event(
            logger, "application.cv.selected", application_id=application.id,
            previous_state=before_state, action="select_cv", new_state=application.status,
            success=True, document_id=document.id,
            allowed_actions_before=before_actions, allowed_actions_after=after_actions,
        )
        return _application_payload(db, application)


@router.post("/applications/{application_id}/start", status_code=202)
async def start_application(application_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    async with orchestrator.mutation_lock(application_id):
        application = _get_or_404(db, Application, application_id)
        previous_state = application.status
        actions = _allowed_application_actions(application)
        event(
            logger, "application.start.requested", application_id=application.id,
            previous_state=previous_state, action="start", new_state=previous_state,
            selected_cv_document_id=application.selected_cv_document_id,
            allowed_actions=actions,
        )
        task = orchestrator.tasks.get(application.id)
        if application.status in RUNNING_STATES or (task is not None and not task.done()):
            result = _application_payload(db, application)
            result.update({"accepted": False, "already_running": True})
            return result
        if application.status == ApplicationState.SUBMITTED.value:
            event(
                logger, "application.start.rejected", application_id=application.id,
                previous_state=previous_state, action="start", new_state=previous_state,
                success=False, error_type="ApplicationAlreadySubmitted",
            )
            raise HTTPException(status_code=409, detail="Application was already submitted")
        document = db.get(CandidateDocument, application.selected_cv_document_id) if application.selected_cv_document_id else None
        if document is None or document.profile_id != application.candidate_id or document.kind != "CV":
            event(
                logger, "application.start.rejected", application_id=application.id,
                previous_state=previous_state, action="start", new_state=previous_state,
                success=False, error_type="CVPrerequisiteMissing",
            )
            raise HTTPException(status_code=409, detail="Select a valid candidate CV before starting")
        if db.get(Job, application.job_id) is None:
            raise HTTPException(status_code=409, detail="The application vacancy no longer exists")
        if "start" not in actions:
            event(
                logger, "application.start.rejected", application_id=application.id,
                previous_state=previous_state, action="start", new_state=previous_state,
                success=False, error_type="InvalidApplicationState",
                allowed_actions=actions,
            )
            raise HTTPException(status_code=409, detail="Start is not available in the current application state")
        application.started_at = application.started_at or datetime.utcnow()
        application.current_operation = "Opening job and application target"
        transition_application(db, application, ApplicationState.OPENING.value, "start_requested", {
            "selected_cv_document_id": document.id,
        })
        application.job.application_status = ApplicationState.OPENING.value
        db.commit()
        db.refresh(application)
        orchestrator.start(application.id)
        event(
            logger, "application.start.completed", application_id=application.id,
            previous_state=previous_state, action="start", new_state=application.status,
            success=True,
        )
        result = _application_payload(db, application)
        result.update({"accepted": True, "already_running": False})
        return result


@router.post("/applications/{application_id}/pause")
async def pause_application(application_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    application = _get_or_404(db, Application, application_id)
    if "pause" not in _allowed_application_actions(application):
        raise HTTPException(status_code=409, detail="Pause is not available in the current application state")
    paused_from = application.status
    await orchestrator.pause(application_id)
    db.refresh(application)
    application.review_data = {**(application.review_data or {}), "paused_from": paused_from}
    transition_application(db, application, ApplicationState.PAUSED.value, "user_paused")
    application.current_operation = "Paused by user"
    db.commit()
    event(logger, "application.action.pause", application_id=application.id, previous_state=paused_from)
    return {"paused": True, "status": application.status}


@router.post("/applications/{application_id}/resume", status_code=202)
async def resume_application(application_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    application = _get_or_404(db, Application, application_id)
    if application.status != ApplicationState.PAUSED.value and "resume" not in _allowed_application_actions(application):
        raise HTTPException(status_code=409, detail="Resume is not available in the current application state")
    await browser_manager.resume()
    if application.status == ApplicationState.PAUSED.value and not application.browser_page_id:
        transition_application(db, application, ApplicationState.READY.value, "resume_without_page")
        db.commit()
        orchestrator.start(application.id)
    else:
        orchestrator.resume(application.id)
    event(logger, "application.action.resume", application_id=application.id)
    return {"paused": False, "accepted": True}


@router.post("/applications/{application_id}/retry", status_code=202)
async def retry_application(application_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    application = _get_or_404(db, Application, application_id)
    if "retry" not in _allowed_application_actions(application):
        raise HTTPException(status_code=409, detail="Retry is not available in the current application state")
    await browser_manager.resume()
    if application.status in {ApplicationState.READY.value, ApplicationState.FAILED.value} and not application.browser_page_id:
        orchestrator.start(application.id)
    else:
        orchestrator.resume(application.id)
    return {"accepted": True, "status": application.status}


@router.post("/applications/{application_id}/manual")
async def request_manual_intervention(application_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    application = _get_or_404(db, Application, application_id)
    if "manual" not in _allowed_application_actions(application):
        raise HTTPException(status_code=409, detail="Manual help is not available in the current application state")
    await orchestrator.pause(application_id)
    db.refresh(application)
    application.review_data = {**(application.review_data or {}), "paused_from": application.status}
    transition_application(db, application, ApplicationState.PAUSED.value, "manual_intervention_requested")
    application.current_operation = "Manual intervention requested; automation is paused"
    application.error = "Use Open browser, make the necessary change, then Resume"
    db.commit()
    return {"paused": True, "status": application.status, "manual_intervention": True}


@router.post("/applications/{application_id}/open-browser")
async def open_browser(application_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    application = _get_or_404(db, Application, application_id)
    if "open-browser" not in _allowed_application_actions(application):
        raise HTTPException(status_code=409, detail="The application browser page is not available")
    await browser_manager.focus(application.browser_page_id)
    return {"focused": True}


@router.post("/applications/{application_id}/abort")
async def abort_application(application_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    application = _get_or_404(db, Application, application_id)
    if "abort" not in _allowed_application_actions(application):
        raise HTTPException(status_code=409, detail="Abort is not available in the current application state")
    await orchestrator.release(application_id)
    db.refresh(application)
    transition_application(db, application, ApplicationState.ABORTED.value, "user_aborted")
    application.browser_page_id = None
    application.job.application_status = "ABORTED"
    db.commit()
    return {"id": application.id, "status": application.status}


@router.post("/applications/{application_id}/reset")
async def reset_application(application_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    async with orchestrator.mutation_lock(application_id):
        application = _get_or_404(db, Application, application_id)
        previous_state = application.status
        if "reset" not in _allowed_application_actions(application):
            raise HTTPException(status_code=409, detail="Reset is not available in the current application state")
        event(
            logger, "application.reset.requested", application_id=application.id,
            previous_state=previous_state, action="reset", new_state=previous_state,
        )
        try:
            await orchestrator.release(application_id)
        except Exception as exc:
            event(
                logger, "application.reset.runtime_cleanup", application_id=application.id,
                previous_state=previous_state, action="reset", new_state=previous_state,
                success=False, error_type=type(exc).__name__,
            )
            raise HTTPException(status_code=409, detail="Application workflow could not be stopped safely for reset") from exc
        db.refresh(application)
        application = ApplicationLifecycleService().reset(db, application)
        event(
            logger, "application.reset.completed", application_id=application.id,
            previous_state=previous_state, action="reset", new_state=application.status,
            success=True, allowed_actions=_allowed_application_actions(application),
        )
        return _application_payload(db, application)


@router.delete("/applications/{application_id}", status_code=204)
async def delete_application(application_id: int, db: Session = Depends(get_db)) -> Response:
    async with orchestrator.mutation_lock(application_id):
        application = db.get(Application, application_id)
        if application is None:
            event(
                logger, "application.delete.completed", application_id=application_id,
                previous_state=None, action="delete", new_state=None,
                success=True, already_deleted=True,
            )
            return Response(status_code=204)
        previous_state = application.status
        event(
            logger, "application.delete.requested", application_id=application_id,
            previous_state=previous_state, action="delete", new_state=None,
        )
        orchestrator.begin_delete(application_id)
        try:
            released = await orchestrator.release(application_id)
            event(
                logger, "application.delete.worker_stopped", application_id=application_id,
                previous_state=previous_state, action="delete", new_state=None,
                success=True, runtime_released=bool(released),
            )
        except Exception as exc:
            event(
                logger, "application.delete.failed", application_id=application_id,
                previous_state=previous_state, action="delete", new_state=None,
                success=False, error_type=type(exc).__name__,
            )
            orchestrator.cancel_delete(application_id)
            raise HTTPException(status_code=409, detail="Application workflow could not be stopped safely") from exc
        try:
            db.refresh(application)
            deleted = ApplicationLifecycleService().delete(db, application)
            event(
                logger, "application.delete.children_deleted", application_id=application_id,
                previous_state=previous_state, action="delete", new_state=None,
                success=True, child_counts=deleted,
            )
            orchestrator.forget_application(application_id)
            event(
                logger, "application.delete.completed", application_id=application_id,
                previous_state=previous_state, action="delete", new_state=None,
                success=True,
            )
            return Response(status_code=204)
        except Exception as exc:
            db.rollback()
            orchestrator.cancel_delete(application_id)
            event(
                logger, "application.delete.failed", application_id=application_id,
                previous_state=previous_state, action="delete", new_state=previous_state,
                success=False, error_type=type(exc).__name__,
            )
            raise


@router.put("/applications/{application_id}/answers/{answer_id}")
async def edit_answer(application_id: int, answer_id: int, payload: AnswerEdit) -> Dict[str, Any]:
    try:
        application = await orchestrator.edit_answer(application_id, answer_id, payload.value)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": application.id, "status": application.status, "review_data": application.review_data}


@router.post("/applications/{application_id}/submit")
async def submit_application(application_id: int) -> Dict[str, Any]:
    try:
        application = await orchestrator.submit(application_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": application.id, "status": application.status, "submitted_at": application.submitted_at}


@router.get("/applications/{application_id}/debug/form")
async def debug_form(application_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    application = _get_or_404(db, Application, application_id)
    try:
        page = browser_manager.get_page(application.browser_page_id)
        form, registry = await BrowserFormScanner().scan(page)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "semantic": form.model_dump(),
        "locators": {key: {"strategy": value.strategy, "value": value.value, "frame": value.frame, "confidence": value.confidence} for key, value in registry.fields.items()},
    }


@router.get("/applications/{application_id}/debug/html")
async def debug_html(application_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    application = _get_or_404(db, Application, application_id)
    page = browser_manager.get_page(application.browser_page_id)
    html = sanitize_html(await page.content(), 30000, "form")
    return {"url": page.url, "chars": len(html), "html": html}


@router.get("/browser")
def browser_status() -> Dict[str, Any]:
    return {
        "running": browser_manager.context is not None,
        "paused": browser_manager.paused,
        "session_key": browser_manager.session_key,
        "pages": browser_manager.page_summary(),
        "stealth": browser_manager.stealth_status.payload(),
        "cdp": browser_manager.chrome.payload(),
        "challenge": browser_manager.active_challenge.payload() if browser_manager.active_challenge else None,
    }


@router.post("/browser/start")
async def start_browser() -> Dict[str, Any]:
    await browser_manager.ensure_started()
    return {"running": True, "pages": browser_manager.page_summary()}


@router.get("/diagnostics")
async def diagnostics(db: Session = Depends(get_db)) -> Dict[str, Any]:
    requests = int(db.execute(select(func.count()).select_from(AIRequestLog).where(AIRequestLog.cache_hit.is_(False))).scalar_one())
    input_chars = int(db.execute(select(func.coalesce(func.sum(AIRequestLog.input_chars), 0))).scalar_one())
    output_chars = int(db.execute(select(func.coalesce(func.sum(AIRequestLog.output_chars), 0))).scalar_one())
    cache_hits = int(db.execute(select(func.count()).select_from(AIRequestLog).where(AIRequestLog.cache_hit.is_(True))).scalar_one())
    scans = list(db.execute(select(JobScan).order_by(JobScan.id.desc()).limit(20)).scalars())
    return {
        "ai": {"requests": requests, "input_chars": input_chars, "output_chars": output_chars, "cache_hits": cache_hits},
        "scanner": {
            "jobs_avoided_by_deduplication": sum(item.unchanged_count for item in scans),
            "new_jobs": sum(item.new_count for item in scans),
            "changed_jobs": sum(item.changed_count for item in scans),
        },
        "browser": browser_status(),
        "codex": await CodexExecutionService().diagnose(False),
        "runtime": {"configured": settings.ai_runtime, "fallback": settings.ai_runtime_fallback},
        "ai_configuration": await ai_health(probe=False),
    }


@router.post("/diagnostics/browser-self-test")
async def browser_diagnostic() -> Dict[str, Any]:
    return await run_browser_self_test()


@router.post("/diagnostics/codex-self-test")
async def codex_diagnostic() -> Dict[str, Any]:
    return await CodexExecutionService().diagnose(True)


@router.websocket("/events")
async def events_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = event_hub.subscribe("applications")
    try:
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        pass
    finally:
        event_hub.unsubscribe("applications", queue)
