import asyncio
import json
from datetime import datetime, timedelta

import pytest

from app.ai.codex import CodexCliProvider, CodexExecutionError, CodexExecutionService
from app.candidate.memory import SemanticMemory, canonical_concept, entails, polish_answer
from app.candidate.profile_parser import parse_cv_text
from app.forms.salary import SalaryPolicyEngine, parse_salary
from app.models import CandidateProfile
from app.schemas.api import SemanticField, SemanticOption
from app.search.planner import build_search_campaign, derive_role_families


def test_cv_profile_extracts_explicit_contacts_derived_skills_and_languages():
    profile, facts = parse_cv_text("""Ada Example
ada@example.test | +48 123 456 789
Projects
Built a FastAPI service using PostgreSQL, React, Docker and REST APIs.
Languages
English B2
Polish native
https://github.com/ada
""")
    assert profile["contact"]["email"] == "ada@example.test"
    assert {"FastAPI", "PostgreSQL", "React", "Docker", "REST"}.issubset(set(profile["skills"]))
    assert {item["language"] for item in profile["languages"]} == {"English", "Polish"}
    skill_facts = [fact for fact in facts if fact["path"].startswith("skills.")]
    assert skill_facts and all(fact["source"] == "DERIVED" for fact in skill_facts)


def test_multi_role_search_plan_is_cv_driven_and_query_deduplicated(db):
    candidate = CandidateProfile(name="Candidate", data={"skills": ["Python", "Linux", "Networking"], "career_goal": {"primary": "First IT experience"}})
    db.add(candidate); db.commit()
    families = derive_role_families(candidate.data)
    assert {"backend", "networking", "system_administration", "help_desk", "it_support", "qa"}.issubset(set(families))
    campaign = build_search_campaign(db, candidate)
    payloads = [item.normalized_query for item in db.query(__import__("app.models", fromlist=["SearchQuery"]).SearchQuery).filter_by(campaign_id=campaign.id)]
    assert len(payloads) == len(set(payloads)) and len(payloads) > 5


def test_semantic_memory_cross_language_ask_once_conflict_and_expiry(db):
    candidate = CandidateProfile(name="Candidate", data={})
    db.add(candidate); db.commit()
    memory = SemanticMemory(db)
    stored = memory.remember(candidate.id, "Are you at least 18 years old?", True)
    db.commit()
    concept, resolved = memory.resolve(candidate.id, "Czy masz ukończone 18 lat?")
    assert concept == "age_at_least_18" and resolved.id == stored.id and resolved.value is True
    with pytest.raises(ValueError, match="conflicts"):
        memory.remember(candidate.id, "Are you over 18?", False)
    stored.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    assert memory.resolve(candidate.id, "Are you eighteen?")[1] is None


def test_fact_entailment_does_not_over_generalize():
    assert entails("age_at_least_21", True, "age_at_least_18")
    assert not entails("age_at_least_18", True, "age_at_least_21")
    assert canonical_concept("Are you over 18?") == "age_at_least_18"


def test_answer_polishing_keeps_raw_meaning():
    raw = "i mostly want experience and dont care much about salary"
    polished = polish_answer("What salary do you expect?", raw)
    assert "experience" in polished.casefold() and "flexible" in polished.casefold()
    assert "certification" not in polished.casefold() and "employment" not in polished.casefold()


def test_salary_policy_minimum_units_and_experience_text():
    monthly = parse_salary("6 000–9 000 PLN gross per month")
    assert monthly.amount == 6000 and monthly.currency == "PLN" and monthly.period == "month" and monthly.gross_net == "gross"
    field = SemanticField(id="salary", question="Expected monthly salary", type="select", options=[SemanticOption(id="high", label="7000 PLN"), SemanticOption(id="low", label="5000 PLN"), SemanticOption(id="mid", label="6000 PLN")])
    assert SalaryPolicyEngine().choose_option(field) == "low"
    text = SalaryPolicyEngine().answer(SemanticField(id="s", question="Salary expectations", type="textarea"), {})
    assert "experience" in text.casefold()


class FakeRunner:
    async def run(self, _prompt, _schema):
        return {"text": "Grounded answer"}


@pytest.mark.asyncio
async def test_codex_provider_uses_structured_runner(db):
    provider = CodexCliProvider(db, FakeRunner())
    assert await provider.generate_text("Motivation", {"skills": ["Python"]}, {}, 100) == "Grounded answer"


class FakeProcess:
    def __init__(self, stdout=b"not-json", hang=False):
        self.stdout = stdout
        self.hang = hang
        self.killed = False
        self.returncode = 0

    async def communicate(self):
        if self.hang and not self.killed:
            await asyncio.Event().wait()
        return self.stdout, b""

    def kill(self):
        self.killed = True


@pytest.mark.asyncio
async def test_codex_malformed_output(monkeypatch):
    service = CodexExecutionService("codex", timeout=1)
    monkeypatch.setattr(service, "executable_path", lambda: "codex")
    async def spawn(*_args, **_kwargs):
        return FakeProcess()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(CodexExecutionError, match="malformed"):
        await service.run("task", {"type": "object"})


@pytest.mark.asyncio
async def test_codex_timeout_kills_process(monkeypatch):
    service = CodexExecutionService("codex", timeout=0.01)
    process = FakeProcess(hang=True)
    monkeypatch.setattr(service, "executable_path", lambda: "codex")
    async def spawn(*_args, **_kwargs):
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(CodexExecutionError, match="timed out"):
        await service.run("task", {"type": "object"})
    assert process.killed
