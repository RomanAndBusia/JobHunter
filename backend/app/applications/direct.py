from typing import Tuple
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, ApplicationState, CandidateProfile, Job
from app.config import settings
from app.repositories.jobs import JobRepository
from app.utils.hashing import canonicalize_url, job_fingerprint, stable_hash


class DirectApplicationService:
    source = "direct"

    @staticmethod
    def active(db: Session, candidate_id: int):
        return db.execute(select(Application).where(
            Application.candidate_id == candidate_id,
            Application.direct_slot_active.is_(True),
        )).scalar_one_or_none()

    @staticmethod
    def validate_url(value: str) -> str:
        candidate = (value or "").strip()
        parts = urlsplit(candidate)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("Enter a complete http:// or https:// job URL.")
        if parts.username or parts.password:
            raise ValueError("Job URLs containing embedded credentials are not allowed.")
        return canonicalize_url(candidate)

    def create(self, db: Session, candidate: CandidateProfile, url: str, occupy_slot: bool = False) -> Tuple[Job, Application, bool]:
        canonical_url = self.validate_url(url)
        active = self.active(db, candidate.id)
        if occupy_slot and active is not None and active.job.canonical_url != canonical_url:
            raise ValueError("Use NEW APPLY before opening another job URL.")
        host = urlsplit(canonical_url).hostname or "unknown"
        source_job_id = stable_hash(canonical_url)[:40]
        placeholder = "Direct job at %s" % host
        repository = JobRepository(db)
        job = repository.find(self.source, source_job_id)
        if job is None:
            payload = {
                "source": self.source,
                "source_job_id": source_job_id,
                "canonical_url": canonical_url,
                "fingerprint": job_fingerprint(host, placeholder, ""),
                "content_hash": stable_hash({"url": canonical_url}),
                "title": placeholder,
                "company": host,
                "location": "",
                "description_normalized": "",
                "requirements": [],
                "nice_to_have": [],
                "responsibilities": [],
                "compact_json": {"url": canonical_url, "analysis_status": "pending"},
                "raw_metadata": {"source": "direct_url", "provider": "pending"},
                "matching_status": "PENDING",
                "application_status": "READY",
            }
            job = repository.upsert(payload).job
        application = db.execute(select(Application).where(
            Application.candidate_id == candidate.id,
            Application.job_id == job.id,
        )).scalar_one_or_none()
        created = application is None
        if application is None:
            application = Application(
                candidate_id=candidate.id,
                job_id=job.id,
                url_fingerprint=stable_hash(canonical_url),
                status=ApplicationState.READY.value,
                review_data={"origin": "direct_url", "ai_runtime": settings.ai_runtime},
            )
            db.add(application)
        elif not application.url_fingerprint:
            application.url_fingerprint = stable_hash(canonical_url)
        job.application_status = application.status or ApplicationState.READY.value
        if occupy_slot:
            application.direct_slot_active = True
            application.review_data = {**(application.review_data or {}), 'origin': 'direct_url'}
        db.commit()
        db.refresh(application)
        return job, application, created
