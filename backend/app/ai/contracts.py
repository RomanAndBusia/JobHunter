from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobEvaluation(StrictModel):
    suitability_score: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    decision: Literal["APPLY", "SKIP", "NEEDS_REVIEW"]
    reasons: List[str]
    matched_skills: List[str]
    missing_skills: List[str]
    hard_blockers: List[str]
    language_fit: Literal["MATCH", "MISMATCH", "UNKNOWN"]
    experience_fit: Literal["MATCH", "MISMATCH", "UNKNOWN"]
    location_fit: Literal["MATCH", "MISMATCH", "UNKNOWN"]
    work_mode_fit: Literal["MATCH", "MISMATCH", "UNKNOWN"]
    contract_fit: Literal["MATCH", "MISMATCH", "UNKNOWN"]
    seniority_fit: Literal["MATCH", "MISMATCH", "UNKNOWN"]
    recommended_cover_letter: bool
    unknowns: List[str] = Field(default_factory=list)
    nice_to_have_match: List[str] = Field(default_factory=list)
    missing_nice_to_have: List[str] = Field(default_factory=list)

    @field_validator('decision', mode='before')
    @classmethod
    def normalize_decision(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


MATCH_INSTRUCTIONS = (
    "Use fixed absolute rubric v1, independently for each job. Never rank, percentile, min-max normalize, "
    "or change a score because of other jobs. Responsibilities/domain/transferable skills 25 points; "
    "mandatory technologies and skills 30; evidenced experience and seniority 20; language/education/"
    "certification/eligibility 15; explicit location/work mode/contract preferences 5; preferred skills "
    "and realistic learnability 5. Distinguish MUST_HAVE, NICE_TO_HAVE, PREFERENCE and UNKNOWN from "
    "the actual vacancy text. Unknown evidence stays unknown and is listed in unknowns. Mandatory "
    "incompatibility weighs much more than a missing nice-to-have. Calibration: 90-100 excellent, "
    "80-89 strong, 70-79 good realistic, 55-69 plausible with gaps, 40-54 weak, 20-39 poor, 0-19 "
    "fundamentally unsuitable. All jobs may score very high or very low. "
    "Evaluate each stable job ID exactly once against the candidate CV/profile and explicit preferences. "
    "Return a detailed evaluation and consistent ok/score/reason/blockers summary. APPLY is a reasonable "
    "opportunity with confidence >=0.75 and no hard blockers; missing minor technology is not a blocker. "
    "SKIP requires an evidenced incompatibility (profession, mandatory experience/seniority, required "
    "language, explicit location/contract/work-mode preferences or legal qualification). Unknown personal "
    "eligibility must be UNKNOWN/NEEDS_REVIEW, never invented or treated as a proven incompatibility. "
    "Do not infer years of commercial employment from projects. Evaluate seniority for this candidate, "
    "not a universal entry-level candidate. Reasons and skills must be short. Optional unknown preferences "
    "alone need not stop a plausible APPLY. recommended_cover_letter is true only when it adds value. "
    "suitability_score is 0..100; summary score equals suitability_score/100 and ok is true only for APPLY."
)


class CoverLetterClaim(StrictModel):
    text: str = Field(max_length=500)
    source_path: str


class CoverLetterPlan(StrictModel):
    mode: Literal["none", "textarea", "txt_attachment"]
    field_id: Optional[str]
    opening: str = Field(max_length=400)
    claims: List[CoverLetterClaim]
    closing: str = Field(max_length=400)
    reason: str = Field(max_length=500)


def cover_letter_contract(context):
    """Constrain native output to inspected IDs and exact persisted fact keys."""
    keys = tuple(context.get('facts', {}).keys())
    fields = tuple(item['id'] for item in context.get('fields', []))
    if not keys or not fields:
        return CoverLetterPlan
    claim = create_model('BoundCoverLetterClaim', __base__=CoverLetterClaim,
                         source_path=(Literal[keys], ...))
    bound_fields = {'claims': (List[claim], ...), 'field_id': (Optional[Literal[fields]], ...)}
    if context.get('mode') == 'always':
        modes = tuple(sorted({item['mode'] for item in context['fields']}))
        bound_fields['mode'] = (Literal[modes], ...)
        bound_fields['field_id'] = (Literal[fields], ...)
    return create_model('BoundCoverLetterPlan', __base__=CoverLetterPlan, **bound_fields)


class IdentityProfile(StrictModel):
    full_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    date_of_birth: Optional[str] = None


class ContactProfile(StrictModel):
    email: Optional[str] = None
    phone: Optional[str] = None


class LocationProfile(StrictModel):
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None


class ExperienceItem(StrictModel):
    company: Optional[str] = None
    title: Optional[str] = None
    location: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    description: Optional[str] = None
    responsibilities: List[str] = Field(default_factory=list)
    technologies: List[str] = Field(default_factory=list)


class EducationItem(StrictModel):
    institution: Optional[str] = None
    degree: Optional[str] = None
    specialization: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    completed: Optional[bool] = None
    description: Optional[str] = None


class ProjectItem(StrictModel):
    name: Optional[str] = None
    description: Optional[str] = None
    technologies: List[str] = Field(default_factory=list)
    role: Optional[str] = None
    achievements: List[str] = Field(default_factory=list)
    url: Optional[str] = None


class LanguageItem(StrictModel):
    language: str = ""
    level: Optional[str] = None
    proficiency: Optional[str] = None


class CertificationItem(StrictModel):
    name: str = ""
    issuer: Optional[str] = None
    date: Optional[str] = None
    status: Optional[str] = None


class SkillGroups(StrictModel):
    technical: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)
    technologies: List[str] = Field(default_factory=list)
    soft: List[str] = Field(default_factory=list)


