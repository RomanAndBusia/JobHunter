import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.browser.manager import browser_manager
from app.browser.challenge import ManualVerificationPending
from app.config import settings
from app.jobs.normalizer import normalize_job
from app.logging import event
from app.models import JobScan, SearchProfile
from app.repositories.jobs import JobRepository
from app.utils.hashing import canonicalize_url, stable_hash
from app.utils.hashing import normalized_text


logger = logging.getLogger(__name__)
PRACUJ_HOST = "https://www.pracuj.pl"
JOB_ID_RE = re.compile(r"(?:oferta,|offerId=|/)(\d{5,})(?:[/?#]|$)", re.IGNORECASE)


@dataclass
class Listing:
    source_job_id: str
    url: str
    title: str = ""
    company: str = ""
    location: str = ""

    @property
    def signature(self) -> str:
        return stable_hash({"u": canonicalize_url(self.url), "t": self.title, "c": self.company, "l": self.location})


def _job_id(url: str, fallback: str = "") -> str:
    match = JOB_ID_RE.search(url)
    return match.group(1) if match else stable_hash(fallback or canonicalize_url(url))[:24]


def _json_ld(soup: BeautifulSoup) -> Iterable[Dict[str, Any]]:
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(tag.get_text(strip=True))
        except (TypeError, ValueError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict):
                yield item
                graph = item.get("@graph", [])
                if isinstance(graph, list):
                    for child in graph:
                        if isinstance(child, dict):
                            yield child


def parse_search_page(html: str) -> List[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    by_id = {}
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "")
        if "oferta" not in href.casefold() and "/praca/" not in href.casefold():
            continue
        url = urljoin(PRACUJ_HOST, href)
        if urlsplit(url).netloc not in {"www.pracuj.pl", "pracuj.pl"}:
            continue
        title = str(anchor.get("title") or anchor.get_text(" ", strip=True))
        source_id = _job_id(url, title)
        if source_id not in by_id:
            container = anchor.find_parent(["article", "li"]) or anchor.parent
            text = container.get_text(" | ", strip=True) if container else title
            parts = [part.strip() for part in text.split("|") if part.strip()]
            by_id[source_id] = Listing(source_id, url, title[:300], parts[1][:300] if len(parts) > 1 else "", parts[2][:300] if len(parts) > 2 else "")
    return list(by_id.values())


def _address_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(filter(None, (_address_text(item) for item in value)))
    if isinstance(value, dict):
        address = value.get("address", value)
        if isinstance(address, dict):
            return ", ".join(str(address.get(key, "")) for key in ("addressLocality", "addressRegion", "addressCountry") if address.get(key))
    return ""


def parse_job_page(html: str, listing: Listing) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    posting = next((item for item in _json_ld(soup) if item.get("@type") == "JobPosting"), {})
    title_tag = soup.select_one("h1")
    description_node = soup.select_one('[data-test="section-offer-description"]') or soup.select_one("main")
    org = posting.get("hiringOrganization", {})
    company = org.get("name", "") if isinstance(org, dict) else str(org or "")
    description_html = posting.get("description") or (str(description_node) if description_node else "")
    description = BeautifulSoup(str(description_html), "html.parser").get_text("\n", strip=True)
    responsibilities = []
    requirements = []
    for heading in soup.select("h2, h3"):
        name = heading.get_text(" ", strip=True).casefold()
        parent = heading.parent
        values = [node.get_text(" ", strip=True) for node in parent.select("li")] if parent else []
        if any(word in name for word in ("wymag", "require", "expect")):
            requirements.extend(values)
        elif any(word in name for word in ("obowią", "responsib", "tasks", "zadania")):
            responsibilities.extend(values)
    employment = posting.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(str(item) for item in employment)
    return {
        "title": posting.get("title") or (title_tag.get_text(" ", strip=True) if title_tag else listing.title),
        "company": company or listing.company,
        "location": _address_text(posting.get("jobLocation")) or listing.location,
        "description": description,
        "requirements": requirements,
        "responsibilities": responsibilities,
        "employment_type": employment,
        "raw_metadata": {
            "date_posted": posting.get("datePosted"),
            "valid_through": posting.get("validThrough"),
            "listing_signature": listing.signature,
        },
    }


