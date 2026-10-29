from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job


@dataclass
class UpsertResult:
    job: Job
    disposition: str


class JobRepository:
    def __init__(self, db: Session):
        self.db = db

    def find(self, source: str, source_job_id: str) -> Optional[Job]:
        return self.db.execute(
            select(Job).where(Job.source == source, Job.source_job_id == source_job_id)
        ).scalar_one_or_none()

    def upsert(self, payload: Dict[str, Any]) -> UpsertResult:
        payload = deepcopy(payload)
        now = datetime.utcnow()
        existing = self.find(payload["source"], payload["source_job_id"])
        if existing is None:
            job = Job(**payload)
            job.first_seen_at = now
            job.last_seen_at = now
            self.db.add(job)
            self.db.flush()
            return UpsertResult(job=job, disposition="new")

        existing.last_seen_at = now
        seen = list(dict.fromkeys((existing.raw_metadata or {}).get('sources_seen', []) + (payload.get('raw_metadata') or {}).get('sources_seen', [])))
        if existing.content_hash == payload["content_hash"]:
            if seen:
                existing.raw_metadata = {**(existing.raw_metadata or {}), 'sources_seen': seen}
            self.db.flush()
            return UpsertResult(job=existing, disposition="unchanged")

        preserved = {"first_seen_at", "matching_status", "application_status"}
        retained_metadata = {key: value for key, value in (existing.raw_metadata or {}).items()
                             if key in {"operator_skip", "original_job_url", "application_source", "application_type", "external_application_url", "final_ats_url", "provider"}}
        for key, value in payload.items():
            if key not in preserved:
                setattr(existing, key, value)
        existing.raw_metadata = {**(existing.raw_metadata or {}), **retained_metadata}
        if seen:
            existing.raw_metadata = {**existing.raw_metadata, 'sources_seen': seen}
        existing.matching_status = "PENDING"
        existing.ai_checked_at = None
        self.db.flush()
        return UpsertResult(job=existing, disposition="changed")
