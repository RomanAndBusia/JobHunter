import re
from dataclasses import dataclass
from typing import Optional

from app.models import Job
from app.utils.hashing import normalized_text


@dataclass
class FilterResult:
    rejected: bool
    reason: Optional[str] = None


NON_IT_TITLES = {
    "chief accountant", "księgowy", "truck driver", "kierowca", "medical doctor",
    "lekarz", "nurse", "pielęgniarka", "warehouse worker", "magazynier",
}
SENIOR_MARKERS = {"senior", "principal", "staff engineer", "architect", "director", "head of", "tech lead"}
IT_SIGNALS = {
    " it ", "developer", "python", "software", "support", "help desk", "service desk",
    "security", "soc", "network", "administrator", "tester", " qa ", "technical",
    "windows", "linux", "lan", "active directory", "ticket", "database", "cloud",
}


def filter_job(job: Job) -> FilterResult:
    title = normalized_text(job.title)
    text = " " + normalized_text(job.title + " " + job.description_normalized[:5000]) + " "
    if any(term in title for term in NON_IT_TITLES) and not any(signal in text for signal in IT_SIGNALS):
        return FilterResult(True, "clearly outside IT career scope")
    years = [int(value) for value in re.findall(r"(?:minimum|min\.?|at least|co najmniej)?\s*(\d{1,2})\+?\s*(?:years?|lat)", text)]
    hard_years = max(years) if years else 0
    if hard_years >= 4 and any(term in title for term in SENIOR_MARKERS):
        return FilterResult(True, "senior role with %d+ years required" % hard_years)
    if any(term in title for term in SENIOR_MARKERS) and not any(word in text for word in ("junior", "intern", "staż")):
        return FilterResult(True, "explicitly senior role")
    return FilterResult(False)

