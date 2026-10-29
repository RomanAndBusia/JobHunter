import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api.router import router
from app.browser.manager import browser_manager
from app.errors import JobHunterError
from app.config import SettingsError, settings, validate_ai_configuration
from app.db.session import Base, SessionLocal, engine
from app.logging import configure_logging
from app.models import Application, CandidateProfile, SearchProfile, SearchCampaign, JobScan


logger = logging.getLogger(__name__)
DEFAULT_SEARCHES = [
    "Security", "SOC", "Python", "IT Support",
]


def initialize_database() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if db.execute(select(CandidateProfile).limit(1)).scalar_one_or_none() is None:
            db.add(CandidateProfile(name="Default candidate", data={
                "identity": {}, "contact": {}, "location": {}, "education": [],
                "employment": [], "projects": [], "skills": [], "languages": [],
                "links": {}, "work_authorization": {}, "preferences": {},
                "career_goal": {"primary": "Get first professional IT experience"},
            }))
        existing = set(db.execute(select(SearchProfile.name)).scalars())
        for query in DEFAULT_SEARCHES:
            if query not in existing:
                db.add(SearchProfile(name=query, query=query, max_pages=2))
        db.commit()


def recover_interrupted_work() -> None:
    from app.applications.state_machine import RUNNING_STATES, transition_application
    with SessionLocal() as db:
        for application in db.execute(select(Application).where(Application.status.in_(RUNNING_STATES))).scalars():
            previous = application.status
            target = 'NEEDS_USER' if previous == 'SUBMITTING' else 'REVIEW_REQUIRED' if previous == 'APPROVED' else 'PAUSED'
            application.review_data = {**(application.review_data or {}), 'paused_from': previous}
            application.current_operation = 'Interrupted by backend restart'
            application.error = 'Resume to inspect the existing Chrome page.' if previous != 'SUBMITTING' else 'Submission outcome is unknown. Inspect the website before continuing.'
            transition_application(db, application, target, 'backend_restart')
        for campaign in db.execute(select(SearchCampaign).where(SearchCampaign.status.in_(['SEARCHING', 'EVALUATING', 'PREPARING_APPLICATIONS']))).scalars():
            campaign.status = 'INTERRUPTED'
        for scan in db.execute(select(JobScan).where(JobScan.completed_at.is_(None))).scalars():
            if (scan.details or {}).get('source') == 'justjoin' and scan.status not in {'WAITING_FOR_LOGIN', 'WAITING_FOR_CHALLENGE', 'ERROR', 'INTERRUPTED'}:
                scan.status = 'INTERRUPTED'
        db.commit()


async def _autostart_browser() -> None:
    try:
        await browser_manager.ensure_started()
    except Exception:
        logger.exception("Visible browser could not start; install Google Chrome and use POST /api/browser/start")


def validate_startup_configuration() -> None:
    diagnostic = validate_ai_configuration(settings)
    if diagnostic.valid:
        logger.info(
            "AI configuration valid: runtime=%s provider=%s model=%s env=%s",
            settings.ai_runtime,
            settings.ai_provider,
            settings.ai_model,
            settings.env_file,
        )
        return
    logger.error(
        "AI configuration invalid: %s: %s",
        diagnostic.error_type,
        diagnostic.error_message,
    )
    # A missing credential is recoverable through Settings diagnostics; malformed
    # runtime/provider semantics must stop startup rather than create a wrong provider.
    if diagnostic.error_type != "MissingAPIKey":
        raise SettingsError(diagnostic.error_message or "AI configuration is invalid.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    validate_startup_configuration()
    initialize_database()
    recover_interrupted_work()
    browser_task = asyncio.create_task(_autostart_browser()) if settings.browser_autostart else None
    yield
    if browser_task is not None and not browser_task.done():
        browser_task.cancel()
    from app.search.runner import campaign_runner
    await campaign_runner.stop()
    from app.search.discovery import discovery_runner
    await discovery_runner.stop()
    # Application owners must finish cancellation before browser disconnection.
    from app.applications.orchestrator import orchestrator
    tasks = list(orchestrator.tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await browser_manager.stop()


app = FastAPI(title="JobHunter API", version="0.1.0", lifespan=lifespan)


@app.exception_handler(JobHunterError)
async def jobhunter_error_handler(_request: Request, exc: JobHunterError) -> JSONResponse:
    error = exc.safe_payload()
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.safe_message,
            "error": error,
            "message": exc.safe_message,
            "stage": exc.stage,
        },
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
