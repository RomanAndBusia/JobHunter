import asyncio

import pytest

from app.applications.direct import DirectApplicationService
from app.applications.lifecycle import ApplicationLifecycleService
from app.browser.challenge import ChallengeDetector, ChallengeState, classify_challenge_signals
from app.browser.challenge import ManualVerificationPending
from app.jobs.page_extractor import GenericJobPageExtractor
from app.models import ApplicationAnswer, ApplicationQuestionRequest, CandidateFact, CandidateProfile, FormSnapshot, SearchProfile
from app.scanner.pracuj import PracujScanner


def test_cloudflare_challenge_uses_multiple_signals():
    state = classify_challenge_signals(
        "https://example.test/cdn-cgi/challenge-platform/check",
        "Just a moment...",
        "Checking your browser before accessing Example",
        ["https://challenges.cloudflare.com/turnstile/v0/api.js"],
        [".cf-turnstile"],
        403,
    )
    assert state.detected is True
    assert state.provider == "cloudflare"
    assert state.requires_manual_action is True
    assert {"url_pattern", "page_title", "visible_text", "challenge_iframe", "challenge_dom"}.issubset(state.evidence)


def test_suspicious_http_status_alone_is_not_mislabeled_as_captcha():
    state = classify_challenge_signals(
        "https://example.test/jobs/123", "Job not available", "Access denied", [], [], 403,
    )
    assert state.detected is False
    assert state.provider is None


def test_live_cloudflare_verification_without_widget_is_a_visible_interstitial():
    state=classify_challenge_signals('https://www.pracuj.pl/praca/it;kw','Just a moment...',
        'Performing security verification. This page is displayed while the website verifies you are not a bot. Performance and Security by Cloudflare',[],[],403)
    assert state.detected and state.blocking and state.provider=='cloudflare'
    assert {'page_title','visible_text'}.issubset(state.evidence)
    normal=classify_challenge_signals('https://example.test/job','IT role',
        'FAQ: performing security verification may be required later',[],[],200)
    assert not normal.detected


def test_polish_interstitial_title_is_detected():
    state = classify_challenge_signals(
        "https://www.pracuj.pl/praca/junior-it;kw", "Cierpliwości...",
        "Weryfikujemy Twoją przeglądarkę", [], [], 200,
    )
    assert state.detected is True
    assert "page_title" in state.evidence


@pytest.mark.asyncio
async def test_manual_gate_resumes_after_challenge_disappears():
    class SequencedDetector(ChallengeDetector):
        def __init__(self):
            self.states = [
                ChallengeState(True, "recaptcha", True, evidence=["challenge_iframe"]),
                ChallengeState(True, "recaptcha", True, evidence=["challenge_iframe"]),
                ChallengeState(False),
            ]

        async def detect(self, page, http_status=None, include_pending=False):
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    result = await SequencedDetector().wait_until_clear(object(), 1, 0.001)
    assert result.cleared is True
    assert result.timed_out is False
    assert result.initial.provider == "recaptcha"


@pytest.mark.asyncio
async def test_pracuj_scan_waits_instead_of_failing_on_manual_challenge(db, monkeypatch):
    profile = SearchProfile(name="challenge-test", query="junior it", max_pages=1)
    db.add(profile)
    db.commit()
    scanner = PracujScanner(db)

    async def pending(_client, _url):
        raise ManualVerificationPending("manual verification required")

    monkeypatch.setattr(scanner, "_get", pending)
    result = await scanner.scan(profile)
    assert result.status == "WAITING_FOR_MANUAL_VERIFICATION"
    assert result.error_count == 0
    assert result.details["challenge"] == "CAPTCHA"


def test_direct_url_creates_one_job_and_application(db):
    candidate = CandidateProfile(name="Candidate", data={})
    db.add(candidate)
    db.commit()
    service = DirectApplicationService()

    job, application, created = service.create(db, candidate, "https://company.example/jobs/123?utm_source=test")
    duplicate_job, duplicate_application, duplicate_created = service.create(db, candidate, "https://company.example/jobs/123")

    assert created is True
    assert duplicate_created is False
    assert job.id == duplicate_job.id
    assert application.id == duplicate_application.id
    assert job.source == "direct"
    assert job.canonical_url == "https://company.example/jobs/123"
    assert application.review_data["origin"] == "direct_url"
    assert application.review_data["ai_runtime"] in {"api", "codex"}


