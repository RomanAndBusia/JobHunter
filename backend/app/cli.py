import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Dict

from sqlalchemy import select

from app.ai.codex import CodexExecutionService
from app.applications.orchestrator import orchestrator
from app.browser.worker_client import run_browser_self_test
from app.db.session import SessionLocal
from app.main import initialize_database
from app.matching.service import MatchService
from app.models import Application, ApplicationQuestionRequest, ApplicationState, CandidateProfile, Job
from app.models import SearchProfile
from app.scanner.justjoin import JustJoinSource


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=True, default=str, separators=(",", ":")))


async def run(args: argparse.Namespace) -> int:
    initialize_database()
    db = SessionLocal()
    try:
        candidate = db.execute(select(CandidateProfile).order_by(CandidateProfile.id)).scalars().first()
        if args.command == "profile":
            emit({"id": candidate.id, "name": candidate.name, "data": candidate.data})
        elif args.command == "scan":
            profiles = list(db.execute(select(SearchProfile).where(SearchProfile.enabled.is_(True))).scalars())
            scans = []
            for profile in profiles:
                scan = await JustJoinSource(db).scan(profile)
                scans.append({"profile_id": profile.id, "status": scan.status, "seen": scan.seen_count, "new": scan.new_count, "changed": scan.changed_count, "unchanged": scan.unchanged_count, "errors": scan.error_count})
            emit(scans)
        elif args.command == "jobs":
            query = select(Job)
            if args.kind == "new":
                query = query.where(Job.matching_status.in_(["PENDING", "AI_REQUIRED"]))
            elif args.kind == "suitable":
                query = query.where(Job.matching_status == "ACCEPTED")
            rows = list(db.execute(query.order_by(Job.id.desc()).limit(args.limit)).scalars())
            emit([{"id": row.id, "title": row.title, "company": row.company, "status": row.matching_status, "url": row.canonical_url} for row in rows])
        elif args.command == "classify":
            emit(await MatchService(db).run(candidate))
        elif args.command == "application":
            if args.action == "start":
                job = db.get(Job, args.id)
                if not job:
                    raise RuntimeError("Job not found")
                application = db.execute(select(Application).where(Application.candidate_id == candidate.id, Application.job_id == job.id)).scalar_one_or_none()
                if application is None:
                    application = Application(candidate_id=candidate.id, job_id=job.id)
                    db.add(application)
                    db.commit()
                    db.refresh(application)
                await orchestrator._run(application.id)
                db.refresh(application)
                emit({"application_id": application.id, "status": application.status})
            else:
                application = db.get(Application, args.id)
                emit({"id": application.id, "status": application.status, "current_step": application.current_step, "error": application.error})
        elif args.command == "questions":
            rows = db.execute(select(ApplicationQuestionRequest).where(ApplicationQuestionRequest.status == "WAITING_FOR_USER")).scalars()
            emit([{"id": row.id, "application_id": row.application_id, "concept": row.concept, "question": row.question, "type": row.answer_type} for row in rows])
        elif args.command == "reviews":
            rows = db.execute(select(Application).where(Application.status == ApplicationState.REVIEW_REQUIRED.value)).scalars()
            emit([{"id": row.id, "job_id": row.job_id, "review": row.review_data} for row in rows])
        elif args.command == "diagnose":
            if args.component == "browser":
                emit(await run_browser_self_test())
            else:
                emit(await CodexExecutionService().diagnose(args.self_test))
        elif args.command == 'diagnostic-run':
            from app.api.router import application_diagnostic_run, pause_application
            from app.browser.manager import browser_manager
            from app.services.pipeline_diagnostics import snapshot
            application = db.get(Application, args.application_id)
            if application is None:
                raise RuntimeError('Application not found')
            await application_diagnostic_run(application.id, db)
            task = orchestrator.tasks.get(application.id)
            if task:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=args.timeout)
                except asyncio.TimeoutError:
                    db.refresh(application)
                    if application.status in {'OPENING', 'FORM_DISCOVERY', 'FILLING', 'VERIFYING'}:
                        await pause_application(application.id, db)
                    else:
                        await orchestrator.release(application.id, close_page=False)
            db.expire_all()
            await browser_manager.inspect_session_state(application.browser_page_id)
            report = snapshot(db, db.get(Application, application.id), browser_manager)
            report['diagnostic_run'] = {'submission_authorized': False, 'timeout_seconds': args.timeout}
            if args.output:
                Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            emit(report)
            await browser_manager.stop()
        return 0
    finally:
        db.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Machine-friendly JobHunter command layer")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("profile")
    commands.add_parser("scan", help="scan all enabled search profiles")
    jobs = commands.add_parser("jobs")
    jobs.add_argument("kind", choices=["new", "suitable", "all"], default="new")
    jobs.add_argument("--limit", type=int, default=25)
    jobs.add_argument("--json", action="store_true", help="accepted for machine-friendly callers")
    commands.add_parser("classify")
    application = commands.add_parser("application")
    application.add_argument("action", choices=["start", "status"])
    application.add_argument("id", type=int, help="job id for start; application id for status")
    questions = commands.add_parser("questions")
    questions.add_argument("--json", action="store_true")
    commands.add_parser("reviews")
    diagnose = commands.add_parser("diagnose")
    diagnose.add_argument("component", choices=["browser", "codex"])
    diagnose.add_argument("--self-test", action="store_true")
    diagnostic = commands.add_parser('diagnostic-run', help='Run one idle application through the production owner; stop at review/questions, never approve or submit')
    diagnostic.add_argument('application_id', type=int)
    diagnostic.add_argument('--timeout', type=float, default=180)
    diagnostic.add_argument('--output', help='Write the redacted diagnostic snapshot as JSON')
    return root


if __name__ == "__main__":
    from app.logging import configure_logging
    configure_logging()
    raise SystemExit(asyncio.run(run(parser().parse_args())))
