import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Column,
)
from sqlalchemy.orm import relationship

from app.db.session import Base


def utcnow():
    return datetime.utcnow()


class ApplicationState(str, enum.Enum):
    READY = "READY"
    PAUSED = "PAUSED"
    OPENING = "OPENING"
    WAITING_FOR_LOGIN = "WAITING_FOR_LOGIN"
    FORM_DISCOVERY = "FORM_DISCOVERY"
    FILLING = "FILLING"
    VERIFYING = "VERIFYING"
    NEEDS_USER = "NEEDS_USER"
    CAPTCHA = "CAPTCHA"
    UNKNOWN_FORM = "UNKNOWN_FORM"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    EXPIRED = "EXPIRED"


class SearchProfile(Base):
    __tablename__ = "search_profiles"
    id = Integer().with_variant(Integer, "sqlite")
    id = __import__("sqlalchemy").Column(id, primary_key=True)
    name = __import__("sqlalchemy").Column(String(120), nullable=False, unique=True)
    query = __import__("sqlalchemy").Column(String(240), nullable=False)
    location = __import__("sqlalchemy").Column(String(160), nullable=True)
    work_modes = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    employment_types = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    seniority = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    max_pages = __import__("sqlalchemy").Column(Integer, nullable=False, default=3)
    enabled = __import__("sqlalchemy").Column(Boolean, nullable=False, default=True)
    created_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source", "source_job_id", name="uq_job_source_id"),
        Index("ix_job_fingerprint", "fingerprint"),
        Index("ix_job_content_hash", "content_hash"),
    )
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    source = __import__("sqlalchemy").Column(String(40), nullable=False)
    source_job_id = __import__("sqlalchemy").Column(String(160), nullable=False)
    canonical_url = __import__("sqlalchemy").Column(Text, nullable=False)
    fingerprint = __import__("sqlalchemy").Column(String(64), nullable=False)
    content_hash = __import__("sqlalchemy").Column(String(64), nullable=False)
    title = __import__("sqlalchemy").Column(String(300), nullable=False)
    company = __import__("sqlalchemy").Column(String(300), nullable=False, default="")
    location = __import__("sqlalchemy").Column(String(300), nullable=False, default="")
    work_mode = __import__("sqlalchemy").Column(String(40), nullable=True)
    employment_type = __import__("sqlalchemy").Column(String(80), nullable=True)
    seniority = __import__("sqlalchemy").Column(String(80), nullable=True)
    category = __import__("sqlalchemy").Column(String(100), nullable=True)
    description_normalized = __import__("sqlalchemy").Column(Text, nullable=False, default="")
    requirements = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    nice_to_have = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    responsibilities = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    compact_json = __import__("sqlalchemy").Column(JSON, nullable=False, default=dict)
    raw_metadata = __import__("sqlalchemy").Column(JSON, nullable=False, default=dict)
    matching_status = __import__("sqlalchemy").Column(String(32), nullable=False, default="PENDING")
    application_status = __import__("sqlalchemy").Column(String(32), nullable=False, default="NONE")
    first_seen_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow)
    last_seen_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow)
    details_fetched_at = __import__("sqlalchemy").Column(DateTime, nullable=True)
    content_checked_at = __import__("sqlalchemy").Column(DateTime, nullable=True)
    ai_checked_at = __import__("sqlalchemy").Column(DateTime, nullable=True)
    is_active = __import__("sqlalchemy").Column(Boolean, nullable=False, default=True)
    matches = relationship("JobMatch", back_populates="job", cascade="all, delete-orphan")
    applications = relationship("Application", back_populates="job")

    @property
    def evaluation(self):
        current = [item for item in self.matches if item.content_hash == self.content_hash]
        return dict(max(current, key=lambda item: item.id or 0).evaluation or {}) if current else {}


