from datetime import datetime

import pytest

from app.ai.contracts import FormPlan, PlannedFormField
from app.ai.provider import AIProviderError
from app.forms.answer_engine import AnswerEngine
from app.models import CandidateFact, CandidateProfile, Job, QuestionMemory
from app.schemas.api import SemanticField, SemanticOption


def job():
    return Job(
        source="pracuj", source_job_id="42", canonical_url="https://example.test",
        fingerprint="f" * 64, content_hash="c" * 64, title="IT Intern",
        company="Example", location="Wrocław", description_normalized="support Python systems",
        requirements=["English"], nice_to_have=[], responsibilities=["Support systems"],
        compact_json={}, raw_metadata={},
    )


def decision(field_id, answer=None, *, action="fill", paths=None, option_id=None,
             needs_user=False, fact_path=None, meaning="candidate fact", category="FACTUAL"):
    return PlannedFormField(
        field_id=field_id, meaning=meaning, action=action, semantic_answer=answer,
        selected_option_id=option_id, source_paths=paths or [], confidence=0.96,
        execution_confidence="EXACT" if not needs_user else "UNKNOWN",
        needs_user=needs_user,
        user_question="Please provide this information" if needs_user else None,
        requested_fact_path=fact_path,
        reason=("Mapped semantically from the candidate profile" if not needs_user
                else "The candidate profile does not contain this personal fact"),
        question_category=category,
    )


class PlanProvider:
    name = "test-ai"
    model = "test-model"

    def __init__(self, planner):
        self.planner = planner
        self.contexts = []
        self.last_request = {}

    async def plan_form(self, context):
        self.contexts.append(context)
        return FormPlan(fields=self.planner(context))


def make_engine(db, planner):
    provider = PlanProvider(planner)
    return AnswerEngine(db, provider_factory=lambda **_kwargs: provider), provider


@pytest.mark.asyncio
async def test_language_multiselect_uses_names_not_proficiency_and_rejects_foreign_options(db):
    candidate = CandidateProfile(name='Test', data={'languages': [{'language': 'English', 'level': 'B2'}]})
    vacancy = job(); db.add_all([candidate, vacancy]); db.flush()
    add_fact(db, candidate, 'languages.0.language', 'English'); db.commit()
    planned = decision('f1', 'English', action='select_option', paths=['languages.0.language'])
    planned.selected_option_ids = ['f1o1']
    engine, _ = make_engine(db, lambda _: [planned])
    field = SemanticField(id='f1', question='Known languages', type='multi_select', control_kind='MULTI_SELECT',
                          options=[SemanticOption(id='f1o1', label='English'), SemanticOption(id='f1o2', label='Polish')])
    answer = (await engine.plan(candidate, vacancy, [field]))[0]
    assert answer.value == ['f1o1'] and not answer.needs_user
    planned.selected_option_ids = ['unobserved-option']
    answer = (await engine.plan(candidate, vacancy, [field]))[0]
    assert answer.needs_user


def add_fact(db, candidate, path, value, source="CV_EXPLICIT"):
    db.add(CandidateFact(
        profile_id=candidate.id, path=path, value=value,
        concept=path.replace(".", "_"), source=source, confidence=0.98,
        user_confirmed=source == "USER_CONFIRMED",
    ))


@pytest.mark.asyncio
async def test_polish_proficiency_question_uses_b2_not_language_name(db):
    candidate = CandidateProfile(name="A", data={"languages": [{"language": "English", "level": "B2"}]})
    vacancy = job(); db.add_all([candidate, vacancy]); db.flush()
    add_fact(db, candidate, "languages.0.level", "B2")
    db.commit()
    planner = lambda _ctx: [decision(
        "f1", "B2", paths=["candidate.languages.0.level"],
        meaning="English proficiency level",
    )]
    answer_engine, _ = make_engine(db, planner)
    answer = await answer_engine.answer(candidate, vacancy, SemanticField(
        id="f1", question="W jakim stopniu znasz język angielski?",
        type="text", required=True,
    ))
    assert answer.value == "B2"
    assert answer.source_paths == ["languages.0.level"]


@pytest.mark.asyncio
async def test_named_language_fact_request_resolves_to_persisted_b2(db):
    candidate = CandidateProfile(name="A", data={"languages": [{"language": "English", "level": "B2"}]})
    vacancy = job(); db.add_all([candidate, vacancy]); db.flush()
    add_fact(db, candidate, "languages", [{"language": "English", "level": "B2"}], "USER_ENTERED")
    add_fact(db, candidate, "languages.0", {"language": "English", "level": "C2"}, "CV_EXPLICIT")
    db.commit()
    planner = lambda _ctx: [decision(
        "f1", action="needs_user", needs_user=True,
        fact_path="languages.English.level", meaning="English proficiency",
    )]
    answer_engine, _ = make_engine(db, planner)
    answer = await answer_engine.answer(candidate, vacancy, SemanticField(
        id="f1", question="English proficiency", type="text", required=True,
    ))
    assert answer.value == "B2"
    assert answer.source == "USER_ENTERED"
    assert not answer.needs_user


