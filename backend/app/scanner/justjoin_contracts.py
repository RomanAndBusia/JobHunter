"""Public source contracts; filter names are the site's actual choices."""
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class JustJoinFilters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    keyword: str = Field(default='', max_length=240)
    category: str = Field(default='security', pattern=r'^[a-z0-9-]*$')
    location: str = Field(default='', max_length=160)
    radius: Optional[int] = Field(default=None, ge=0, le=500)
    salary_only: bool = False
    min_salary: Optional[int] = Field(default=None, ge=0, le=1000000)
    currency: Literal['original', 'PLN', 'EUR', 'USD', 'GBP', 'CHF'] = 'original'
    published_days: Optional[Literal[1, 7, 14, 30]] = None
    work_modes: List[Literal['remote', 'hybrid', 'office']] = Field(default_factory=list)
    employment_types: List[Literal['b2b', 'permanent', 'internship', 'mandate_contract', 'contract']] = Field(default_factory=list)
    work_types: List[Literal['full_time', 'part_time', 'internship', 'freelance', 'b2b_contract']] = Field(default_factory=list)
    experience: List[Literal['intern', 'junior', 'mid', 'senior', 'manager', 'c_level']] = Field(default_factory=lambda: ['junior'])
    languages: List[Literal['pl', 'en', 'de', 'fr', 'es', 'uk', 'ru', 'it']] = Field(default_factory=list)
    sort: Literal['default', 'newest', 'salary'] = 'default'


class JustJoinSearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    filters: JustJoinFilters = Field(default_factory=JustJoinFilters)
    mode: Literal['manual', 'ai', 'recommendations'] = 'manual'
    target_count: int = Field(default=20, ge=1, le=100)
    candidate_id: Optional[int] = None


class JustJoinSessionOut(BaseModel):
    state: Literal['UNKNOWN', 'CHECKING', 'LOGGED_OUT', 'WAITING_FOR_LOGIN', 'LOGGED_IN', 'SESSION_EXPIRED', 'ERROR'] = 'UNKNOWN'
    message: str = 'Connect to Chrome to check your Just Join IT session.'
    page_url: Optional[str] = None
    signals: List[str] = Field(default_factory=list)


class JustJoinRunOut(BaseModel):
    id: int
    status: str
    seen: int
    errors: int
    details: Dict[str, Any]
    completed_at: Optional[datetime] = None


class PrepareJobsRequest(BaseModel):
    candidate_id: int
    selected_cv_document_id: int
    job_ids: List[int] = Field(min_length=1, max_length=50)
