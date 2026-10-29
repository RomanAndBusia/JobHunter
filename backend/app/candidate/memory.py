from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FactAlias, QuestionMemory
from app.utils.hashing import normalized_text, stable_hash


CONCEPT_ALIASES = {
    "age_at_least_18": (
        "are you at least 18", "are you over 18", "are you eighteen", "do you have 18 years",
        "czy masz ukończone 18 lat", "czy jesteś pełnoletni", "pełnoletność",
    ),
    "work_authorization_poland": (
        "authorized to work in poland", "right to work in poland", "prawo do pracy w polsce", "legalnie pracować w polsce",
    ),
    "student_status": ("are you currently a student", "czy jesteś studentem", "student status"),
    "availability_date": ("when can you start", "available from", "availability date", "od kiedy możesz zacząć"),
    "english_level": ("english level", "english proficiency", "poziom angielskiego", "język angielski"),
    "salary_expectation": ("salary expectation", "expected salary", "expected hourly rate", "oczekiwane wynagrodzenie", "oczekiwana stawka"),
}

TIME_SENSITIVE = {"student_status", "availability_date", "salary_expectation", "notice_period"}


def canonical_concept(question: str) -> Optional[str]:
    q = normalized_text(question)
    age_match = __import__("re").search(r"(?:at least|over|older than|ukończone)\s*(\d{1,3})", q)
    if age_match:
        return "age_at_least_%s" % age_match.group(1)
    for concept, aliases in CONCEPT_ALIASES.items():
        if any(normalized_text(alias) in q or q in normalized_text(alias) for alias in aliases):
            return concept
    return None


def memory_scope(concept: Optional[str], question: str) -> str:
    if concept in TIME_SENSITIVE:
        return "GLOBAL"
    q = normalized_text(question)
    if any(token in q for token in ("why do you want", "why this company", "dlaczego chcesz")):
        return "COMPANY"
    return "GLOBAL" if concept else "APPLICATION_ONLY"


class SemanticMemory:
    def __init__(self, db: Session):
        self.db = db

    def resolve(self, candidate_id: int, question: str) -> Tuple[Optional[str], Optional[QuestionMemory]]:
        normalized = normalized_text(question)
        concept = canonical_concept(question)
        alias = self.db.execute(select(FactAlias).where(
            FactAlias.candidate_id == candidate_id,
            FactAlias.normalized_alias == normalized,
        )).scalar_one_or_none()
        concept = alias.concept if alias else concept
        if not concept:
            return None, None
        memory = self.db.execute(select(QuestionMemory).where(
            QuestionMemory.candidate_id == candidate_id,
            QuestionMemory.concept == concept,
        )).scalar_one_or_none()
        if memory is None and concept.startswith("age_at_least_"):
            requested_age = int(concept.rsplit("_", 1)[1])
            candidates = self.db.execute(select(QuestionMemory).where(QuestionMemory.candidate_id == candidate_id)).scalars()
            memory = next((item for item in candidates if item.concept.startswith("age_at_least_") and int(item.concept.rsplit("_", 1)[1]) >= requested_age and bool(item.value)), None)
        if memory and memory.expires_at and memory.expires_at <= datetime.utcnow():
            memory = None
        return concept, memory

    def remember(self, candidate_id: int, question: str, value: Any, presentation: Any = None, scope: Optional[str] = None, confirm_conflict: bool = False) -> QuestionMemory:
        concept = canonical_concept(question) or "question:" + stable_hash(normalized_text(question))[:24]
        memory = self.db.execute(select(QuestionMemory).where(
            QuestionMemory.candidate_id == candidate_id,
            QuestionMemory.concept == concept,
        )).scalar_one_or_none()
        now = datetime.utcnow()
        if memory and memory.value != value and not confirm_conflict:
            raise ValueError("The answer conflicts with remembered information; confirm replacement explicitly")
        history = list(memory.history or []) if memory else []
        if memory:
            history.append({"value": memory.value, "presentation_value": memory.presentation_value, "replaced_at": now.isoformat()})
        else:
            memory = QuestionMemory(candidate_id=candidate_id, concept=concept, question_fingerprint=stable_hash(normalized_text(question)), value=value, source="USER_CONFIRMED", confidence=1.0)
            self.db.add(memory)
        memory.value = value
        memory.raw_user_value = value
        memory.presentation_value = presentation if presentation is not None else value
        memory.source = "USER_CONFIRMED"
        memory.scope = scope or memory_scope(concept, question)
        memory.aliases = list(dict.fromkeys(list(memory.aliases or []) + [question]))
        memory.history = history
        memory.confirmed_at = now
        memory.last_confirmed_at = now
        alias = self.db.execute(select(FactAlias).where(
            FactAlias.candidate_id == candidate_id, FactAlias.concept == concept,
            FactAlias.normalized_alias == normalized_text(question),
        )).scalar_one_or_none()
        if alias is None:
            self.db.add(FactAlias(candidate_id=candidate_id, concept=concept, alias=question, normalized_alias=normalized_text(question)))
        return memory


def entails(concept: str, value: Any, requested_concept: str) -> bool:
    if concept == requested_concept:
        return True
    if concept.startswith("age_at_least_") and requested_concept.startswith("age_at_least_") and bool(value):
        known = int(concept.rsplit("_", 1)[1])
        requested = int(requested_concept.rsplit("_", 1)[1])
        return known >= requested
    return False


def polish_answer(question: str, raw_value: Any) -> Any:
    if not isinstance(raw_value, str):
        return raw_value
    compact = " ".join(raw_value.split()).strip()
    q = normalized_text(question)
    if any(token in q for token in ("salary", "wynagrod", "compensation")) and any(token in normalized_text(compact) for token in ("experience", "doświadczen", "dont care", "don't care")):
        return "My priority is gaining relevant professional experience, so I am flexible regarding compensation within the employer's entry-level range."
    if not compact:
        return compact
    return compact[0].upper() + compact[1:]
