from io import BytesIO

import pytest

from app.ai.contracts import (
    CandidateProfileExtraction, CandidateProfilePayload, ContactProfile, EducationItem,
    ExperienceItem, ExtractedFact, IdentityProfile, LanguageItem, LinksProfile,
    LocationProfile, PreferencesProfile, ProjectItem, SkillGroups,
)
from app.candidate.documents import import_document
from app.models import CandidateFact, CandidateProfile


def extraction(email="a.person@example.test"):
    profile = CandidateProfilePayload(
        identity=IdentityProfile(
            full_name="Candidate", first_name="Candidate", last_name=None, date_of_birth=None,
        ),
        contact=ContactProfile(email=email, phone="+48 123 456 789"),
        location=LocationProfile(city=None, region=None, country=None),
        professional_summary="Python and systems candidate.",
        employment=[ExperienceItem(
            company="Example", title="System administrator", location=None,
            start_date="2025", end_date="2026", description="Maintained servers.",
            responsibilities=["Maintained servers"], technologies=["Linux"],
        )],
        education=[EducationItem(
            institution="University", degree=None, specialization=None, start_date=None,
            end_date=None, completed=None, description=None,
        )],
        projects=[ProjectItem(
            name="AiChatBot", description="Built a chatbot.", technologies=["Python"],
            role="Developer", achievements=["Implemented the service"], url=None,
        )],
        skills=["Python", "Linux"],
        skill_groups=SkillGroups(
            technical=["Python"], tools=["Git"], technologies=["Linux"], soft=[],
        ),
        languages=[LanguageItem(
            language="English", level="B2", proficiency="Upper intermediate",
        )],
        certifications=[],
        links=LinksProfile(
            linkedin=None, github="https://github.com/example-user", portfolio=None, other=[],
        ),
        preferences=PreferencesProfile(
            availability=None, salary_expectations=None, preferred_locations=[],
            remote_preference=None, relocation=None, work_authorization=None, notice_period=None,
        ),
    )
    return CandidateProfileExtraction(profile=profile, facts=[
        ExtractedFact(
            path="contact.email", source="CV_EXPLICIT", confidence=0.99,
            evidence="email line",
        ),
        ExtractedFact(
            path="employment", source="CV_EXPLICIT", confidence=0.98,
            evidence="employment section",
        ),
        ExtractedFact(
            path="projects", source="CV_EXPLICIT", confidence=0.98,
            evidence="projects section",
        ),
        ExtractedFact(
            path="languages.0.level", source="CV_EXPLICIT", confidence=0.98,
            evidence="English B2",
        ),
    ])


class Extractor:
    name = "test-ai"
    model = "test-model"
    last_request = {}

    async def extract_candidate_profile(self, _text):
        return extraction()


def provider_factory(**_kwargs):
    return Extractor()


@pytest.mark.asyncio
async def test_txt_cv_import_versions_and_extracts_provenanced_profile(db, tmp_path):
    db.autoflush = False
    profile = CandidateProfile(name="Candidate", data={"contact": {}, "links": {}})
    db.add(profile); db.commit()
    content = b"Candidate\na.person@example.test\n+48 123 456 789\nhttps://github.com/example-user"
    first = await import_document(
        db, profile, "resume.txt", BytesIO(content), tmp_path, provider_factory,
    )
    second = await import_document(
        db, profile, "resume-new.txt", BytesIO(content + b"\nUpdated"),
        tmp_path, provider_factory,
    )
    facts = {fact.path: fact for fact in db.query(CandidateFact).all()}
    assert first.version == 1 and not first.active
    assert second.version == 2 and second.active
    assert facts["contact.email"].value == "a.person@example.test"
    assert facts["contact.email"].source == "CV_EXPLICIT"
    assert profile.data["links"]["github"] == "https://github.com/example-user"
    assert profile.data["employment"][0]["title"] == "System administrator"
    assert profile.data["projects"][0]["name"] == "AiChatBot"


@pytest.mark.asyncio
async def test_ai_cv_refresh_replaces_machine_fact_but_preserves_user_fact(db, tmp_path):
    profile = CandidateProfile(
        name="Candidate", data={"languages": [{"language": "English", "level": "B2"}]},
    )
    db.add(profile); db.flush()
    stale = CandidateFact(
        profile_id=profile.id, path="languages.0.level", value="C2",
        concept="languages_0_level", source="CV_DERIVED", confidence=0.5,
        user_confirmed=False,
    )
    protected = CandidateFact(
        profile_id=profile.id, path="contact.email", value="user@example.test",
        concept="contact_email", source="USER_ENTERED", confidence=1.0,
        user_confirmed=False,
    )
    db.add_all([stale, protected]); db.commit()
    await import_document(
        db, profile, "resume.txt", BytesIO(b"English B2"), tmp_path, provider_factory,
    )
    db.refresh(stale); db.refresh(protected)
    assert stale.value == "B2" and stale.source == "CV_EXPLICIT"
    assert protected.value == "user@example.test" and protected.source == "USER_ENTERED"


@pytest.mark.asyncio
async def test_malformed_pdf_returns_a_precise_import_error(db, tmp_path):
    profile = CandidateProfile(name="Candidate", data={})
    db.add(profile); db.commit()
    with pytest.raises(ValueError, match="PDF file is invalid or unreadable"):
        await import_document(
            db, profile, "resume.pdf", BytesIO(b"not-a-pdf"), tmp_path, provider_factory,
        )