class JobScan(Base):
    __tablename__ = "job_scans"
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    profile_id = __import__("sqlalchemy").Column(Integer, ForeignKey("search_profiles.id"), nullable=True)
    started_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow)
    completed_at = __import__("sqlalchemy").Column(DateTime, nullable=True)
    status = __import__("sqlalchemy").Column(String(32), nullable=False, default="RUNNING")
    seen_count = __import__("sqlalchemy").Column(Integer, nullable=False, default=0)
    new_count = __import__("sqlalchemy").Column(Integer, nullable=False, default=0)
    changed_count = __import__("sqlalchemy").Column(Integer, nullable=False, default=0)
    unchanged_count = __import__("sqlalchemy").Column(Integer, nullable=False, default=0)
    error_count = __import__("sqlalchemy").Column(Integer, nullable=False, default=0)
    details = __import__("sqlalchemy").Column(JSON, nullable=False, default=dict)


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    name = __import__("sqlalchemy").Column(String(160), nullable=False, default="Default candidate")
    data = __import__("sqlalchemy").Column(JSON, nullable=False, default=dict)
    confirmed_at = __import__("sqlalchemy").Column(DateTime, nullable=True)
    updated_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    facts = relationship("CandidateFact", back_populates="profile", cascade="all, delete-orphan")
    documents = relationship("CandidateDocument", back_populates="profile", cascade="all, delete-orphan")


class CandidateFact(Base):
    __tablename__ = "candidate_facts"
    __table_args__ = (UniqueConstraint("profile_id", "path", name="uq_candidate_fact_path"),)
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    profile_id = __import__("sqlalchemy").Column(Integer, ForeignKey("candidate_profiles.id"), nullable=False)
    path = __import__("sqlalchemy").Column(String(300), nullable=False)
    value = __import__("sqlalchemy").Column(JSON, nullable=False)
    source = __import__("sqlalchemy").Column(String(40), nullable=False)
    confidence = __import__("sqlalchemy").Column(Float, nullable=False, default=1.0)
    confirmed_at = __import__("sqlalchemy").Column(DateTime, nullable=True)
    concept = Column(String(160), nullable=True, index=True)
    user_confirmed = Column(Boolean, nullable=False, default=False)
    valid_from = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    last_confirmed_at = Column(DateTime, nullable=True)
    profile = relationship("CandidateProfile", back_populates="facts")


class CandidateDocument(Base):
    __tablename__ = "candidate_documents"
    __table_args__ = (UniqueConstraint("profile_id", "kind", "version", name="uq_document_version"),)
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    profile_id = __import__("sqlalchemy").Column(Integer, ForeignKey("candidate_profiles.id"), nullable=False)
    kind = __import__("sqlalchemy").Column(String(40), nullable=False)
    version = __import__("sqlalchemy").Column(Integer, nullable=False)
    original_name = __import__("sqlalchemy").Column(String(300), nullable=False)
    storage_path = __import__("sqlalchemy").Column(Text, nullable=False)
    sha256 = __import__("sqlalchemy").Column(String(64), nullable=False)
    extracted_text = __import__("sqlalchemy").Column(Text, nullable=False, default="")
    active = __import__("sqlalchemy").Column(Boolean, nullable=False, default=True)
    created_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow)
    profile = relationship("CandidateProfile", back_populates="documents")


