import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

from app.models import CandidateFact


SOURCE_PRIORITY = {
    "USER_CONFIRMED": 6,
    "USER_ENTERED": 5,
    "CV_EXPLICIT": 4,
    "CV_DERIVED": 3,
    "AI_DERIVED": 2,
    "DERIVED": 1,
}

PATH_ALIASES = {
    "salary_expectation": "preferences.salary_expectations",
    "salary_expectations": "preferences.salary_expectations",
    "availability": "preferences.availability",
    "notice_period": "preferences.notice_period",
    "relocation": "preferences.relocation",
    "work_authorization": "preferences.work_authorization",
    "city": "location.city",
    "country": "location.country",
}


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def value_at(data: Any, path: str) -> Tuple[bool, Any]:
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return current not in (None, "", [], {}), current


@dataclass(frozen=True)
class FactResolution:
    requested_path: str
    canonical_path: str
    exists: bool
    value: Any = None
    source: Optional[str] = None
    confidence: Optional[float] = None


class CandidateFactResolver:
    """Resolve canonical and semantic fact paths against one persisted profile."""

    def __init__(self, profile: Dict[str, Any], facts: Iterable[CandidateFact]):
        self.profile = profile or {}
        now = datetime.utcnow()
        self.facts = {fact.path: fact for fact in facts
                      if (fact.expires_at is None or fact.expires_at > now)
                      and (fact.valid_from is None or fact.valid_from <= now)}

    def canonical_path(self, path: str) -> str:
        candidate = (path or "").removeprefix("candidate_profile.").removeprefix("candidate.")
        candidate = PATH_ALIASES.get(candidate, candidate)
        match = re.fullmatch(r"languages(?:\.|\[)([^.\]]+)(?:\])?\.(level|proficiency)", candidate, re.I)
        if match and not match.group(1).isdigit():
            language_name = _fold(match.group(1))
            for index, language in enumerate(self.profile.get("languages") or []):
                if isinstance(language, dict) and _fold(str(language.get("language") or "")) == language_name:
                    return "languages.%d.%s" % (index, match.group(2).casefold())
        return candidate

    def resolve(self, path: str) -> FactResolution:
        canonical = self.canonical_path(path)
        exists, value = value_at(self.profile, canonical)
        related = [
            fact for fact_path, fact in self.facts.items()
            if fact_path == canonical
            or canonical.startswith(fact_path + ".")
            or fact_path.startswith(canonical + ".")
        ]
        best = max(
            related,
            key=lambda fact: (SOURCE_PRIORITY.get(fact.source, 0), fact.confidence or 0),
            default=None,
        )
        return FactResolution(
            requested_path=path,
            canonical_path=canonical,
            exists=bool(exists and best is not None),
            value=value if exists else None,
            source=best.source if best is not None else None,
            confidence=best.confidence if best is not None else None,
        )
