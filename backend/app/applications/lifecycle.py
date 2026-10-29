from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Application,
    ApplicationAnswer,
    ApplicationQuestionRequest,
    ApplicationState,
    ApplicationStep,
    FormSnapshot,
    Job,
)


class ApplicationLifecycleService:
    """Reset or remove one vacancy-scoped application session only."""

    CHILDREN = (ApplicationAnswer, ApplicationQuestionRequest, FormSnapshot, ApplicationStep)

    def reset(self, db: Session, application: Application) -> Application:
        for model in self.CHILDREN:
            db.execute(delete(model).where(model.application_id == application.id))
        application.status = ApplicationState.READY.value
        application.target_type = None
        application.target_host = None
        application.target_url = None
        application.browser_page_id = None
        application.current_step = None
        application.current_operation = None
        application.last_operation = None
        application.fallback_level = 0
        application.navigation_history = []
        application.document_versions = []
        application.review_data = {"origin": (application.review_data or {}).get('origin', application.job.source), "ai_runtime": settings.ai_runtime}
        application.error = None
        application.started_at = None
        application.reviewed_at = None
        application.approved_at = None
        application.submitted_at = None
        application.job.application_status = ApplicationState.READY.value
        db.commit()
        db.refresh(application)
        from app.applications.cover_letters import generated_path
        generated_path(application.id).unlink(missing_ok=True)
        return application

    def delete(self, db: Session, application: Application):
        job = application.job
        job_id = job.id
        is_direct = job.source == "direct"
        deleted = {}
        for model in self.CHILDREN:
            result = db.execute(delete(model).where(model.application_id == application.id))
            deleted[model.__tablename__] = int(result.rowcount or 0)
        db.delete(application)
        db.flush()
        remaining_rows = list(db.execute(
            select(Application).where(Application.job_id == job_id).order_by(Application.updated_at.desc())
        ).scalars())
        if is_direct and not remaining_rows:
            db.delete(job)
        elif not remaining_rows:
            job.application_status = "NONE"
        else:
            job.application_status = remaining_rows[0].status
        db.commit()
        from app.applications.cover_letters import generated_path
        generated_path(application.id).unlink(missing_ok=True)
        if db.get(Application, application.id) is not None:
            raise RuntimeError("Application hard delete did not remove the root row")
        for model in self.CHILDREN:
            remaining = db.execute(
                select(func.count()).select_from(model).where(model.application_id == application.id)
            ).scalar_one()
            if remaining:
                raise RuntimeError("Application hard delete left rows in %s" % model.__tablename__)
        return deleted