class PracujScanner:
    def __init__(self, db: Session):
        self.db = db
        self.repo = JobRepository(db)
        self._semaphore = asyncio.Semaphore(settings.scanner_concurrency)
        self._browser_page = None
        self._previous_active_page_id = None
        self._manual_verification_pending = False

    @staticmethod
    def search_url(profile: SearchProfile, page: int) -> str:
        query = quote(profile.query.strip(), safe="")
        url = "%s/praca/%s;kw" % (PRACUJ_HOST, query)
        params = ["pn=%d" % page]
        if profile.location:
            params.append("wp=%s" % quote(profile.location, safe=""))
        return url + "?" + "&".join(params)

    async def _get(self, client: httpx.AsyncClient, url: str) -> str:
        async with self._semaphore:
            html = await self._get_with_browser(url)
            await asyncio.sleep(settings.scanner_delay_seconds)
            return html

    async def _get_with_browser(self, url: str) -> str:
        await browser_manager.ensure_started()
        if self._browser_page is None and browser_manager.active_challenge_page_id:
            try:
                pending_page = browser_manager.get_page(browser_manager.active_challenge_page_id)
                if "pracuj.pl" in urlsplit(pending_page.url).netloc.casefold():
                    self._browser_page = pending_page
            except RuntimeError:
                pass
        if self._browser_page is None or self._browser_page.is_closed():
            assert browser_manager.context is not None
            self._previous_active_page_id = browser_manager.active_application_page_id
            self._browser_page = await browser_manager.context.new_page()
            browser_manager._register(self._browser_page)
            browser_manager.active_application_page_id = self._previous_active_page_id
        # A resumable challenge is read/polled before any further navigation.
        if browser_manager.active_challenge_page_id:
            prior = await browser_manager.wait_for_manual_verification(self._browser_page)
            if prior.timed_out:
                self._manual_verification_pending = True
                raise ManualVerificationPending("Complete manual verification in the existing Chrome tab")
            response = None if self._browser_page.url == url else await self._browser_page.goto(url, wait_until="domcontentloaded", timeout=45000)
        else:
            response = await self._browser_page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await self._browser_page.wait_for_timeout(750)
        async def challenge_progress(stage: str, state: Any) -> None:
            event(
                logger, "pracuj_manual_verification", stage=stage,
                provider=state.provider, page_url=state.url,
            )
        verification = await browser_manager.wait_for_manual_verification(
            self._browser_page, challenge_progress,
        )
        if verification.timed_out:
            self._manual_verification_pending = True
            raise ManualVerificationPending("Pracuj manual verification is still required; complete it in the visible browser and retry the scan")
        if response is not None and response.status >= 400 and not verification.initial.detected:
            raise RuntimeError("Pracuj browser navigation returned HTTP %d" % response.status)
        return await self._browser_page.content()

    @staticmethod
    def _matches_profile(job: Any, profile: SearchProfile) -> bool:
        if profile.work_modes:
            aliases = {
                "zdalna": "remote", "remote": "remote", "home office": "remote",
                "hybrydowa": "hybrid", "hybrid": "hybrid",
                "stacjonarna": "on-site", "on-site": "on-site", "onsite": "on-site",
            }
            wanted = {aliases.get(normalized_text(value), normalized_text(value)) for value in profile.work_modes}
            if job.work_mode and job.work_mode not in wanted:
                return False
        if profile.employment_types and job.employment_type:
            actual = normalized_text(job.employment_type)
            if not any(normalized_text(value) in actual for value in profile.employment_types):
                return False
        if profile.seniority and job.seniority:
            wanted_levels = {normalized_text(value) for value in profile.seniority}
            if normalized_text(job.seniority) not in wanted_levels:
                return False
        return True

    async def scan(self, profile: SearchProfile) -> JobScan:
        scan = JobScan(profile_id=profile.id, status="RUNNING")
        self.db.add(scan)
        self.db.commit()
        listings = {}
        try:
            client = None
            for page in range(1, profile.max_pages + 1):
                html = await self._get(client, self.search_url(profile, page))
                page_listings = parse_search_page(html)
                if not page_listings:
                    break
                for item in page_listings:
                    listings[item.source_job_id] = item

            scan.seen_count = len(listings)
            for listing in listings.values():
                known = self.repo.find("pracuj", listing.source_job_id)
                known_signature = (known.raw_metadata or {}).get("listing_signature") if known else None
                if known is not None and known_signature == listing.signature:
                    known.last_seen_at = datetime.utcnow()
                    scan.unchanged_count += 1
                    scan.details = {**(scan.details or {}), "job_ids": list((scan.details or {}).get("job_ids", [])) + [known.id]}
                    continue
                try:
                    html = await self._get(client, listing.url)
                    data = parse_job_page(html, listing)
                    normalized = normalize_job("pracuj", listing.source_job_id, listing.url, data)
                    profile_matches = self._matches_profile(normalized, profile)
                    if not profile_matches:
                        details = dict(scan.details or {})
                        details["filtered_by_profile"] = int(details.get("filtered_by_profile", 0)) + 1
                        scan.details = details
                    payload = normalized.persistence_dict()
                    payload["details_fetched_at"] = datetime.utcnow()
                    payload["content_checked_at"] = datetime.utcnow()
                    result = self.repo.upsert(payload)
                    scan.details = {**(scan.details or {}), "job_ids": list((scan.details or {}).get("job_ids", [])) + [result.job.id]}
                    if not profile_matches:
                        result.job.raw_metadata = {**(result.job.raw_metadata or {}), "search_filter_skip": True}
                        result.job.matching_status = "REJECTED"
                    if result.disposition == "new":
                        scan.new_count += 1
                    elif result.disposition == "changed":
                        scan.changed_count += 1
                    else:
                        scan.unchanged_count += 1
                except ManualVerificationPending:
                    raise
                except Exception as exc:
                    scan.error_count += 1
                    event(logger, "job_detail_failed", source_job_id=listing.source_job_id, error_type=type(exc).__name__)
            scan.status = "COMPLETED" if scan.error_count == 0 else "COMPLETED_WITH_ERRORS"
            scan.completed_at = datetime.utcnow()
            self.db.commit()
            return scan
        except ManualVerificationPending as exc:
            scan.status = "WAITING_FOR_MANUAL_VERIFICATION"
            scan.completed_at = datetime.utcnow()
            scan.details = {"manual_intervention": str(exc), "challenge": "CAPTCHA"}
            self.db.commit()
            return scan
        except Exception as exc:
            scan.status = "FAILED"
            scan.completed_at = datetime.utcnow()
            scan.details = {"error": str(exc), "error_type": type(exc).__name__}
            self.db.commit()
            raise
        finally:
            if not self._manual_verification_pending and self._browser_page is not None and not self._browser_page.is_closed():
                await browser_manager.close_page(browser_manager._register(self._browser_page))
            if self._previous_active_page_id in browser_manager.pages:
                browser_manager.active_application_page_id = self._previous_active_page_id