def test_duplicate_direct_url_preserves_extracted_job_and_match_state(db):
    candidate = CandidateProfile(name="Candidate", data={})
    db.add(candidate)
    db.commit()
    service = DirectApplicationService()
    job, _application, _created = service.create(db, candidate, "https://company.example/jobs/123")
    job.title = "Extracted Junior Support Engineer"
    job.description_normalized = "Real extracted vacancy content"
    job.compact_json = {"title": job.title, "requirements": ["Windows"]}
    job.content_hash = "e" * 64
    job.matching_status = "ACCEPTED"
    db.commit()

    duplicate, _application, created = service.create(db, candidate, "https://company.example/jobs/123")

    assert created is False
    assert duplicate.title == "Extracted Junior Support Engineer"
    assert duplicate.description_normalized == "Real extracted vacancy content"
    assert duplicate.compact_json["requirements"] == ["Windows"]
    assert duplicate.content_hash == "e" * 64
    assert duplicate.matching_status == "ACCEPTED"


def test_different_direct_urls_are_isolated_application_sessions(db):
    candidate = CandidateProfile(name="Candidate", data={"languages": [{"language": "English", "level": "B2"}]})
    db.add(candidate); db.commit()
    service = DirectApplicationService()
    first_job, first, _ = service.create(db, candidate, "https://company.example/jobs/123")
    second_job, second, _ = service.create(db, candidate, "https://company.example/jobs/124")
    assert first_job.id != second_job.id
    assert first.id != second.id
    assert first.url_fingerprint != second.url_fingerprint


def test_reset_clears_only_vacancy_state_and_preserves_candidate_facts(db):
    candidate = CandidateProfile(name="Candidate", data={"languages": [{"language": "English", "level": "B2"}]})
    db.add(candidate); db.flush()
    db.add(CandidateFact(
        profile_id=candidate.id, path="languages", value=candidate.data["languages"],
        source="USER_ENTERED", confidence=1.0, user_confirmed=True,
    ))
    _job, application, _ = DirectApplicationService().create(db, candidate, "https://company.example/jobs/123")
    db.add(ApplicationAnswer(application_id=application.id, field_id="email", question="Email", value="x", source="USER_ENTERED"))
    db.add(ApplicationQuestionRequest(application_id=application.id, candidate_id=candidate.id, field_id="salary", question="Salary"))
    db.add(FormSnapshot(application_id=application.id, url="https://company.example/jobs/123", host="company.example", semantic_form={}, fingerprint="f" * 64))
    application.error = "old vacancy error"
    db.commit()

    ApplicationLifecycleService().reset(db, application)
    assert application.status == "READY"
    assert application.error is None
    assert db.query(ApplicationAnswer).count() == 0
    assert db.query(ApplicationQuestionRequest).count() == 0
    assert db.query(FormSnapshot).count() == 0
    assert db.query(CandidateFact).filter_by(profile_id=candidate.id).count() == 1


def test_delete_removes_direct_application_but_preserves_candidate_profile(db):
    candidate = CandidateProfile(name="Candidate", data={"location": {"country": "Poland"}})
    db.add(candidate); db.flush()
    db.add(CandidateFact(
        profile_id=candidate.id, path="location.country", value="Poland",
        source="USER_ENTERED", confidence=1.0, user_confirmed=True,
    ))
    job, application, _ = DirectApplicationService().create(db, candidate, "https://company.example/jobs/delete-me")
    application_id, job_id, candidate_id = application.id, job.id, candidate.id
    ApplicationLifecycleService().delete(db, application)
    assert db.get(type(application), application_id) is None
    assert db.get(type(job), job_id) is None
    assert db.get(CandidateProfile, candidate_id) is not None
    assert db.query(CandidateFact).filter_by(profile_id=candidate_id).count() == 1


@pytest.mark.parametrize("value", ["", "company.example/jobs/1", "file:///tmp/job", "https://user:secret@example.test/job"])
def test_direct_url_rejects_invalid_or_credentialed_urls(value):
    with pytest.raises(ValueError):
        DirectApplicationService.validate_url(value)


@pytest.mark.asyncio
async def test_generic_job_page_extraction_captures_job_metadata():
    class FakePage:
        url = "https://boards.greenhouse.io/example/jobs/42"

        async def evaluate(self, _script):
            return {
                "title": "Junior Python Developer",
                "company": "Example",
                "location": "Warsaw, PL",
                "description": "Requirements\nPython and SQL\nResponsibilities\nBuild reliable tools\nEnglish B2\nRemote work",
                "employment_type": "FULL_TIME",
                "deadline": "2026-12-31",
                "salary": {"value": 10000},
                "body": "Requirements\nPython and SQL\nResponsibilities\nBuild reliable tools\nEnglish B2\nRemote work",
                "application_url": "https://boards.greenhouse.io/example/jobs/42#app",
            }

    result = await GenericJobPageExtractor().extract(FakePage())
    assert result.title == "Junior Python Developer"
    assert result.company == "Example"
    assert result.work_mode == "remote"
    assert result.seniority == "junior"
    assert result.compact["ats_provider"] == "greenhouse"
    assert "Python" in result.compact["technologies"]
