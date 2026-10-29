import hashlib
import io
import logging
import re
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict
from xml.etree import ElementTree

from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.logging import event
from app.models import CandidateDocument, CandidateFact, CandidateProfile


logger = logging.getLogger(__name__)
ProviderFactory = Callable[..., AIProvider]


ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt"}


def _extract_known_facts(text: str) -> Dict[str, Any]:
    facts = {}
    email = re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, re.IGNORECASE)
    phone = re.search(r"(?<!\w)(?:\+\d{1,3}[\s.-]?)?(?:\(?\d{2,3}\)?[\s.-]?)?\d{3}[\s.-]?\d{3}[\s.-]?\d{3}(?!\w)", text)
    linkedin = re.search(r"https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9_%/-]+", text, re.IGNORECASE)
    github = re.search(r"https?://(?:www\.)?github\.com/[A-Za-z0-9_.-]+", text, re.IGNORECASE)
    if email:
        facts["contact.email"] = email.group(0)
    if phone:
        facts["contact.phone"] = " ".join(phone.group(0).split())
    if linkedin:
        facts["links.linkedin"] = linkedin.group(0)
    if github:
        facts["links.github"] = github.group(0)
    return facts


def _set_path(data: Dict[str, Any], path: str, value: Any) -> None:
    current = data
    parts = path.split(".")
    for key in parts[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    if current.get(parts[-1]) in (None, ""):
        current[parts[-1]] = value


def _safe_name(name: str) -> str:
    base = Path(name).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:180]


def _empty(value: Any) -> bool:
    return value in (None, "", [], {})


def _merge_profile(current: Any, extracted: Any) -> Any:
    if _empty(current):
        return deepcopy(extracted)
    if isinstance(current, dict) and isinstance(extracted, dict):
        result = deepcopy(current)
        for key, value in extracted.items():
            result[key] = _merge_profile(result.get(key), value)
        return result
    return current


def _path_value(data: Dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


async def import_document(
    db: Session,
    profile: CandidateProfile,
    name: str,
    stream: BinaryIO,
    storage_root: Path,
    provider_factory: ProviderFactory = get_ai_provider,
) -> CandidateDocument:
    safe_name = _safe_name(name)
    suffix = Path(safe_name).suffix.casefold()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError("Only PDF and DOCX candidate documents are supported")
    content = stream.read()
    if len(content) > 10 * 1024 * 1024:
        raise ValueError("Document is larger than 10 MB")
    sha = hashlib.sha256(content).hexdigest()
    current = db.execute(select(func.max(CandidateDocument.version)).where(
        CandidateDocument.profile_id == profile.id,
        CandidateDocument.kind == "CV",
    )).scalar_one()
    extracted = ""
    if suffix == ".txt":
        extracted = content.decode("utf-8", errors="replace")
    elif suffix == ".pdf":
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted and reader.decrypt("") == 0:
                raise ValueError("Password-protected PDF documents are not supported")
            extracted = "\n".join((page.extract_text() or "") for page in reader.pages)
        except ValueError:
            raise
        except (PdfReadError, OSError) as exc:
            raise ValueError("The PDF file is invalid or unreadable") from exc
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                root = ElementTree.fromstring(archive.read("word/document.xml"))
            paragraphs = []
            for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                paragraphs.append("".join(node.text or "" for node in paragraph.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")))
            extracted = "\n".join(paragraphs)
        except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise ValueError("The DOCX file is invalid or unreadable") from exc
    if not extracted.strip():
        raise ValueError("The candidate document contains no readable text")
    provider = provider_factory(db=db)
    event(logger, "ai.cv_parse.started", profile_id=profile.id, input_chars=len(extracted), provider=provider.name, model=getattr(provider, "model", ""))
    parsed = await provider.extract_candidate_profile(extracted)
    parsed_profile = parsed.profile.model_dump()
    event(
        logger, "ai.cv_parse.completed", profile_id=profile.id, input_chars=len(extracted),
        fact_count=len(parsed.facts), experience_count=len(parsed.profile.employment),
        project_count=len(parsed.profile.projects), provider=provider.name,
        model=getattr(provider, "model", ""), request_id=getattr(provider, "last_request", {}).get("request_id"),
    )

    version = int(current or 0) + 1
    folder = storage_root / "documents" / str(profile.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("cv-v%d-%s" % (version, safe_name))
    path.write_bytes(content)
    db.execute(update(CandidateDocument).where(
        CandidateDocument.profile_id == profile.id,
        CandidateDocument.kind == "CV",
    ).values(active=False))
    document = CandidateDocument(
        profile_id=profile.id, kind="CV", version=version, original_name=safe_name,
        storage_path=str(path), sha256=sha, extracted_text=extracted[:200000], active=True,
    )
    db.add(document)
    profile_data = _merge_profile(profile.data or {}, parsed_profile)
    fallback_facts = [{"path": path, "concept": path.replace(".", "_"), "value": value, "source": "CV_EXPLICIT", "confidence": 0.9} for path, value in _extract_known_facts(extracted).items()]
    facts_by_path: Dict[str, Dict[str, Any]] = {}
    ai_facts = []
    for extracted_fact in parsed.facts:
        value = _path_value(parsed_profile, extracted_fact.path)
        if _empty(value):
            continue
        ai_facts.append({
            "path": extracted_fact.path,
            "concept": extracted_fact.path.replace(".", "_"),
            "value": value,
            "source": extracted_fact.source,
            "confidence": extracted_fact.confidence,
        })
    for extracted_fact in ai_facts + fallback_facts:
        previous = facts_by_path.get(extracted_fact["path"])
        if previous is None or float(extracted_fact["confidence"]) > float(previous["confidence"]):
            facts_by_path[extracted_fact["path"]] = extracted_fact
    for extracted_fact in facts_by_path.values():
        fact_path = extracted_fact["path"]
        fact_value = extracted_fact["value"]
        existing_fact = db.execute(select(CandidateFact).where(
            CandidateFact.profile_id == profile.id,
            CandidateFact.path == fact_path,
        )).scalar_one_or_none()
        if existing_fact is None:
            db.add(CandidateFact(
                profile_id=profile.id, path=fact_path, value=fact_value,
                concept=extracted_fact.get("concept"), source=extracted_fact["source"],
                confidence=extracted_fact["confidence"], user_confirmed=False,
            ))
            if not any(part.isdigit() for part in fact_path.split(".")):
                _set_path(profile_data, fact_path, fact_value)
        elif (
            not existing_fact.user_confirmed
            and existing_fact.source not in {"USER_CONFIRMED", "USER_ENTERED"}
        ) or _empty(existing_fact.value):
            existing_fact.value = fact_value
            existing_fact.concept = extracted_fact.get("concept")
            existing_fact.source = extracted_fact["source"]
            existing_fact.confidence = extracted_fact["confidence"]
        if not any(part.isdigit() for part in fact_path.split(".")):
            _set_path(profile_data, fact_path, fact_value)
    profile.data = profile_data
    db.commit()
    db.refresh(document)
    return document
