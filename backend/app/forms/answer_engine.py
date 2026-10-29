import logging
import re
import unicodedata
from datetime import datetime
from types import SimpleNamespace
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.logging import event
from app.models import Application, ApplicationQuestionRequest, CandidateFact, CandidateProfile, Job, QuestionMemory
from app.candidate.fact_resolver import CandidateFactResolver, SOURCE_PRIORITY
from app.candidate.memory import canonical_concept
from app.config import settings
from app.schemas.api import SemanticField
from app.utils.hashing import normalized_text, stable_hash


logger = logging.getLogger(__name__)
ProviderFactory = Callable[..., AIProvider]
_FACT_ROOTS = {
    "identity", "contact", "location", "professional_summary", "employment", "education",
    "projects", "skills", "skill_groups", "languages", "certifications", "links",
    "preferences", "work_authorization", "career_goal",
}


@dataclass
class ProposedAnswer:
    field_id: str
    question: str
    value: Any
    source: str
    confidence: float
    needs_user: bool = False
    meaning: str = ""
    source_paths: Optional[List[str]] = None
    reason: str = ""
    requested_fact_path: Optional[str] = None
    action: Optional[str] = None
    execution_confidence: str = "UNKNOWN"
    question_category: str = "FACTUAL"
    suggested_value: Any = None
    suggestion_reason: str = ""

    def __post_init__(self) -> None:
        if self.source_paths is None:
            self.source_paths = []


def question_concept(question: str) -> str:
    return canonical_concept(question) or "question:" + stable_hash(normalized_text(question))[:24]


def _value_at(data: Any, path: str) -> Tuple[bool, Any]:
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return current not in (None, "", [], {}), current


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.translate(str.maketrans({"ł": "l", "Ł": "L"}))
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


