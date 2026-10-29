from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.models import CandidateProfile, SearchCampaign, SearchPlan, SearchQuery
from app.utils.hashing import normalized_text


FAMILIES = {
    "backend": ["Junior Python", "Junior Backend Developer"],
    "junior_software": ["Junior Software Developer", "Software Developer Intern"],
    "help_desk": ["Help Desk IT", "Service Desk"],
    "it_support": ["IT Support", "Technical Support"],
    "qa": ["Junior QA", "Software Tester"],
    "cybersecurity": ["Junior Cybersecurity", "Cybersecurity Intern"],
    "soc": ["Junior SOC Analyst"],
    "networking": ["Junior Network Administrator", "Network Support"],
    "system_administration": ["Junior System Administrator", "IT Administrator"],
    "application_support": ["Application Support", "Junior Application Support"],
    "it_operations": ["IT Operations", "IT Operations Intern"],
    "internships": ["IT Internship", "Staż IT"],
}


def derive_role_families(profile: Dict[str, Any]) -> List[str]:
    compact = normalized_text(str(profile))
    result = ["help_desk", "it_support", "qa", "application_support", "internships"]
    signals = {
        "backend": ("python", "fastapi", "django", "java", "sql", "api"),
        "junior_software": ("programming", "developer", "javascript", "typescript", "react", "git"),
        "cybersecurity": ("cybersecurity", "security", "pentest", "siem"),
        "soc": ("soc", "siem", "incident response"),
        "networking": ("network", "tcp/ip", "cisco", "routing"),
        "system_administration": ("linux", "windows server", "active directory", "powershell"),
        "it_operations": ("docker", "cloud", "azure", "aws", "operations"),
    }
    for family, markers in signals.items():
        if any(marker in compact for marker in markers):
            result.append(family)
    return list(dict.fromkeys(result))


def build_search_campaign(db: Session, candidate: CandidateProfile, name: str = "CV-driven IT search") -> SearchCampaign:
    families = derive_role_families(candidate.data or {})
    campaign = SearchCampaign(candidate_id=candidate.id, name=name, goal="first_it_experience", status="READY")
    db.add(campaign)
    db.flush()
    db.add(SearchPlan(campaign_id=campaign.id, families=families, rationale={"source": "candidate_profile", "goal": "first_it_experience"}))
    seen = set()
    location = (candidate.data or {}).get("preferences", {}).get("location") or (candidate.data or {}).get("location", {}).get("city")
    for family in families:
        for query in FAMILIES.get(family, []):
            normalized = normalized_text(query)
            if normalized in seen:
                continue
            seen.add(normalized)
            db.add(SearchQuery(campaign_id=campaign.id, role_family=family, query=query, normalized_query=normalized, location=location))
    db.commit()
    db.refresh(campaign)
    return campaign
