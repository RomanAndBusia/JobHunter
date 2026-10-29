import re
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Tuple


SKILLS = {
    "python": "Python", "fastapi": "FastAPI", "django": "Django", "flask": "Flask",
    "javascript": "JavaScript", "typescript": "TypeScript", "react": "React", "angular": "Angular",
    "vue": "Vue", "node.js": "Node.js", "nodejs": "Node.js", "java": "Java", "c#": "C#",
    ".net": ".NET", "c++": "C++", "sql": "SQL", "postgresql": "PostgreSQL", "mysql": "MySQL",
    "sqlite": "SQLite", "mongodb": "MongoDB", "redis": "Redis", "docker": "Docker",
    "kubernetes": "Kubernetes", "git": "Git", "linux": "Linux", "windows": "Windows",
    "rest api": "REST", "restful": "REST", "graphql": "GraphQL", "azure": "Azure", "aws": "AWS",
    "active directory": "Active Directory", "tcp/ip": "TCP/IP", "networking": "Networking", "rest apis": "REST",
    "cybersecurity": "Cybersecurity", "siem": "SIEM", "soc": "SOC", "selenium": "Selenium",
    "playwright": "Playwright", "pytest": "pytest", "jira": "Jira", "help desk": "Help Desk",
}

LANGUAGES = {
    "english": "English", "polish": "Polish", "german": "German", "russian": "Russian",
    "ukrainian": "Ukrainian", "spanish": "Spanish", "french": "French",
    "angielski": "English", "polski": "Polish", "niemiecki": "German", "rosyjski": "Russian",
}

LEVELS = (
    (r"\b(c2|native|mother tongue|ojczysty)\b", "C2"),
    (r"\b(c1|advanced|fluent|biegły)\b", "C1"),
    (r"\b(b2|upper[- ]intermediate)\b", "B2"),
    (r"\b(b1|intermediate|średniozaawansowany)\b", "B1"),
    (r"\b(a2|elementary)\b", "A2"),
    (r"\b(a1|beginner|podstawowy)\b", "A1"),
)


def _unique(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def parse_cv_text(text: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Conservative local extraction. Explicit contact/language facts and derived skills retain distinct provenance."""
    clean = re.sub(r"[\t\r]+", " ", text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in clean.split("\n") if line.strip()]
    lower = clean.casefold()
    profile: Dict[str, Any] = {
        "identity": {}, "contact": {}, "location": {}, "education": [], "employment": [],
        "projects": [], "skills": [], "languages": [], "links": {}, "work_authorization": {},
        "preferences": {}, "career_goal": {"primary": "Get useful first professional IT experience."},
    }
    facts: List[Dict[str, Any]] = []

    def put(path: str, value: Any, source: str, confidence: float) -> None:
        current = profile
        parts = path.split(".")
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = value
        facts.append({"path": path, "concept": path.replace(".", "_"), "value": value, "source": source, "confidence": confidence})

    email = re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", clean, re.I)
    phone = re.search(r"(?<!\w)(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,3}\)?[\s.-]?)?\d{3}[\s.-]?\d{3}[\s.-]?\d{3}(?!\w)", clean)
    if email:
        put("contact.email", email.group(0), "CV_EXPLICIT", 0.99)
    if phone:
        put("contact.phone", " ".join(phone.group(0).split()), "CV_EXPLICIT", 0.97)
    for name, pattern in {
        "linkedin": r"https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9_%/-]+",
        "github": r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+",
        "portfolio": r"https?://(?![^\s]*(?:linkedin|github)\.com)[^\s<>]+",
    }.items():
        match = re.search(pattern, clean, re.I)
        if match:
            put("links." + name, match.group(0).rstrip(".,)"), "CV_EXPLICIT", 0.96)

    if lines:
        first = lines[0]
        if 2 <= len(first.split()) <= 5 and not any(ch.isdigit() for ch in first) and "@" not in first and len(first) < 80:
            put("identity.full_name", first, "CV_AI_EXTRACTED", 0.78)
            parts = first.split()
            if len(parts) >= 2:
                put("identity.first_name", parts[0], "CV_AI_EXTRACTED", 0.72)
                put("identity.last_name", " ".join(parts[1:]), "CV_AI_EXTRACTED", 0.72)

    derived_skills = []
    for token, canonical in SKILLS.items():
        if re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", lower):
            derived_skills.append(canonical)
    profile["skills"] = _unique(derived_skills)
    for index, skill in enumerate(profile["skills"]):
        facts.append({"path": "skills.%d" % index, "concept": "skill:" + skill.casefold(), "value": skill, "source": "DERIVED", "confidence": 0.88})

    languages = []
    for token, canonical in LANGUAGES.items():
        for match in re.finditer(r"\b" + re.escape(token) + r"\b", lower):
            window = lower[max(0, match.start() - 20):match.end() + 45]
            level = None
            for pattern, canonical_level in LEVELS:
                if re.search(pattern, window, re.I):
                    level = canonical_level
                    break
            languages.append({"language": canonical, "level": level or "Unspecified"})
            break
    seen_languages = set()
    for item in languages:
        if item["language"] in seen_languages:
            continue
        seen_languages.add(item["language"])
        profile["languages"].append(item)
        index = len(profile["languages"]) - 1
        facts.append({"path": "languages.%d" % index, "concept": "language:" + item["language"].casefold(), "value": item, "source": "CV_EXPLICIT", "confidence": 0.9 if item["level"] != "Unspecified" else 0.75})

    # Preserve useful section text without pretending a local heuristic understood every row.
    headings = {"education": ("education", "wykształcenie"), "employment": ("experience", "employment", "doświadczenie"), "projects": ("projects", "projekty")}
    for key, markers in headings.items():
        for index, line in enumerate(lines):
            if any(marker == line.casefold().strip(":") for marker in markers):
                block = lines[index + 1:index + 7]
                if block:
                    profile[key] = [{"text": value, "source": "CV_EXPLICIT"} for value in block]
                break
    return deepcopy(profile), facts