@pytest.mark.asyncio
async def test_language_question_can_include_language_and_level(db):
    candidate = CandidateProfile(name="A", data={"languages": [{"language": "English", "level": "B2"}]})
    vacancy = job(); db.add_all([candidate, vacancy]); db.flush()
    add_fact(db, candidate, "languages.0", {"language": "English", "level": "B2"})
    db.commit()
    planner = lambda _ctx: [decision(
        "f1", "English — B2", paths=["languages.0"],
        meaning="spoken languages and proficiency",
    )]
    answer_engine, _ = make_engine(db, planner)
    answer = await answer_engine.answer(candidate, vacancy, SemanticField(
        id="f1", question="What languages do you speak?", type="text", required=True,
    ))
    assert answer.value == "English — B2"


@pytest.mark.asyncio
async def test_location_maps_accent_insensitively_to_actual_option_id(db):
    candidate = CandidateProfile(name="A", data={"location": {"city": "Wroclaw"}})
    vacancy = job(); db.add_all([candidate, vacancy]); db.flush()
    add_fact(db, candidate, "location.city", "Wroclaw", "USER_ENTERED")
    db.commit()
    planner = lambda _ctx: [decision(
        "f1", "Wroclaw", action="select_option", paths=["location.city"], meaning="current city",
    )]
    answer_engine, _ = make_engine(db, planner)
    answer = await answer_engine.answer(candidate, vacancy, SemanticField(
        id="f1", question="Miasto", type="single_choice", required=True,
        options=[
            SemanticOption(id="city-1", label="Wrocław, Dolnośląskie"),
            SemanticOption(id="city-2", label="Warszawa"),
        ],
    ))
    assert answer.value == "city-1"
    assert answer.source == "USER_ENTERED_MAPPED"


@pytest.mark.asyncio
async def test_confirmed_notice_period_maps_to_real_availability_option(db):
    candidate = CandidateProfile(name="A", data={"preferences": {"notice_period": "14 days"}})
    vacancy = job(); db.add_all([candidate, vacancy]); db.flush()
    add_fact(db, candidate, "preferences.notice_period", "14 days", "USER_CONFIRMED")
    db.commit()
    planner = lambda _ctx: [decision(
        "f1", action="needs_user", needs_user=True,
        fact_path="preferences.notice_period", meaning="availability",
    )]
    answer_engine, _ = make_engine(db, planner)
    answer = await answer_engine.answer(candidate, vacancy, SemanticField(
        id="f1", question="Availability", type="single_choice", required=True,
        options=[SemanticOption(id="now", label="Immediately"), SemanticOption(id="two", label="2 weeks")],
    ))
    assert answer.value == "two"
    assert not answer.needs_user


@pytest.mark.asyncio
async def test_skill_name_alone_cannot_support_commercial_experience_yes(db):
    candidate = CandidateProfile(name="A", data={"skills": ["Data Engineering", "Python"]})
    vacancy = job(); db.add_all([candidate, vacancy]); db.flush()
    add_fact(db, candidate, "skills", candidate.data["skills"], "CV_EXPLICIT")
    db.commit()
    planner = lambda _ctx: [decision(
        "f1", "Yes", action="select_option", paths=["skills"], option_id="yes",
        meaning="commercial Data Engineering experience",
    )]
    answer_engine, _ = make_engine(db, planner)
    answer = await answer_engine.answer(candidate, vacancy, SemanticField(
        id="f1", question="Do you have at least 1.5 years of commercial experience in Data Engineering?",
        type="single_choice", required=True,
        options=[SemanticOption(id="yes", label="Yes"), SemanticOption(id="no", label="No")],
    ))
    assert answer.needs_user
    assert answer.value is None


@pytest.mark.asyncio
async def test_python_project_evidence_can_support_experience_yes(db):
    candidate = CandidateProfile(name="A", data={"projects": [{"name": "Scanner", "technologies": ["Python"]}]})
    vacancy = job(); db.add_all([candidate, vacancy]); db.flush()
    add_fact(db, candidate, "projects.0", candidate.data["projects"][0], "CV_EXPLICIT")
    db.commit()
    planner = lambda _ctx: [decision(
        "f1", "Yes", action="select_option", paths=["projects.0"], option_id="yes",
        meaning="Python coding experience",
    )]
    answer_engine, _ = make_engine(db, planner)
    answer = await answer_engine.answer(candidate, vacancy, SemanticField(
        id="f1", question="Do you have experience coding in Python?", type="single_choice", required=True,
        options=[SemanticOption(id="yes", label="Yes"), SemanticOption(id="no", label="No")],
    ))
    assert answer.value == "yes"
    assert not answer.needs_user