class LinksProfile(StrictModel):
    linkedin: Optional[str] = None
    github: Optional[str] = None
    portfolio: Optional[str] = None
    other: List[str] = Field(default_factory=list)


class PreferencesProfile(StrictModel):
    availability: Optional[str] = None
    salary_expectations: Optional[str] = None
    preferred_locations: List[str] = Field(default_factory=list)
    remote_preference: Optional[str] = None
    relocation: Optional[str] = None
    work_authorization: Optional[str] = None
    notice_period: Optional[str] = None


class CandidateProfilePayload(StrictModel):
    identity: IdentityProfile = Field(default_factory=IdentityProfile)
    contact: ContactProfile = Field(default_factory=ContactProfile)
    location: LocationProfile = Field(default_factory=LocationProfile)
    professional_summary: Optional[str] = None
    employment: List[ExperienceItem] = Field(default_factory=list)
    education: List[EducationItem] = Field(default_factory=list)
    projects: List[ProjectItem] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    skill_groups: SkillGroups = Field(default_factory=SkillGroups)
    languages: List[LanguageItem] = Field(default_factory=list)
    certifications: List[CertificationItem] = Field(default_factory=list)
    links: LinksProfile = Field(default_factory=LinksProfile)
    preferences: PreferencesProfile = Field(default_factory=PreferencesProfile)


class ExtractedFact(StrictModel):
    path: str = Field(min_length=1, max_length=300)
    source: Literal["CV_EXPLICIT", "CV_DERIVED"]
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(max_length=500)


class CandidateProfileExtraction(StrictModel):
    profile: CandidateProfilePayload = Field(default_factory=CandidateProfilePayload)
    facts: List[ExtractedFact] = Field(default_factory=list)


class PlannedFormField(StrictModel):
    field_id: str
    meaning: str = Field(max_length=300)
    action: Literal["fill", "select_option", "check", "uncheck", "upload", "skip", "needs_user"]
    semantic_answer: Optional[str] = None
    selected_option_id: Optional[str] = None
    selected_option_ids: List[str] = Field(default_factory=list)
    source_paths: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    execution_confidence: Literal["EXACT", "HIGH_CONFIDENCE", "AMBIGUOUS", "UNKNOWN"] = "UNKNOWN"
    needs_user: bool
    user_question: Optional[str] = None
    requested_fact_path: Optional[str] = None
    reason: str = Field(max_length=600)
    question_category: Literal[
        "FACTUAL", "DERIVABLE", "PREFERENCE", "SELF_ASSESSMENT",
        "MULTI_SELECT_PREFERENCE", "LEGAL_DECLARATION", "CONSENT",
        "REQUIRED_RECRUITMENT_CONSENT", "OPTIONAL_FUTURE_RECRUITMENT_CONSENT",
        "PRIVACY_ACKNOWLEDGEMENT", "JOB_SPECIFIC_TEXT",
    ] = "FACTUAL"


class RepeatGroupPlan(StrictModel):
    group_id: str
    source_path: str
    add_action_id: str
    current_rows: int = Field(ge=0, le=30)
    desired_rows: int = Field(ge=1, le=30)


class FormPlan(StrictModel):
    fields: List[PlannedFormField]
    repeat_groups: List[RepeatGroupPlan] = Field(default_factory=list)


class PagePlan(StrictModel):
    page_kind: Literal["VACANCY", "APPLICATION_ENTRY", "FORM", "REVIEW", "CHALLENGE", "UNKNOWN"]
    action: Literal["NAVIGATE_APPLICATION_ENTRY", "INSPECT_FORM", "WAIT_FOR_USER"]
    element_id: Optional[str] = None
    confidence: Literal["HIGH", "LOW"]
    reason: str = Field(max_length=600)