class JobMatch(Base):
    __tablename__ = "job_matches"
    __table_args__ = (UniqueConstraint("job_id", "content_hash", "profile_hash", name="uq_match_cache"),)
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    job_id = __import__("sqlalchemy").Column(Integer, ForeignKey("jobs.id"), nullable=False)
    profile_hash = __import__("sqlalchemy").Column(String(64), nullable=False)
    content_hash = __import__("sqlalchemy").Column(String(64), nullable=False)
    accepted = __import__("sqlalchemy").Column(Boolean, nullable=False)
    evaluation = Column(JSON, nullable=False, default=dict)
    reason = __import__("sqlalchemy").Column(String(600), nullable=False)
    blockers = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    score = __import__("sqlalchemy").Column(Float, nullable=True)
    provider = __import__("sqlalchemy").Column(String(80), nullable=False)
    model = __import__("sqlalchemy").Column(String(120), nullable=False)
    created_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow)
    job = relationship("Job", back_populates="matches")


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("candidate_id", "job_id", name="uq_candidate_job_application"),
        UniqueConstraint("candidate_id", "direct_slot_active", name="uq_candidate_direct_slot"),
    )
    # True owns the Apply-by-URL slot; NULL means detached (multiple NULLs allowed).
    direct_slot_active = Column(Boolean, nullable=True)
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    candidate_id = __import__("sqlalchemy").Column(Integer, ForeignKey("candidate_profiles.id"), nullable=False)
    job_id = __import__("sqlalchemy").Column(Integer, ForeignKey("jobs.id"), nullable=False)
    url_fingerprint = __import__("sqlalchemy").Column(String(64), nullable=True, index=True)
    selected_cv_document_id = __import__("sqlalchemy").Column(
        Integer, ForeignKey("candidate_documents.id", ondelete="SET NULL"), nullable=True,
    )
    status = __import__("sqlalchemy").Column(String(32), nullable=False, default=ApplicationState.READY.value)
    target_type = __import__("sqlalchemy").Column(String(60), nullable=True)
    target_host = __import__("sqlalchemy").Column(String(240), nullable=True)
    target_url = __import__("sqlalchemy").Column(Text, nullable=True)
    browser_page_id = __import__("sqlalchemy").Column(String(80), nullable=True)
    current_step = __import__("sqlalchemy").Column(String(200), nullable=True)
    current_operation = __import__("sqlalchemy").Column(String(300), nullable=True)
    last_operation = __import__("sqlalchemy").Column(String(300), nullable=True)
    fallback_level = __import__("sqlalchemy").Column(Integer, nullable=False, default=0)
    navigation_history = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    document_versions = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    review_data = __import__("sqlalchemy").Column(JSON, nullable=False, default=dict)
    error = __import__("sqlalchemy").Column(Text, nullable=True)
    started_at = __import__("sqlalchemy").Column(DateTime, nullable=True)
    reviewed_at = __import__("sqlalchemy").Column(DateTime, nullable=True)
    approved_at = __import__("sqlalchemy").Column(DateTime, nullable=True)
    submitted_at = __import__("sqlalchemy").Column(DateTime, nullable=True)
    created_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow)
    updated_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    job = relationship("Job", back_populates="applications")
    candidate = relationship("CandidateProfile")
    selected_cv_document = relationship("CandidateDocument", foreign_keys=[selected_cv_document_id])
    steps = relationship("ApplicationStep", back_populates="application", cascade="all, delete-orphan")
    answers = relationship("ApplicationAnswer", back_populates="application", cascade="all, delete-orphan")
    snapshots = relationship("FormSnapshot", back_populates="application", cascade="all, delete-orphan")
    question_requests = relationship("ApplicationQuestionRequest", back_populates="application", cascade="all, delete-orphan")


class ApplicationStep(Base):
    __tablename__ = "application_steps"
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    application_id = __import__("sqlalchemy").Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False)
    sequence = __import__("sqlalchemy").Column(Integer, nullable=False)
    state_from = __import__("sqlalchemy").Column(String(32), nullable=True)
    state_to = __import__("sqlalchemy").Column(String(32), nullable=False)
    action = __import__("sqlalchemy").Column(String(120), nullable=False)
    details = __import__("sqlalchemy").Column(JSON, nullable=False, default=dict)
    success = __import__("sqlalchemy").Column(Boolean, nullable=False, default=True)
    created_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow)
    application = relationship("Application", back_populates="steps")


class ApplicationAnswer(Base):
    __tablename__ = "application_answers"
    __table_args__ = (UniqueConstraint("application_id", "field_id", name="uq_application_field_answer"),)
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    application_id = __import__("sqlalchemy").Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False)
    field_id = __import__("sqlalchemy").Column(String(80), nullable=False)
    question = __import__("sqlalchemy").Column(Text, nullable=False)
    value = __import__("sqlalchemy").Column(JSON, nullable=False)
    source = __import__("sqlalchemy").Column(String(40), nullable=False)
    semantic_meaning = __import__("sqlalchemy").Column(Text, nullable=True)
    source_paths = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    confidence = __import__("sqlalchemy").Column(Float, nullable=True)
    execution_confidence = __import__("sqlalchemy").Column(String(32), nullable=False, default="UNKNOWN")
    reason = __import__("sqlalchemy").Column(Text, nullable=True)
    status = __import__("sqlalchemy").Column(String(32), nullable=False, default="PLANNED")
    verified = __import__("sqlalchemy").Column(Boolean, nullable=False, default=False)
    manual_override = __import__("sqlalchemy").Column(Boolean, nullable=False, default=False)
    updated_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    application = relationship("Application", back_populates="answers")