@pytest.mark.asyncio
async def test_required_and_optional_recruitment_consents_are_differentiated(db):
    candidate = CandidateProfile(name="A", data={})
    vacancy = job(); db.add_all([candidate, vacancy]); db.commit()
    planner = lambda _ctx: [
        decision("required", "Yes", action="check", meaning="recruitment data consent"),
        decision("future", "Yes", action="check", meaning="future recruitment consent"),
    ]
    answer_engine, _ = make_engine(db, planner)
    answers = await answer_engine.plan(candidate, vacancy, [
        SemanticField(
            id="required", question="I consent to processing my personal data for this recruitment",
            type="checkbox", required=True,
        ),
        SemanticField(
            id="future", question="Consent to processing personal data in future recruitment processes",
            type="checkbox", required=False,
        ),
    ])
    assert answers[0].needs_user
    assert answers[0].action == "needs_user"
    assert not answers[1].needs_user
    assert answers[1].action == "skip"


@pytest.mark.asyncio
async def test_required_recruitment_consent_follows_explicit_auto_accept_policy(db, monkeypatch):
    from dataclasses import replace
    import app.forms.answer_engine as answer_engine_module

    candidate = CandidateProfile(name="A", data={})
    vacancy = job(); db.add_all([candidate, vacancy]); db.commit()
    planner = lambda _ctx: [decision(
        "required", "Yes", action="check", meaning="current recruitment consent",
        category="REQUIRED_RECRUITMENT_CONSENT",
    )]
    monkeypatch.setattr(
        answer_engine_module, "settings",
        replace(answer_engine_module.settings, auto_accept_required_recruitment_consents=True),
    )
    answer_engine, _ = make_engine(db, planner)
    answer = await answer_engine.answer(candidate, vacancy, SemanticField(
        id="required", question="Consent to data processing for this recruitment",
        type="checkbox", control_kind="CONSENT",
        question_category="REQUIRED_RECRUITMENT_CONSENT", required=True,
    ))
    assert answer.needs_user is False
    assert answer.action == "check"
    assert answer.value is True
    assert answer.source == "USER_POLICY"


@pytest.mark.asyncio
async def test_subjective_rating_is_suggestion_until_user_confirms(db):
    candidate = CandidateProfile(name="A", data={"projects": [{"description": "Diagnosed network failures"}]})
    vacancy = job(); db.add_all([candidate, vacancy]); db.flush()
    add_fact(db, candidate, "projects.0", candidate.data["projects"][0], "CV_EXPLICIT")
    db.commit()
    planner = lambda _ctx: [decision(
        "rating", "4", action="select_option", paths=["projects.0"], option_id="four",
        meaning="supported troubleshooting self-assessment", category="SELF_ASSESSMENT",
    )]
    answer_engine, _ = make_engine(db, planner)
    answer = await answer_engine.answer(candidate, vacancy, SemanticField(
        id="rating", question="How much do you like independently analyzing technical problems?",
        type="single_choice", control_kind="RATING", question_category="SELF_ASSESSMENT", required=True,
        options=[SemanticOption(id=str(index), label=str(index)) for index in range(1, 4)] + [
            SemanticOption(id="four", label="4"), SemanticOption(id="five", label="5"),
        ],
    ))
    assert answer.needs_user
    assert answer.value is None
    assert answer.suggested_value == "4"
    assert answer.question_category == "SELF_ASSESSMENT"