class AnswerEngine:
    """AI-first semantic planner with deterministic grounding and option validation."""

    def __init__(self, db: Session, provider_factory: ProviderFactory = get_ai_provider, application_id=None):
        self.db = db
        self.ai = provider_factory(db=db)
        self.application_id = application_id
        self.repeat_groups = []

    def _facts(self, candidate_id: int) -> Dict[str, CandidateFact]:
        rows = self.db.execute(select(CandidateFact).where(CandidateFact.profile_id == candidate_id)).scalars()
        now = datetime.utcnow()
        return {row.path: row for row in rows
                if (row.expires_at is None or row.expires_at > now)
                and (row.valid_from is None or row.valid_from <= now)}

    def _memory(self, candidate_id: int) -> Dict[str, QuestionMemory]:
        rows = self.db.execute(select(QuestionMemory).where(
            QuestionMemory.candidate_id == candidate_id,
            QuestionMemory.confirmed_at.is_not(None),
        )).scalars()
        result = {row.concept: row for row in rows
                  if row.scope in {'GLOBAL', 'CANDIDATE_GLOBAL', 'JOB_PREFERENCE_DEFAULT'}
                  and (row.expires_at is None or row.expires_at > datetime.utcnow())}
        if self.application_id is not None:
            local = self.db.execute(select(ApplicationQuestionRequest).where(
                ApplicationQuestionRequest.application_id == self.application_id,
                ApplicationQuestionRequest.status == 'ANSWERED',
            )).scalars()
            for row in local:
                result['application:%s' % row.id] = SimpleNamespace(
                    value=row.raw_value, presentation_value=row.presentation_value,
                    source='SYNTHETIC_TEST' if (row.field_context or {}).get('synthetic_test') else 'USER_CONFIRMED',
                    question=row.question, fact_path=row.fact_path,
                )
        return result

    @staticmethod
    def _best_option(field: SemanticField, semantic_answer: str) -> Optional[str]:
        wanted = _fold(semantic_answer)
        if not wanted:
            return None
        exact = [item for item in field.options if not item.disabled and _fold(item.label) == wanted]
        if len(exact) == 1:
            return exact[0].id
        contained = [item for item in field.options if not item.disabled and (wanted in _fold(item.label) or _fold(item.label) in wanted)]
        if len(contained) == 1:
            return contained[0].id
        wanted_tokens = set(wanted.split()) - {"poland", "polska", "country", "city"}
        scored = []
        for item in field.options:
            if item.disabled:
                continue
            score = len(wanted_tokens & set(_fold(item.label).split()))
            if score:
                scored.append((score, item.id))
        scored.sort(reverse=True)
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1]
        return None

    @staticmethod
    def _boolean_evidence_is_sufficient(field: SemanticField, answer: str, paths: List[str]) -> bool:
        if _fold(answer) not in {"yes", "true", "tak"}:
            return True
        question = _fold(" ".join((field.question, str(field.metadata.get("surrounding_text") or ""))))
        canonical_paths = [path for path in paths if not path.startswith("memory:")]
        if any(path.startswith("memory:") for path in paths):
            return True
        if any(term in question for term in ("eligible to work", "work authorization", "right to work")):
            return any("work_authorization" in path for path in canonical_paths)
        duration_markers = ("commercial experience", "professional experience", "year", "years", "month", "months")
        if any(term in question for term in duration_markers):
            return any(path.startswith("employment.") for path in canonical_paths)
        if "experience" in question:
            return any(path.startswith(("employment.", "projects.")) for path in canonical_paths)
        return True

    @staticmethod
    def _availability_value(field: SemanticField, value: Any) -> Any:
        question = _fold(field.question)
        if "availability" not in question and "notice" not in question:
            return value
        folded = _fold(str(value))
        if "immediate" in folded:
            return "Immediately"
        number_match = re.search(r"\b(7|14|30|60|90)\b", folded)
        if number_match and "day" in folded:
            return {"7": "1 week", "14": "2 weeks", "30": "1 month", "60": "2 months", "90": "More than 3 months"}[number_match.group(1)]
        return value

    async def answer(self, candidate: CandidateProfile, job: Job, field: SemanticField) -> ProposedAnswer:
        return (await self.plan(candidate, job, [field]))[0]

    async def plan(
        self,
        candidate: CandidateProfile,
        job: Job,
        fields: List[SemanticField],
        failure_context: Optional[Dict[str, Any]] = None,
        form_actions: Optional[List[Dict[str, Any]]] = None,
    ) -> List[ProposedAnswer]:
        facts = self._facts(candidate.id)
        resolver = CandidateFactResolver(candidate.data or {}, facts.values())
        memories = self._memory(candidate.id)
        context = {
            "candidate": candidate.data or {},
            "candidate_fact_provenance": {
                path: {"source": fact.source, "confidence": fact.confidence, "confirmed": fact.user_confirmed}
                for path, fact in facts.items()
            },
            "confirmed_answers": {
                concept: {"value": memory.value, "presentation_value": memory.presentation_value, "source": memory.source,
                          "question": getattr(memory, 'question', None), "fact_path": getattr(memory, 'fact_path', None)}
                for concept, memory in memories.items()
            },
            "job": {
                "title": job.title, "company": job.company, "location": job.location,
                "description": job.description_normalized, "requirements": job.requirements,
                "responsibilities": job.responsibilities, "compact": job.compact_json,
            },
            "form": {"fields": [field.model_dump() for field in fields], "actions": form_actions or []},
            "failure_context": failure_context,
        }
        application = self.db.get(Application, self.application_id) if self.application_id else None
        if application and application.selected_cv_document:
            context['selected_cv'] = {'id': application.selected_cv_document_id,
                                      'text': application.selected_cv_document.extracted_text}
        event(logger, "ai.form_plan.started", candidate_id=candidate.id, job_id=job.id, field_count=len(fields), provider=self.ai.name, model=getattr(self.ai, "model", ""))
        plan = await self.ai.plan_form(context)
        self.repeat_groups = []
        for group in plan.repeat_groups:
            evidence = resolver.resolve(group.source_path)
            action = next((a for a in (form_actions or []) if a['id'] == group.add_action_id), None)
            if (evidence.exists and isinstance(evidence.value, list)
                    and len(evidence.value) == group.desired_rows
                    and action and action.get('type') == 'add_row' and not action.get('submits')):
                self.repeat_groups.append(group)
        event(logger, "ai.form_plan.completed", candidate_id=candidate.id, job_id=job.id, field_count=len(plan.fields), provider=self.ai.name, model=getattr(self.ai, "model", ""), request_id=getattr(self.ai, "last_request", {}).get("request_id"))
        expected_ids = [field.id for field in fields]
        returned_ids = [item.field_id for item in plan.fields]
        if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != set(expected_ids):
            raise ValueError("AI form plan did not map exactly once to every requested field")
        planned = {item.field_id: item for item in plan.fields}
        answers: List[ProposedAnswer] = []
        for field in fields:
            item = planned[field.id]
            # A confirmed answer for this exact application field is
            # authoritative even if the planner forgets to reference it.
            from app.forms.identity import field_key
            if application and not field.metadata.get('compound_id'):
                local_id = 's%s:%s' % (application.current_step or '1', field_key(field))
                local = self.db.execute(select(ApplicationQuestionRequest).where(
                    ApplicationQuestionRequest.application_id == application.id,
                    ApplicationQuestionRequest.field_id == local_id,
                    ApplicationQuestionRequest.status == 'ANSWERED',
                )).scalar_one_or_none()
                if local is not None:
                    actual = local.raw_value if field.options else local.presentation_value if local.presentation_value is not None else local.raw_value
                    multi = field.control_kind in {'CHECKBOX_GROUP', 'MULTI_SELECT'}
                    values = actual if isinstance(actual, list) else [actual]
                    ids = [next((o.id for o in field.options if o.label == str(v) and not o.disabled), None) for v in values]
                    if not field.options or (all(ids) and (multi or len(ids) == 1)):
                        item.source_paths = ['memory:application:%s' % local.id]
                        item.semantic_answer = str(actual)
                        item.needs_user = False
                        item.action = 'select_option' if field.options else 'check' if actual is True else 'uncheck' if actual is False else 'fill'
                        item.selected_option_id = ids[0] if field.options and not multi else None
                        item.selected_option_ids = ids if field.options and multi else []
                        item.execution_confidence = 'EXACT'
            question_category = (
                field.question_category
                if field.question_category and field.question_category != "FACTUAL"
                else item.question_category
            )
            item.source_paths = [
                resolver.canonical_path(path)
                for path in item.source_paths
            ]
            value: Any = item.semantic_answer
            source_types = []
            grounded = True
            policy_grounded = False
            consent_text = _fold(" ".join((field.question, str(field.metadata.get("surrounding_text") or ""))))
            consent_categories = {
                "CONSENT", "REQUIRED_RECRUITMENT_CONSENT",
                "OPTIONAL_FUTURE_RECRUITMENT_CONSENT", "PRIVACY_ACKNOWLEDGEMENT",
            }
            is_consent = question_category in consent_categories or field.control_kind == "CONSENT" or field.type in {"checkbox", "toggle"} and any(
                marker in consent_text for marker in ("consent", "personal data", "zgod")
            )
            optional_future_consent = question_category == "OPTIONAL_FUTURE_RECRUITMENT_CONSENT" or is_consent and any(
                marker in consent_text for marker in ("future", "marketing", "przysz")
            )
            required_process_consent = field.required and question_category in {
                "REQUIRED_RECRUITMENT_CONSENT", "PRIVACY_ACKNOWLEDGEMENT", "CONSENT",
            }
            if is_consent:
                event(
                    logger, "consent.classified", job_id=job.id, field_id=field.id,
                    category=question_category, required=field.required,
                    auto_accept_policy=settings.auto_accept_required_recruitment_consents,
                )
            if optional_future_consent and not any(path.startswith("memory:") for path in item.source_paths):
                item.action = "skip"
                item.needs_user = False
                value = None
                item.reason = "Optional future-recruitment or marketing consent was not confirmed."
            elif (required_process_consent and settings.auto_accept_required_recruitment_consents
                  and not any(path.startswith("memory:") for path in item.source_paths)):
                item.action = "check"
                item.needs_user = False
                item.execution_confidence = "EXACT"
                value = True
                policy_grounded = True
                source_types.append("USER_POLICY")
                item.reason = "Accepted by the configured required-recruitment-consent policy."
            elif required_process_consent and not any(path.startswith("memory:") for path in item.source_paths):
                item.action = "needs_user"
                item.needs_user = True
                value = None
                item.reason = "Required recruitment consent needs explicit user confirmation."
            elif is_consent and not field.required and not any(path.startswith("memory:") for path in item.source_paths):
                item.action = "skip"
                item.needs_user = False
                value = None
                item.reason = "Optional consent was not explicitly confirmed."
            for path in item.source_paths:
                if path.startswith("memory:"):
                    memory = memories.get(path.split(":", 1)[1])
                    if memory is None:
                        grounded = False
                        break
                    source_types.append(memory.source)
                    continue
                resolution = resolver.resolve(path)
                event(logger, 'candidate_fact.resolved' if resolution.exists else 'candidate_fact.missing',
                      candidate_id=candidate.id, field_id=field.id, source_path=path)
                if not resolution.exists or resolution.source is None:
                    grounded = False
                    break
                source_types.append(resolution.source)
            requested_path = resolver.canonical_path(item.requested_fact_path or "") or None
            requested_resolution = resolver.resolve(requested_path) if requested_path else None
            if item.needs_user and requested_resolution and requested_resolution.exists:
                item.needs_user = False
                item.action = "select_option" if field.options else "fill"
                value = self._availability_value(field, requested_resolution.value)
                item.semantic_answer = str(value)
                item.source_paths = [requested_resolution.canonical_path]
                source_types = [requested_resolution.source or ""]
                item.execution_confidence = "EXACT"
                item.reason = "Resolved from a persisted canonical candidate fact."
                if field.options:
                    item.selected_option_id = self._best_option(field, item.semantic_answer)
            if item.action not in {"skip", "needs_user"} and not policy_grounded and (not item.source_paths or not grounded):
                item.needs_user = True
                item.action = "needs_user"
                value = None
                item.reason = "The proposed answer was not grounded in a persisted candidate fact."
            if (
                item.action not in {"skip", "needs_user"}
                and not self._boolean_evidence_is_sufficient(field, item.semantic_answer or "", item.source_paths)
            ):
                item.needs_user = True
                item.action = "needs_user"
                value = None
                item.reason = "The available evidence does not support this affirmative claim."
            if item.action == "select_option" and not item.needs_user:
                if field.control_kind in {'MULTI_SELECT', 'CHECKBOX_GROUP'}:
                    selected_ids = item.selected_option_ids or ([item.selected_option_id] if item.selected_option_id else [])
                    valid_ids = {option.id for option in field.options if not option.disabled}
                    if not selected_ids or not set(selected_ids).issubset(valid_ids):
                        item.needs_user = True
                        value = None
                        item.reason = 'Multi-select requires actual enabled option IDs for every selected value.'
                    else:
                        value = list(dict.fromkeys(selected_ids))
                    selected = None
                else:
                    selected = item.selected_option_id
                valid_ids = {option.id for option in field.options if not option.disabled}
                if selected not in valid_ids and field.control_kind not in {'MULTI_SELECT', 'CHECKBOX_GROUP'}:
                    selected = self._best_option(field, item.semantic_answer or "")
                if field.options and selected not in valid_ids and field.control_kind not in {'MULTI_SELECT', 'CHECKBOX_GROUP'}:
                    item.needs_user = True
                    item.action = "needs_user"
                    value = None
                    item.reason = "No unique actual website option matched the semantic answer."
                elif selected:
                    value = selected
            if requested_path and requested_path.split(".", 1)[0] not in _FACT_ROOTS:
                requested_path = None
            # Knowing a language name, or a narrative proficiency, cannot
            # authorize an invented CEFR level. Require the exact level in a
            # persisted value or confirmed answer before mapping that option.
            if not item.needs_user and item.action != 'skip':
                selected_label = next((o.label for o in field.options if o.id == value), '') if field.options else str(value or '')
                cefr = re.search(r'\b[ABC][12]\b', selected_label, re.I)
                language_control = (field.metadata.get('compound_role') == 'level'
                                    or re.search(r'language|german|english|język|niemieck|angielsk', field.question, re.I))
                if cefr and language_control:
                    evidence = []
                    for path in item.source_paths:
                        if path.startswith('memory:'):
                            memory = memories.get(path.split(':', 1)[1])
                            if memory is not None:
                                evidence.append(str(memory.value))
                        elif path.startswith('languages'):
                            resolution = resolver.resolve(path)
                            if resolution.exists and resolution.source in {'CV_EXPLICIT', 'USER_ENTERED', 'USER_CONFIRMED'}:
                                def levels(value):
                                    if isinstance(value, dict):
                                        return [str(v) for k, v in value.items() if k in {'level', 'proficiency'} and v is not None]
                                    if isinstance(value, list):
                                        return [level for entry in value for level in levels(entry)]
                                    return [str(value)] if re.search(r'\.(level|proficiency)$', path) else []
                                evidence.extend(levels(resolution.value))
                    if not any(re.search(r'\b' + re.escape(cefr.group()) + r'\b', v, re.I) for v in evidence):
                        item.needs_user = True
                        item.action = 'needs_user'
                        value = None
                        item.reason = 'No persisted evidence supports this exact CEFR level.'
            if item.execution_confidence in {"AMBIGUOUS", "UNKNOWN"} and item.action != "skip":
                item.needs_user = True
                item.action = "needs_user"
                value = None
                item.reason = "The answer could not be mapped with sufficient execution confidence."
            confirmed_memory = any(
                path.startswith("memory:") and path.split(":", 1)[1] in memories
                for path in item.source_paths
            )
            suggested_value = None
            suggestion_reason = ""
            confirmed_direct = bool(source_types) and all(s in {'USER_CONFIRMED', 'USER_ENTERED'} for s in source_types)
            if question_category == "LEGAL_DECLARATION" and not (confirmed_memory or confirmed_direct):
                item.needs_user = True
                item.action = "needs_user"
                value = None
                item.reason = "This legal declaration requires an explicit user-confirmed answer."
            elif question_category == "SELF_ASSESSMENT" and not confirmed_memory:
                valid_ids = {option.id for option in field.options}
                candidate_suggestion = item.selected_option_id if item.selected_option_id in valid_ids else None
                if candidate_suggestion is None and item.semantic_answer:
                    candidate_suggestion = self._best_option(field, item.semantic_answer) if field.options else item.semantic_answer
                suggested_value = next(
                    (option.label for option in field.options if option.id == candidate_suggestion),
                    candidate_suggestion,
                )
                suggestion_reason = item.reason or item.meaning or "Profile evidence supports this as a suggestion, not an objective fact."
                item.needs_user = True
                item.action = "needs_user"
                value = None
                item.reason = "Self-assessment requires user confirmation before it can be used."
            if item.needs_user:
                event(logger, 'form.field.needs_user', application_id=self.application_id,
                      field_id=field.id, fact_path=requested_path)
                event(logger, "ai.user_fact_required", candidate_id=candidate.id, job_id=job.id, field_id=field.id, requested_fact_path=requested_path)
            elif item.action != 'skip':
                event(logger, 'form.field.resolved_from_memory' if any(p.startswith('memory:') for p in item.source_paths)
                      else 'form.field.resolved_from_profile', application_id=self.application_id,
                      field_id=field.id)
            provenance = max(
                source_types or [""],
                key=lambda source: SOURCE_PRIORITY.get(source, 0),
            )
            source = provenance or ("NEEDS_USER" if item.needs_user else "AI_PLANNED")
            if any(path.startswith('memory:') and getattr(memories.get(path.split(':',1)[1]), 'source', None) == 'SYNTHETIC_TEST' for path in item.source_paths):
                source = 'SYNTHETIC_TEST'
            if item.action == "select_option" and source:
                source = (source + "_MAPPED")[:40]
            answers.append(ProposedAnswer(
                field_id=field.id, question=item.user_question or field.question, value=value,
                source=source, confidence=item.confidence,
                needs_user=item.needs_user or item.action == "needs_user",
                meaning=item.meaning, source_paths=item.source_paths, reason=item.reason,
                requested_fact_path=requested_path, action=item.action,
                execution_confidence=item.execution_confidence,
                question_category=question_category,
                suggested_value=suggested_value,
                suggestion_reason=suggestion_reason,
            ))
            event(
                logger, "planner.field_planned", job_id=job.id, field_id=field.id,
                control_kind=field.control_kind, question_category=question_category,
                action=item.action, needs_user=item.needs_user,
            )
            event(logger, "ai.field_mapping", candidate_id=candidate.id, job_id=job.id, field_id=field.id, meaning=item.meaning, action=item.action, source_paths=item.source_paths, source=source, confidence=item.confidence, needs_user=item.needs_user)
        return answers
