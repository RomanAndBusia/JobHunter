from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SearchProfileCreate(BaseModel):
    name: str
    query: str
    location: Optional[str] = None
    work_modes: List[str] = Field(default_factory=list)
    employment_types: List[str] = Field(default_factory=list)
    seniority: List[str] = Field(default_factory=list)
    max_pages: int = Field(default=3, ge=1, le=20)
    enabled: bool = True


class SearchProfileOut(SearchProfileCreate, ORMModel):
    id: int
    created_at: datetime


class JobOut(ORMModel):
    id: int
    source: str
    source_job_id: str
    canonical_url: str
    title: str
    company: str
    location: str
    work_mode: Optional[str]
    employment_type: Optional[str]
    seniority: Optional[str]
    category: Optional[str]
    requirements: List[str]
    responsibilities: List[str]
    compact_json: Dict[str, Any]
    matching_status: str
    application_status: str
    first_seen_at: datetime
    last_seen_at: datetime
    evaluation: Dict[str, Any] = Field(default_factory=dict)
    raw_metadata: Dict[str, Any] = Field(default_factory=dict)


class CandidateProfileUpdate(BaseModel):
    name: str = "Default candidate"
    data: Dict[str, Any]
    confirmed: bool = False


class CandidateProfileOut(ORMModel):
    id: int
    name: str
    data: Dict[str, Any]
    confirmed_at: Optional[datetime]
    updated_at: datetime


class MatchRunRequest(BaseModel):
    candidate_id: int
    job_ids: Optional[List[int]] = None


class ApplicationCreate(BaseModel):
    candidate_id: int
    job_id: int


class ApplicationCVSelectionRequest(BaseModel):
    selected_cv_document_id: int


class DirectApplicationCreate(BaseModel):
    candidate_id: int
    job_url: str = Field(min_length=8, max_length=4000)


class AnswerEdit(BaseModel):
    value: Any


class QuestionAnswer(BaseModel):
    value: Any
    scope: Optional[str] = None
    confirm_conflict: bool = False
    synthetic_test: bool = False


class CodexTaskCreate(BaseModel):
    task: str = Field(min_length=3, max_length=2000)


class SearchCampaignCreate(BaseModel):
    candidate_id: int
    name: str = "CV-driven IT search"


class ApplicationOut(ORMModel):
    direct_slot_active: Optional[bool] = None
    id: int
    candidate_id: int
    job_id: int
    url_fingerprint: Optional[str]
    selected_cv_document_id: Optional[int]
    status: str
    target_type: Optional[str]
    target_host: Optional[str]
    target_url: Optional[str]
    current_step: Optional[str]
    current_operation: Optional[str]
    last_operation: Optional[str]
    fallback_level: int
    document_versions: List[Dict[str, Any]]
    review_data: Dict[str, Any]
    error: Optional[str]
    started_at: Optional[datetime]
    reviewed_at: Optional[datetime]
    submitted_at: Optional[datetime]
    created_at: datetime


class SemanticOption(BaseModel):
    id: str
    label: str
    selected: bool = False
    disabled: bool = False


class SemanticField(BaseModel):
    id: str
    question: str
    type: str
    control_kind: str = "TEXT"
    semantic_group_id: Optional[str] = None
    option_discovery_state: str = "NOT_REQUIRED"
    verification_strategy: str = "VALUE"
    question_category: str = "FACTUAL"
    description: Optional[str] = None
    required: bool = False
    value: Any = None
    options: List[SemanticOption] = Field(default_factory=list)
    confidence: float = 0.0
    locator_strategy: str = ""
    frame: str = "main"
    max_length: Optional[int] = None
    role: Optional[str] = None
    aria: Dict[str, Any] = Field(default_factory=dict)
    selected_options: List[str] = Field(default_factory=list)
    validation_message: Optional[str] = None
    disabled: bool = False
    visible: bool = True
    section: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SemanticForm(BaseModel):
    name: str = "application"
    step: str = "1"
    url: str = ""
    fields: List[SemanticField] = Field(default_factory=list)
    actions: List[Dict[str, Any]] = Field(default_factory=list)
    validation_errors: List[Dict[str, Any]] = Field(default_factory=list)
    fingerprint: str = ""
