from app.applications.orchestrator import _browser_value_matches_plan
from app.forms.answer_engine import ProposedAnswer
from app.schemas.api import SemanticField, SemanticOption


def test_untrusted_prefill_does_not_match_grounded_location_plan():
    field = SemanticField(
        id="country", question="Location country", type="single_choice",
        required=True, value="Hungary",
        options=[
            SemanticOption(id="pl", label="Poland"),
            SemanticOption(id="hu", label="Hungary"),
        ],
    )
    answer = ProposedAnswer(
        field_id="country", question="Location country", value="pl",
        source="USER_ENTERED_MAPPED", confidence=1.0,
        source_paths=["location.country"], action="select_option",
    )
    assert not _browser_value_matches_plan(field, answer)


def test_matching_prefill_can_be_verified_against_actual_option():
    field = SemanticField(
        id="city", question="Location city", type="single_choice",
        required=True, value="Wrocław, Dolnośląskie",
        options=[SemanticOption(id="wroclaw", label="Wrocław, Dolnośląskie")],
    )
    answer = ProposedAnswer(
        field_id="city", question="Location city", value="wroclaw",
        source="USER_ENTERED_MAPPED", confidence=1.0,
        source_paths=["location.city"], action="select_option",
    )
    assert _browser_value_matches_plan(field, answer)
