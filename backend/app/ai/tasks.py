import asyncio
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import func, select

from app.ai.codex import CodexExecutionService
from app.applications.orchestrator import orchestrator
from app.db.session import SessionLocal
from app.matching.service import MatchService
from app.models import Application, ApplicationQuestionRequest, ApplicationState, CandidateProfile, CodexRun, Job
from app.search.planner import build_search_campaign
from app.models import SearchProfile
from app.scanner.justjoin import JustJoinSource


SAFE_ACTIONS = (
    "scan_jobs", "get_candidate_profile", "list_new_jobs", "get_job", "classify_jobs", "start_suitable_applications",
    "inspect_application", "get_pending_questions", "answer_known_questions", "prepare_review", "list_reviews",
)


class CodexTaskService:
    def __init__(self) -> None:
        self.tasks: Dict[int, asyncio.Task] = {}

    def start(self, run_id: int) -> None:
        running = self.tasks.get(run_id)
        if running and not running.done():
            return
        task = asyncio.create_task(self._run(run_id))
        self.tasks[run_id] = task
        task.add_done_callback(lambda _task: self.tasks.pop(run_id, None))

    async def _plan(self, task: str) -> List[Dict[str, Any]]:
        schema = {
            "type": "object",
            "properties": {"actions": {"type": "array", "items": {
                "type": "object", "properties": {
                    "name": {"type": "string", "enum": list(SAFE_ACTIONS)},
                    "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 50},
                    "id": {"type": ["integer", "null"], "minimum": 1},
                }, "required": ["name", "limit", "id"], "additionalProperties": False,
            }, "maxItems": 12}}, "required": ["actions"], "additionalProperties": False,
        }
        prompt = (
            "Plan this JobHunter task using only the allowed high-level commands. The deterministic application owns the database, browser, form verification, and final submit. "
            "Never request final submission. Use get_candidate_profile before decisions, list/classify before application starts, and avoid duplicate actions. Task: " + task
        )
        result = await CodexExecutionService().run(prompt, schema)
        return list(result.get("actions") or [])

    async def _execute(self, actions: List[Dict[str, Any]], run: CodexRun, db: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {"actions": [], "jobs_discovered": 0, "jobs_accepted": 0, "applications_prepared": 0, "questions_needing_user": 0}
        candidate = db.execute(select(CandidateProfile).order_by(CandidateProfile.id)).scalars().first()
        if candidate is None:
            raise RuntimeError("No candidate profile exists")
        for index, action in enumerate(actions):
            name = action["name"]
            limit = int(action.get("limit") or 20)
            run.progress = {**result, "current_task": name, "step": index + 1, "total_steps": len(actions)}
            db.commit()
            item: Dict[str, Any] = {"name": name}
            if name == "get_candidate_profile":
                item["profile"] = {"id": candidate.id, "name": candidate.name, "skills": (candidate.data or {}).get("skills", []), "career_goal": (candidate.data or {}).get("career_goal", {})}
            elif name == "scan_jobs":
                profiles = list(db.execute(select(SearchProfile).where(SearchProfile.enabled.is_(True)).limit(limit)).scalars())
                item["scans"] = []
                for profile in profiles:
                    scan = await JustJoinSource(db).scan(profile)
                    item["scans"].append({"profile_id": profile.id, "status": scan.status, "new": scan.new_count, "changed": scan.changed_count, "errors": scan.error_count})
            elif name == "list_new_jobs":
                rows = list(db.execute(select(Job).where(Job.matching_status.in_(["PENDING", "AI_REQUIRED"])).order_by(Job.id.desc()).limit(limit)).scalars())
                item["jobs"] = [{"id": job.id, "title": job.title, "company": job.company, "status": job.matching_status} for job in rows]
                result["jobs_discovered"] = int(db.execute(select(func.count()).select_from(Job)).scalar_one())
            elif name == "get_job":
                job = db.get(Job, int(action.get("id") or 0))
                item["job"] = {"id": job.id, "title": job.title, "company": job.company, "url": job.canonical_url, "status": job.matching_status} if job else None
            elif name == "classify_jobs":
                match = await MatchService(db).run(candidate)
                item["matching"] = match
                result["jobs_accepted"] = int(db.execute(select(func.count()).select_from(Job).where(Job.matching_status == "ACCEPTED")).scalar_one())
            elif name == "start_suitable_applications":
                jobs = list(db.execute(select(Job).where(Job.matching_status == "ACCEPTED", Job.application_status == "NONE").order_by(Job.id).limit(limit)).scalars())
                started = []
                for job in jobs:
                    application = db.execute(select(Application).where(Application.candidate_id == candidate.id, Application.job_id == job.id)).scalar_one_or_none()
                    if application is None:
                        application = Application(candidate_id=candidate.id, job_id=job.id)
                        db.add(application)
                        db.flush()
                    job.application_status = "READY"
                    started.append(application.id)
                db.commit()
                for application_id in started:
                    orchestrator.start(application_id)
                item["application_ids"] = started
                result["applications_prepared"] += len(started)
            elif name == "get_pending_questions":
                rows = list(db.execute(select(ApplicationQuestionRequest).where(ApplicationQuestionRequest.status == "WAITING_FOR_USER").limit(limit)).scalars())
                item["questions"] = [{"id": row.id, "application_id": row.application_id, "question": row.question, "type": row.answer_type} for row in rows]
                result["questions_needing_user"] = len(rows)
            elif name == "inspect_application":
                application = db.get(Application, int(action.get("id") or 0))
                item["application"] = {"id": application.id, "status": application.status, "job_id": application.job_id, "review": application.review_data, "error": application.error} if application else None
            elif name == "answer_known_questions":
                rows = list(db.execute(select(Application).where(Application.status == ApplicationState.NEEDS_USER.value)).scalars())
                for application in rows:
                    if not db.execute(select(ApplicationQuestionRequest).where(ApplicationQuestionRequest.application_id == application.id, ApplicationQuestionRequest.status == "WAITING_FOR_USER")).scalars().first():
                        orchestrator.resume(application.id)
                item["resumed_application_ids"] = [application.id for application in rows]
            elif name == "prepare_review":
                rows = list(db.execute(select(Application).where(Application.status == ApplicationState.REVIEW_REQUIRED.value).limit(limit)).scalars())
                item["reviews"] = [{"id": row.id, "job_id": row.job_id} for row in rows]
            elif name == "list_reviews":
                rows = list(db.execute(select(Application).where(Application.status == ApplicationState.REVIEW_REQUIRED.value).limit(limit)).scalars())
                item["reviews"] = [{"id": row.id, "job_id": row.job_id, "status": row.status} for row in rows]
            result["actions"].append(item)
        return result

    async def _run(self, run_id: int) -> None:
        db = SessionLocal()
        try:
            run = db.get(CodexRun, run_id)
            if run is None:
                return
            run.status = "RUNNING"
            run.started_at = datetime.utcnow()
            run.progress = {"current_task": "planning"}
            db.commit()
            actions = await self._plan(run.task)
            run.result = await self._execute(actions, run, db)
            run.status = "COMPLETED"
            run.completed_at = datetime.utcnow()
            run.progress = {**run.result, "current_task": "completed"}
            db.commit()
        except Exception as exc:
            db.rollback()
            run = db.get(CodexRun, run_id)
            if run:
                run.status = "FAILED"
                run.error = "%s: %s" % (type(exc).__name__, str(exc))
                run.completed_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()


codex_tasks = CodexTaskService()
