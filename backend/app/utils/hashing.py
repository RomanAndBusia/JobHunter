import hashlib
import json
import re
import unicodedata
from typing import Any, Dict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref"}


def normalized_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(text.split())


def stable_hash(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def job_fingerprint(company: str, title: str, location: str) -> str:
    return stable_hash("|".join(normalized_text(item) for item in (company, title, location)))


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.casefold() not in TRACKING_PARAMS]
    path = re.sub(r"/+", "/", parts.path).rstrip("/")
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, urlencode(sorted(query)), ""))


def profile_hash(data: Dict[str, Any]) -> str:
    return stable_hash(data)