class BrowserSession(Base):
    __tablename__ = "browser_sessions"
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    session_key = __import__("sqlalchemy").Column(String(80), nullable=False, unique=True)
    status = __import__("sqlalchemy").Column(String(32), nullable=False)
    pages = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    started_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow)
    ended_at = __import__("sqlalchemy").Column(DateTime, nullable=True)


class AIRequestLog(Base):
    __tablename__ = "ai_request_logs"
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    operation = __import__("sqlalchemy").Column(String(80), nullable=False)
    provider = __import__("sqlalchemy").Column(String(80), nullable=False)
    model = __import__("sqlalchemy").Column(String(120), nullable=False)
    cache_key = __import__("sqlalchemy").Column(String(64), nullable=True, index=True)
    cache_hit = __import__("sqlalchemy").Column(Boolean, nullable=False, default=False)
    input_chars = __import__("sqlalchemy").Column(Integer, nullable=False, default=0)
    output_chars = __import__("sqlalchemy").Column(Integer, nullable=False, default=0)
    success = __import__("sqlalchemy").Column(Boolean, nullable=False, default=True)
    error_type = __import__("sqlalchemy").Column(String(100), nullable=True)
    created_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow)


class FormSnapshot(Base):
    __tablename__ = "form_snapshots"
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    application_id = __import__("sqlalchemy").Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False)
    url = __import__("sqlalchemy").Column(Text, nullable=False)
    host = __import__("sqlalchemy").Column(String(240), nullable=False)
    step = __import__("sqlalchemy").Column(String(120), nullable=True)
    semantic_form = __import__("sqlalchemy").Column(JSON, nullable=False)
    validation_errors = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    fallback_level = __import__("sqlalchemy").Column(Integer, nullable=False, default=0)
    fingerprint = __import__("sqlalchemy").Column(String(64), nullable=False, index=True)
    created_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow)
    application = relationship("Application", back_populates="snapshots")


class QuestionMemory(Base):
    __tablename__ = "question_memory"
    __table_args__ = (UniqueConstraint("candidate_id", "concept", name="uq_candidate_question_concept"),)
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    candidate_id = __import__("sqlalchemy").Column(Integer, ForeignKey("candidate_profiles.id"), nullable=False)
    concept = __import__("sqlalchemy").Column(String(160), nullable=False)
    question_fingerprint = __import__("sqlalchemy").Column(String(64), nullable=False)
    value = __import__("sqlalchemy").Column(JSON, nullable=False)
    source = __import__("sqlalchemy").Column(String(40), nullable=False)
    confidence = __import__("sqlalchemy").Column(Float, nullable=False, default=1.0)
    confirmed_at = __import__("sqlalchemy").Column(DateTime, nullable=True)
    aliases = Column(JSON, nullable=False, default=list)
    raw_user_value = Column(JSON, nullable=True)
    presentation_value = Column(JSON, nullable=True)
    scope = Column(String(32), nullable=False, default="GLOBAL")
    scope_key = Column(String(200), nullable=True)
    valid_from = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    last_confirmed_at = Column(DateTime, nullable=True)
    history = Column(JSON, nullable=False, default=list)


