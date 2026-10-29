from app.jobs.normalizer import normalize_job
from app.models import Job
from app.repositories.jobs import JobRepository
from app.scanner.pracuj import parse_search_page
from app.scanner.pracuj import PracujScanner
from app.utils.hashing import canonicalize_url, job_fingerprint, stable_hash


def payload(description="Python and Windows support"):
    return normalize_job("pracuj", "123456", "https://www.pracuj.pl/praca/x,oferta,123456?utm_source=test", {
        "title": "Junior IT Support", "company": "Example", "location": "Warsaw",
        "description": description, "requirements": ["Windows", "English B2"],
        "responsibilities": ["Handle tickets"], "employment_type": "full-time",
        "raw_metadata": {"listing_signature": "v1"},
    }).persistence_dict()


def test_hashes_are_stable_and_urls_drop_tracking():
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})
    assert job_fingerprint(" ACME ", "Junior IT", "Warsaw") == job_fingerprint("acme", "junior it", "warsaw")
    assert canonicalize_url("https://EXAMPLE.com/job/?utm_source=x&a=1") == "https://example.com/job?a=1"


def test_job_upsert_deduplicates_and_invalidates_changed_content(db):
    repo = JobRepository(db)
    first = repo.upsert(payload())
    db.commit()
    same = repo.upsert(payload())
    changed = repo.upsert(payload("Python, Linux and network support"))
    db.commit()
    assert first.disposition == "new"
    assert same.disposition == "unchanged"
    assert changed.disposition == "changed"
    assert db.query(Job).count() == 1
    assert changed.job.matching_status == "PENDING"


def test_compact_normalization_keeps_semantics_without_description():
    compact = payload()["compact_json"]
    assert compact["id"] == "p:123456"
    assert compact["req"] == ["Windows", "English B2"]
    assert "description" not in compact


def test_search_parser_deduplicates_overlapping_links():
    html = """<article><a href='/praca/a,oferta,123456'>Junior IT</a></article>
    <article><a href='https://www.pracuj.pl/praca/a,oferta,123456?ref=x'>Same</a></article>
    <article><a href='/praca/b,oferta,987654'>QA Intern</a></article>"""
    result = parse_search_page(html)
    assert [item.source_job_id for item in result] == ["123456", "987654"]


def test_search_profile_filters_work_mode_employment_and_seniority():
    from types import SimpleNamespace
    job = normalize_job("pracuj", "1", "https://example.test", {
        "title": "Junior Support", "description": "remote support", "employment_type": "Umowa o pracę",
    })
    matching = SimpleNamespace(work_modes=["remote"], employment_types=["umowa o pracę"], seniority=["entry"])
    rejected = SimpleNamespace(work_modes=["on-site"], employment_types=[], seniority=[])
    assert PracujScanner._matches_profile(job, matching)
    assert not PracujScanner._matches_profile(job, rejected)
