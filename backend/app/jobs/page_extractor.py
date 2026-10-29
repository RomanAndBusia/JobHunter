import re
import html
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from app.utils.hashing import job_fingerprint, stable_hash


@dataclass
class ExtractedJobPage:
    title: str
    company: str
    location: str
    description: str
    requirements: List[str]
    responsibilities: List[str]
    work_mode: Optional[str]
    employment_type: Optional[str]
    seniority: Optional[str]
    compact: Dict[str, Any]
    raw_metadata: Dict[str, Any]

    def job_updates(self) -> Dict[str, Any]:
        content = {
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "description": self.description,
            "requirements": self.requirements,
            "responsibilities": self.responsibilities,
            "compact": self.compact,
        }
        return {
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "description_normalized": self.description,
            "requirements": self.requirements,
            "responsibilities": self.responsibilities,
            "work_mode": self.work_mode,
            "employment_type": self.employment_type,
            "seniority": self.seniority,
            "compact_json": self.compact,
            "raw_metadata": self.raw_metadata,
            "fingerprint": job_fingerprint(self.company, self.title, self.location),
            "content_hash": stable_hash(content),
        }


class GenericJobPageExtractor:
    """Extracts source-neutral vacancy data from a normal rendered job page."""

    _SCRIPT = r"""
    () => {
      const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
      const firstText = (selectors) => {
        for (const selector of selectors) {
          const node = document.querySelector(selector);
          const value = node && clean(node.textContent);
          if (value) return value;
        }
        return '';
      };
      const meta = (key) => {
        const node = document.querySelector(`meta[property="${key}"], meta[name="${key}"]`);
        return node ? clean(node.content) : '';
      };
      const postings = [];
      const walk = (value) => {
        if (!value || typeof value !== 'object') return;
        if (Array.isArray(value)) { value.forEach(walk); return; }
        const kind = value['@type'];
        if (kind === 'JobPosting' || (Array.isArray(kind) && kind.includes('JobPosting'))) postings.push(value);
        if (value['@graph']) walk(value['@graph']);
      };
      document.querySelectorAll('script[type="application/ld+json"]').forEach((node) => {
        try { walk(JSON.parse(node.textContent || 'null')); } catch (_) {}
      });
      const job = postings[0] || {};
      const org = job.hiringOrganization || {};
      const address = ((job.jobLocation || {}).address || {});
      const location = clean([
        address.addressLocality, address.addressRegion, address.addressCountry
      ].filter(Boolean).join(', '));
      const apply = Array.from(document.querySelectorAll('a[href], button')).find((node) =>
        /^(apply|apply now|submit application|aplikuj|aplikuj teraz)$/i.test(clean(node.textContent))
      );
      const body = (document.body ? document.body.innerText : '').slice(0, 50000);
      const content=document.querySelector('main, article') || document.body;
      const clone=content && content.cloneNode(true);
      if (clone) clone.querySelectorAll('form,header,nav,footer,[role=navigation],script,style').forEach(el=>el.remove());
      const narrative=clone ? clean(clone.textContent) : '';
      // Many SPAs render application controls without an HTML form element.
      // Their labels/options/consents are not vacancy-description evidence.
      const applicationControls = document.querySelector('input[type=file]') ||
        document.querySelectorAll('input:not([type=hidden]):not([type=search]),textarea,select,[role=combobox]').length >= 3;
      return {
        title: clean(job.title) || meta('og:title') || firstText(['h1', '[data-testid*="title" i]', '[class*="job-title" i]']),
        company: clean(org.name) || firstText(['[itemprop="hiringOrganization"]', '[data-testid*="company" i]', '[class*="company" i]']),
        location: location || firstText(['[itemprop="jobLocation"]', '[data-testid*="location" i]', '[class*="location" i]']),
        description: clean(job.description) || firstText(['[itemprop="description"]', '[class*="job-description" i]']) || (!applicationControls ? narrative || meta('description') : ''),
        employment_type: clean(Array.isArray(job.employmentType) ? job.employmentType.join(', ') : job.employmentType),
        deadline: clean(job.validThrough),
        salary: job.baseSalary || null,
        applicant_location: job.applicantLocationRequirements || null,
        body,
        application_url: apply && apply.href ? apply.href : '',
      };
    }
    """

    async def extract(self, page: Any) -> ExtractedJobPage:
        data = await page.evaluate(self._SCRIPT)
        for frame in getattr(page, 'frames', []):
            if frame is getattr(page, 'main_frame', None) or any(x in frame.url.lower() for x in ('recaptcha', 'hcaptcha', 'challenges.cloudflare')):
                continue
            try:
                child = await frame.evaluate(self._SCRIPT)
                if child.get('title') and len(child.get('description') or '') > len(data.get('description') or ''):
                    data = child
            except Exception:
                continue
        url = getattr(page, "url", "")
        host = urlsplit(url).hostname or ""
        body = str(data.get("body") or "")
        description = self._clean_html_text(str(data.get("description") or ''))[:40000]
        title = str(data.get("title") or "Job at %s" % host)[:300]
        company = str(data.get("company") or self._company_from_host(host))[:300]
        location = str(data.get("location") or "")[:300]
        requirements = self._section_lines(body, ("requirements", "wymagania", "qualifications"))
        responsibilities = self._section_lines(body, ("responsibilities", "obowiązki", "what you will do"))
        lower = (description + " " + body[:12000]).casefold()
        work_mode = "remote" if re.search(r"\b(remote|zdaln)\w*", lower) else (
            "hybrid" if re.search(r"\b(hybrid|hybryd)\w*", lower) else (
                "on-site" if re.search(r"\b(on[- ]site|stacjonarn)\w*", lower) else None
            )
        )
        provider = self._provider(host)
        technologies = sorted(set(re.findall(
            r"\b(Python|Java|JavaScript|TypeScript|React|Angular|Vue|SQL|AWS|Azure|GCP|Docker|Kubernetes|Linux|Git)\b",
            description,
            re.IGNORECASE,
        )))
        languages = sorted(set(re.findall(
            r"\b(English|Polish|German|French|Spanish|angielski|polski|niemiecki|francuski|hiszpański)\b",
            description,
            re.IGNORECASE,
        )))
        compact = {
            "title": title,
            "company": company,
            "location": location,
            "work_mode": work_mode,
            "employment_type": data.get("employment_type") or None,
            "requirements": requirements,
            "responsibilities": responsibilities,
            "technologies": technologies,
            "languages": languages,
            "salary": data.get("salary"),
            "application_deadline": data.get("deadline") or None,
            "application_url": data.get("application_url") or None,
            "ats_provider": provider,
        }
        return ExtractedJobPage(
            title=title,
            company=company,
            location=location,
            description=description,
            requirements=requirements,
            responsibilities=responsibilities,
            work_mode=work_mode,
            employment_type=str(data.get("employment_type") or "") or None,
            seniority=self._seniority(title + " " + description[:4000]),
            compact=compact,
            raw_metadata={"source": "direct_url", "provider": provider, "page_url": url,
                          "company_fallback": not bool(data.get('company'))},
        )

    @staticmethod
    def _clean_html_text(value: str) -> str:
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())

    @staticmethod
    def _company_from_host(host: str) -> str:
        parts = [item for item in host.split(".") if item and item not in {"www", "jobs", "careers"}]
        return (parts[0].replace("-", " ").title() if parts else "Unknown company")

    @staticmethod
    def _provider(host: str) -> str:
        for marker, provider in (
            ("greenhouse", "greenhouse"), ("lever.co", "lever"),
            ("myworkdayjobs", "workday"), ("smartrecruiters", "smartrecruiters"),
            ("teamtailor", "teamtailor"), ("ashbyhq", "ashby"),
            ("traffit", "traffit"), ("recruitee", "recruitee"), ("pracuj.pl", "pracuj"),
        ):
            if marker in host.casefold():
                return provider
        return "company_site"

    @staticmethod
    def _seniority(text: str) -> Optional[str]:
        value = text.casefold()
        for marker, label in (("intern", "internship"), ("staż", "internship"), ("junior", "junior"),
                              ("senior", "senior"), ("lead", "lead"), ("mid", "mid")):
            if marker in value:
                return label
        return None

    @staticmethod
    def _section_lines(body: str, headings: tuple) -> List[str]:
        lower = body.casefold()
        start = min([lower.find(item) for item in headings if lower.find(item) >= 0] or [-1])
        if start < 0:
            return []
        fragment = body[start:start + 5000]
        parts = [" ".join(item.split()) for item in re.split(r"[\n\r•]+|(?<=[.;])\s+", fragment)]
        return [item[:500] for item in parts[1:16] if 12 <= len(item) <= 500]