class FactAlias(Base):
    __tablename__ = "fact_aliases"
    __table_args__ = (UniqueConstraint("candidate_id", "concept", "normalized_alias", name="uq_fact_alias"),)
    id = Column(Integer, primary_key=True)
    candidate_id = Column(Integer, ForeignKey("candidate_profiles.id"), nullable=False)
    concept = Column(String(160), nullable=False, index=True)
    alias = Column(Text, nullable=False)
    normalized_alias = Column(Text, nullable=False)
    language = Column(String(12), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class QuestionConcept(Base):
    __tablename__ = "question_concepts"
    id = Column(Integer, primary_key=True)
    key = Column(String(160), nullable=False, unique=True)
    value_type = Column(String(40), nullable=False, default="text")
    entailment = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class ApplicationQuestionRequest(Base):
    __tablename__ = "application_question_requests"
    __table_args__ = (UniqueConstraint("application_id", "field_id", name="uq_application_question_field"),)
    id = Column(Integer, primary_key=True)
    application_id = Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    candidate_id = Column(Integer, ForeignKey("candidate_profiles.id"), nullable=False)
    field_id = Column(String(80), nullable=False)
    question = Column(Text, nullable=False)
    concept = Column(String(160), nullable=True, index=True)
    fact_path = Column(String(300), nullable=True)
    answer_type = Column(String(40), nullable=False, default="text")
    field_context = Column(JSON, nullable=False, default=dict)
    options = Column(JSON, nullable=False, default=list)
    status = Column(String(32), nullable=False, default="WAITING_FOR_USER", index=True)
    scope = Column(String(32), nullable=False, default="GLOBAL")
    raw_value = Column(JSON, nullable=True)
    presentation_value = Column(JSON, nullable=True)
    conflict = Column(JSON, nullable=True)
    question_category = Column(String(40), nullable=False, default="FACTUAL")
    suggested_value = Column(JSON, nullable=True)
    suggestion_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    answered_at = Column(DateTime, nullable=True)
    application = relationship("Application", back_populates="question_requests")


class RoleFamily(Base):
    __tablename__ = "role_families"
    id = Column(Integer, primary_key=True)
    key = Column(String(80), nullable=False, unique=True)
    label = Column(String(160), nullable=False)
    aliases = Column(JSON, nullable=False, default=list)


class SearchCampaign(Base):
    __tablename__ = "search_campaigns"
    id = Column(Integer, primary_key=True)
    candidate_id = Column(Integer, ForeignKey("candidate_profiles.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    goal = Column(String(200), nullable=False, default="first_it_experience")
    status = Column(String(32), nullable=False, default="DRAFT")
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class SearchPlan(Base):
    __tablename__ = "search_plans"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("search_campaigns.id"), nullable=False, unique=True)
    families = Column(JSON, nullable=False, default=list)
    rationale = Column(JSON, nullable=False, default=dict)
    user_edited = Column(Boolean, nullable=False, default=False)


class SearchQuery(Base):
    __tablename__ = "search_queries"
    __table_args__ = (UniqueConstraint("campaign_id", "normalized_query", name="uq_campaign_query"),)
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("search_campaigns.id"), nullable=False, index=True)
    role_family = Column(String(80), nullable=False)
    query = Column(String(240), nullable=False)
    normalized_query = Column(String(240), nullable=False)
    location = Column(String(160), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)


class SalaryPolicy(Base):
    __tablename__ = "salary_policies"
    id = Column(Integer, primary_key=True)
    candidate_id = Column(Integer, ForeignKey("candidate_profiles.id"), nullable=False, unique=True)
    strategy = Column(String(60), nullable=False, default="LOWEST_REASONABLE_AVAILABLE")
    experience_text_enabled = Column(Boolean, nullable=False, default=True)
    fallback_monthly_pln = Column(Float, nullable=True)
    fallback_hourly_pln = Column(Float, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class CodexRun(Base):
    __tablename__ = "codex_runs"
    id = Column(Integer, primary_key=True)
    task = Column(Text, nullable=False)
    status = Column(String(32), nullable=False, default="QUEUED", index=True)
    progress = Column(JSON, nullable=False, default=dict)
    result = Column(JSON, nullable=False, default=dict)
    diagnostics = Column(Text, nullable=False, default="")
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class ProcessStatus(Base):
    __tablename__ = "process_statuses"
    id = Column(Integer, primary_key=True)
    component = Column(String(80), nullable=False, unique=True)
    status = Column(String(32), nullable=False)
    pid = Column(Integer, nullable=True)
    details = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class UnknownComponent(Base):
    __tablename__ = "unknown_components"
    id = __import__("sqlalchemy").Column(Integer, primary_key=True)
    fingerprint = __import__("sqlalchemy").Column(String(64), nullable=False, index=True)
    host = __import__("sqlalchemy").Column(String(240), nullable=False)
    framework_hints = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    interaction_pattern = __import__("sqlalchemy").Column(JSON, nullable=False, default=dict)
    fallback_path = __import__("sqlalchemy").Column(JSON, nullable=False, default=list)
    successful = __import__("sqlalchemy").Column(Boolean, nullable=False, default=False)
    created_at = __import__("sqlalchemy").Column(DateTime, nullable=False, default=utcnow)
