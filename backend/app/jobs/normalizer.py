import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from app.utils.hashing import canonicalize_url, job_fingerprint, normalized_text, stable_hash


def _clean(value: Any) -> str:
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _unique(items: Iterable[str], limit: int = 20) -> List[str]:
    result = []
    seen = set()
    for item in items:
        clean = _clean(item).strip(" •-–")
        key = normalized_text(clean)
        if clean and key not in seen:
            result.append(clean[:300])
            seen.add(key)
        if len(result) >= limit:
            break
    return result


def _infer_mode(text: str) -> Optional[str]:
    lower = normalized_text(text)
    if any(word in lower for word in ("remote", "zdaln")):
        return "remote"
    if any(word in lower for word in ("hybrid", "hybryd")):
        return "hybrid"
    if any(word in lower for word in ("on-site", "onsite", "stacjon")):
        return "on-site"
    return None


def _infer_level(title: str, description: str) -> Optional[str]:
    # Title evidence takes precedence over prose mentioning other roles.
    for text in (normalized_text(title), normalized_text(description[:1500])):
        for level, pattern in [('internship', r'\b(?:intern|internship|staż\w*|praktyk\w*)\b'),
                               ('entry', r'\b(?:junior|entry level|młodsz\w*|trainee)\b'),
                               ('senior', r'\b(?:senior|lead|principal|architect|manager)\b')]:
            if re.search(pattern, text): return level
    return None


@dataclass
class NormalizedJob:
    source: str
    source_job_id: str
    canonical_url: str
    title: str
    company: str
    location: str
    description_normalized: str
    requirements: List[str] = field(default_factory=list)
    nice_to_have: List[str] = field(default_factory=list)
    responsibilities: List[str] = field(default_factory=list)
    work_mode: Optional[str] = None
    employment_type: Optional[str] = None
    seniority: Optional[str] = None
    category: Optional[str] = None
    raw_metadata: Dict[str, Any] = field(default_factory=dict)

    def persistence_dict(self) -> Dict[str, Any]:
        compact = {
            "id": "p:" + self.source_job_id if self.source == "pracuj" else self.source + ":" + self.source_job_id,
            "t": self.title[:180],
            "c": self.company[:140],
            "loc": self.location[:140],
            "mode": self.work_mode,
            "lvl": self.seniority,
            "type": self.employment_type,
            "req": self.requirements[:12],
            "nice": self.nice_to_have[:8],
            "duties": self.responsibilities[:10],
        }
        compact = {key: value for key, value in compact.items() if value not in (None, "", [])}
        semantic_content = {
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "description": self.description_normalized,
            "requirements": self.requirements,
            "nice_to_have": self.nice_to_have,
            "responsibilities": self.responsibilities,
            "employment_type": self.employment_type,
            "work_mode": self.work_mode,
            "seniority": self.seniority,
            "salary": self.raw_metadata.get('salary'),
        }
        result = asdict(self)
        result["canonical_url"] = canonicalize_url(self.canonical_url)
        result["fingerprint"] = job_fingerprint(self.company, self.title, self.location)
        result["content_hash"] = stable_hash(semantic_content)
        result["compact_json"] = compact
        return result


def normalize_job(source: str, source_job_id: str, url: str, data: Dict[str, Any]) -> NormalizedJob:
    description = _clean(data.get("description") or data.get("description_normalized"))
    title = _clean(data.get("title"))
    requirements = _unique(data.get("requirements", []))
    responsibilities = _unique(data.get("responsibilities", []))
    if not requirements and description:
        requirements = _unique(
            sentence for sentence in re.split(r"[\n\r•]+", description)
            if any(word in normalized_text(sentence) for word in ("require", "wymaga", "knowledge", "znajomo", "experience", "doświadc"))
        )
    return NormalizedJob(
        source=source,
        source_job_id=str(source_job_id),
        canonical_url=url,
        title=title,
        company=_clean(data.get("company") or data.get("hiringOrganization")),
        location=_clean(data.get("location") or data.get("jobLocation")),
        description_normalized=description[:30000],
        requirements=requirements,
        nice_to_have=_unique(data.get("nice_to_have", [])),
        responsibilities=responsibilities,
        work_mode=data.get("work_mode") or _infer_mode(" ".join((title, description))),
        employment_type=_clean(data.get("employment_type")) or None,
        seniority=data.get("seniority") or _infer_level(title, description),
        category=_clean(data.get("category")) or None,
        raw_metadata=data.get("raw_metadata", {}),
    )
