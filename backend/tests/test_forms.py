from pathlib import Path

import pytest

from app.forms.repeatable import RepeatableGroupExecutor
from app.forms.sanitize import sanitize_html
from app.forms.semantic import extract_semantic_html


FIXTURES = Path(__file__).parent / "fixtures"


def read(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_semantic_extraction_uses_labels_roles_options_and_actions():
    form, registry = extract_semantic_html(read("custom_react_select.html"))
    assert form.fields[0].question == "English proficiency"
    assert form.fields[0].type == "combobox"
    assert [option.label for option in form.fields[0].options] == ["A2", "B1", "B2", "C1"]
    assert registry.resolve("f1").confidence > 0.8


def test_all_difficult_fixtures_produce_compact_semantics():
    expected = {
        "autocomplete_city.html": "combobox",
        "repeated_languages.html": "text",
        "conditional_fields.html": "single_choice",
        "multi_step.html": "email",
        "validation_recovery.html": "text",
        "dynamic_history.html": "text",
    }
    for filename, field_type in expected.items():
        form, _ = extract_semantic_html(read(filename), "https://fixture.test")
        assert form.fields
        assert form.fields[0].type == field_type
        assert len(str(form.model_dump())) < 10000


def test_html_sanitizer_removes_executable_and_noisy_content():
    clean = sanitize_html("<nav>menu</nav><form><script>steal()</script><label for='x'>Email</label><input id='x' onclick='bad()'></form>")
    assert "script" not in clean and "steal" not in clean and "nav" not in clean
    assert "onclick" not in clean and "Email" in clean and "input" in clean


def test_question_boundaries_keep_radio_checkbox_preferences_and_consents_separate():
    form, registry = extract_semantic_html('''
      <form>
        <fieldset>
          <legend>Do you have unrestricted right to work in Poland?</legend>
          <input type="hidden" name="candidate.openQuestionAnswers.7367096.openQuestionId" value="7367096">
          <label><input type="radio" name="candidate.openQuestionAnswers.7367096.content" value="yes" required>Yes</label>
          <label><input type="radio" name="candidate.openQuestionAnswers.7367096.content" value="permit">No (work permit required)</label>
        </fieldset>
        <fieldset>
          <legend>What is your preferred work model?</legend>
          <label><input type="checkbox" name="workModel" value="office">work from the office (Wrocław)</label>
          <label><input type="checkbox" name="workModel" value="hybrid">hybrid work</label>
          <label><input type="checkbox" name="workModel" value="remote">100% remote</label>
        </fieldset>
        <fieldset><legend>Legal Agreements</legend>
          <label><input type="checkbox" name="currentConsent" required>I have read the Privacy Policy and consent to processing personal data for this recruitment</label>
          <label><input type="checkbox" name="futureConsent">I consent to future recruitment processes</label>
        </fieldset>
      </form>
    ''')
    authorization = next(field for field in form.fields if field.control_kind == 'RADIO_GROUP')
    work_model = next(field for field in form.fields if field.control_kind == 'CHECKBOX_GROUP')
    consents = [field for field in form.fields if field.control_kind == 'CONSENT']
    assert [option.label for option in authorization.options] == ['Yes', 'No (work permit required)']
    assert authorization.question == 'Do you have unrestricted right to work in Poland?'
    assert [option.label for option in work_model.options] == [
        'work from the office (Wrocław)', 'hybrid work', '100% remote',
    ]
    assert work_model.question_category == 'MULTI_SELECT_PREFERENCE'
    assert len(consents) == 2
    assert consents[0].question_category == 'PRIVACY_ACKNOWLEDGEMENT'
    assert consents[1].question_category == 'OPTIONAL_FUTURE_RECRUITMENT_CONSENT'
    option_locators = [spec.value for spec in registry.option_locators.values()]
    assert len(option_locators) == len(set(option_locators)) == 5


@pytest.mark.asyncio
async def test_repeatable_group_adds_and_verifies_three_rows():
    rows = [{}]
    values = [{"language": "Polish", "level": "Fluent"}, {"language": "English", "level": "B2"}, {"language": "German", "level": "B1"}]

    async def scan(): return list(rows)
    async def fill(index, data): rows[index] = data; return rows[index] == data
    async def add(): rows.append({}); return True

    result = await RepeatableGroupExecutor().execute(values, scan, fill, add)
    assert result.verified and result.completed_rows == 3 and rows == values