@pytest.mark.asyncio
async def test_legal_declaration_is_never_inferred_from_cv_evidence(db):
    candidate = CandidateProfile(name="A", data={"employment": [{"company": "Example"}]})
    vacancy = job(); db.add_all([candidate, vacancy]); db.flush()
    add_fact(db, candidate, "employment.0", candidate.data["employment"][0], "CV_EXPLICIT")
    db.commit()
    planner = lambda _ctx: [decision(
        "legal", "No", action="select_option", paths=["employment.0"], option_id="no",
        meaning="Article 52 termination declaration", category="LEGAL_DECLARATION",
    )]
    answer_engine, _ = make_engine(db, planner)
    answer = await answer_engine.answer(candidate, vacancy, SemanticField(
        id="legal", question="Have you ever been dismissed under Article 52 of the Labour Code?",
        type="single_choice", control_kind="RADIO_GROUP", question_category="LEGAL_DECLARATION", required=True,
        options=[SemanticOption(id="yes", label="Yes"), SemanticOption(id="no", label="No")],
    ))
    assert answer.needs_user
    assert answer.value is None
    assert "explicit user-confirmed" in answer.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("question,fact_path", [
    ("Current city", "location.city"),
    ("Salary expectations", "salary_expectation"),
])
async def test_unknown_personal_fact_requires_user_without_guessing(db, question, fact_path):
    candidate = CandidateProfile(name="A", data={})
    vacancy = job(); db.add_all([candidate, vacancy]); db.commit()
    planner = lambda _ctx: [decision(
        "f1", action="needs_user", needs_user=True, fact_path=fact_path, meaning=question,
    )]
    answer_engine, _ = make_engine(db, planner)
    answer = await answer_engine.answer(candidate, vacancy, SemanticField(
        id="f1", question=question, type="text", required=True,
    ))
    assert answer.needs_user
    assert answer.value is None
    expected_path = (
        "preferences.salary_expectations" if fact_path == "salary_expectation" else fact_path
    )
    assert answer.requested_fact_path == expected_path


@pytest.mark.asyncio
async def test_whole_form_plan_receives_job_context_and_confirmed_memory(db):
    candidate = CandidateProfile(name="A", data={"contact": {"email": "a@example.test"}})
    vacancy = job(); db.add_all([candidate, vacancy]); db.flush()
    add_fact(db, candidate, "contact.email", "a@example.test", "USER_ENTERED")
    db.add(QuestionMemory(
        candidate_id=candidate.id, concept="work_authorization_poland",
        question_fingerprint="x" * 64, value="Yes", source="USER_CONFIRMED",
        confidence=1.0, confirmed_at=datetime.utcnow(),
    ))
    db.commit()

    def planner(ctx):
        assert ctx["job"]["title"] == "IT Intern"
        assert "support Python systems" in ctx["job"]["description"]
        assert ctx["confirmed_answers"]["work_authorization_poland"]["value"] == "Yes"
        return [
            decision("f1", "a@example.test", paths=["contact.email"], meaning="email"),
            decision(
                "f2", "Yes", action="select_option",
                paths=["memory:work_authorization_poland"], option_id="yes",
                meaning="work authorization",
            ),
        ]

    answer_engine, provider = make_engine(db, planner)
    answers = await answer_engine.plan(candidate, vacancy, [
        SemanticField(id="f1", question="Email", type="email", required=True),
        SemanticField(
            id="f2", question="Authorized in Poland?", type="single_choice", required=True,
            options=[SemanticOption(id="yes", label="Yes"), SemanticOption(id="no", label="No")],
        ),
    ])
    assert [item.value for item in answers] == ["a@example.test", "yes"]
    assert len(provider.contexts) == 1


@pytest.mark.asyncio
async def test_job_specific_free_text_is_grounded_in_project_and_vacancy(db):
    candidate = CandidateProfile(name="A", data={
        "projects": [{"name": "Network Scanner", "description": "Built a Python network scanner."}],
    })
    vacancy = job(); db.add_all([candidate, vacancy]); db.flush()
    add_fact(
        db, candidate, "projects.0",
        {"name": "Network Scanner", "description": "Built a Python network scanner."},
    )
    db.commit()

    def planner(ctx):
        project = ctx["candidate"]["projects"][0]
        assert ctx["job"]["company"] == "Example"
        assert "Python" in ctx["job"]["description"]
        return [decision(
            "f1",
            "I am interested in Example's IT Intern role because my Network Scanner project used Python for practical systems work.",
            paths=["projects.0"], meaning="job-specific motivation",
        )]

    answer_engine, _ = make_engine(db, planner)
    answer = await answer_engine.answer(candidate, vacancy, SemanticField(
        id="f1", question="Why are you interested in this role?",
        type="textarea", required=True, max_length=500,
    ))
    assert "Network Scanner" in answer.value
    assert "Example" in answer.value
    assert answer.source_paths == ["projects.0"]


@pytest.mark.asyncio
async def test_ai_failure_propagates_without_heuristic_fallback(db):
    candidate = CandidateProfile(name="A", data={"languages": [{"language": "English", "level": "B2"}]})
    vacancy = job(); db.add_all([candidate, vacancy]); db.commit()

    class FailingProvider(PlanProvider):
        async def plan_form(self, context):
            raise AIProviderError("provider_unavailable", "planner unavailable")

    provider = FailingProvider(lambda _ctx: [])
    answer_engine = AnswerEngine(db, provider_factory=lambda **_kwargs: provider)
    with pytest.raises(AIProviderError, match="planner unavailable"):
        await answer_engine.answer(candidate, vacancy, SemanticField(
            id="f1", question="W jakim stopniu znasz język angielski?",
            type="text", required=True,
        ))
