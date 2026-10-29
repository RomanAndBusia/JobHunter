import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, Tag

from app.schemas.api import SemanticField, SemanticForm, SemanticOption
from app.logging import event
from app.utils.hashing import normalized_text, stable_hash


logger = logging.getLogger(__name__)
_QUESTION_CONTAINER = re.compile(r"(?:^|[-_ ])(?:form[-_ ]?group|field[-_ ]?group|question|open[-_ ]?question)(?:$|[-_ ])", re.I)
_QUESTION_ID = re.compile(r"open.*question.*id|question.*id", re.I)


@dataclass
class LocatorSpec:
    strategy: str
    value: str
    frame: str = "main"
    confidence: float = 0.5
    metadata: Dict[str, Any] = field(default_factory=dict)


class SemanticRegistry:
    """Short-lived mapping between safe semantic IDs and browser locator recipes."""

    def __init__(self) -> None:
        self.fields: Dict[str, LocatorSpec] = {}
        self.options: Dict[str, Tuple[str, str]] = {}
        self.option_locators: Dict[str, LocatorSpec] = {}

    def register(self, semantic_id: str, locator: LocatorSpec) -> None:
        self.fields[semantic_id] = locator

    def register_option(
        self, option_id: str, field_id: str, value: str,
        locator: Optional[LocatorSpec] = None,
    ) -> None:
        self.options[option_id] = (field_id, value)
        if locator is not None:
            self.option_locators[option_id] = locator

    def resolve(self, semantic_id: str) -> LocatorSpec:
        if semantic_id not in self.fields:
            raise KeyError("Unknown semantic field: %s" % semantic_id)
        return self.fields[semantic_id]


def _label_for(element: Tag, soup: BeautifulSoup) -> Tuple[str, float, str]:
    if element.name == 'input' and element.get('type') in {'button', 'submit'} and element.get('value'):
        return str(element['value']), 0.98, 'button-value'
    role = str(element.get("role") or "").casefold()
    has_popup = str(element.get("aria-haspopup") or "").casefold()
    if element.name == "button" and role not in {"radio", "checkbox", "switch", "combobox"} and not has_popup:
        own_text = element.get_text(" ", strip=True)
        if own_text:
            return own_text, 0.98, "button-text"
    element_id = str(element.get("id") or "")
    if element_id:
        explicit = soup.find("label", attrs={"for": element_id})
        if explicit:
            return explicit.get_text(" ", strip=True), 1.0, "label[for]"
    wrapped = element.find_parent("label")
    if wrapped:
        return wrapped.get_text(" ", strip=True), 0.95, "wrapping-label"
    aria = str(element.get("aria-label") or "")
    if aria:
        return aria, 0.95, "aria-label"
    labelled_by = str(element.get("aria-labelledby") or "")
    if labelled_by:
        labels = [soup.find(id=item) for item in labelled_by.split()]
        text = " ".join(node.get_text(" ", strip=True) for node in labels if node)
        if text:
            return text, 0.95, "aria-labelledby"
    placeholder = str(element.get("placeholder") or "")
    if placeholder:
        return placeholder, 0.72, "placeholder"
    fieldset = element.find_parent("fieldset")
    if fieldset:
        legend = fieldset.find("legend")
        if legend:
            return legend.get_text(" ", strip=True), 0.85, "legend"
    owner = element.parent
    for _ in range(8):
        if not isinstance(owner, Tag) or owner.name in {'form', 'body', 'html'}:
            break
        labels = owner.find_all('label')
        if len(labels) == 1:
            return labels[0].get_text(' ', strip=True), 0.8, 'shared-container-label'
        if len(labels) > 1:
            break
        owner = owner.parent
    if str(element.get('type') or '').casefold() == 'checkbox':
        owner = element.parent
        for _ in range(6):
            if not isinstance(owner, Tag) or owner.name in {'form', 'body', 'html'}:
                break
            controls = owner.select('input:not([type="hidden"]),select,textarea,[role="checkbox"],[role="switch"],[role="radio"]')
            if len(controls) > 1:
                break
            text = owner.get_text(' ', strip=True)
            if text:
                return text[:800], 0.8, 'single-control-container-text'
            owner = owner.parent
    name = str(element.get("name") or "")
    if name:
        return name.replace("_", " ").replace("-", " "), 0.55, "name"
    parent = element.parent
    if parent:
        own = parent.get_text(" ", strip=True)
        value = str(element.get("value") or "")
        if value:
            own = own.replace(value, "")
        if own:
            return own[:240], 0.45, "nearby-text"
    return "Unlabelled field", 0.15, "none"


def _field_type(element: Tag) -> str:
    tag = element.name.casefold()
    role = str(element.get("role") or "").casefold()
    input_type = str(element.get("type") or "text").casefold()
    if tag == 'a' and element.get('onclick'):
        return 'button'
    classes = " ".join(element.get("class", [])).casefold()
    aria_haspopup = str(element.get("aria-haspopup") or "").casefold()
    if tag == 'input' and input_type in {'submit', 'button', 'reset'}:
        return 'button'
    if str(element.get("contenteditable") or "").lower() in {"true", "plaintext-only"}:
        return "rich_text" if element.find_parent(class_=re.compile(r"editor|froala|ql-container")) or "fr-element" in classes else "contenteditable"
    if role == "radio":
        return "single_choice"
    if role == "checkbox":
        return "checkbox"
    if role == "switch":
        return "toggle"
    if role == "combobox":
        return "combobox"
    if aria_haspopup in {"listbox", "dialog", "menu", "tree", "grid"}:
        return "custom_select"
    if tag == "textarea":
        return "textarea"
    if tag == "select":
        return "multi_select" if element.has_attr("multiple") else "select"
    if input_type == "file":
        return "file"
    if input_type in {"checkbox", "radio", "date", "month", "number", "range", "email", "tel"}:
        return {"tel": "phone", "radio": "single_choice", "range": "range"}.get(input_type, input_type)
    if element.get("aria-autocomplete") or "autocomplete" in classes:
        return "combobox"
    if role == "spinbutton":
        return "number"
    if tag == "button" or role == "button":
        return "button"
    return "text"


def _selector(element: Tag, index: int) -> Tuple[str, str, float]:
    strategy, selector, confidence = _base_selector(element, index)
    if element.find_parent(attrs={'aria-modal':'true'}):
        selector = '[aria-modal="true"]:visible ' + selector
    elif element.find_parent('dialog', attrs={'open':True}):
        selector = 'dialog[open]:visible ' + selector
    return strategy, selector, confidence


def _base_selector(element: Tag, index: int) -> Tuple[str, str, float]:
    if element.get("id"):
        root = element
        while isinstance(root.parent, Tag):
            root = root.parent
        prefix = element.name if len(root.find_all(id=element['id'])) > 1 else ''
        return "css", prefix + "#" + re.sub(r"([^A-Za-z0-9_-])", r"\\\1", str(element["id"])), 0.98
    if element.get("name"):
        escaped = str(element["name"]).replace('"', '\\"')
        if str(element.get('type') or '').lower() in {'checkbox', 'radio'} and element.has_attr('value'):
            option_value = str(element['value']).replace('"', '\\"')
            return 'css', '%s[name="%s"][value="%s"]' % (element.name, escaped, option_value), 0.94
        return "css", '%s[name="%s"]' % (element.name, escaped), 0.88
    parts = []
    node = element
    while isinstance(node, Tag) and node.name not in {"html", "body", "[document]"} and len(parts) < 32:
        if node.get('aria-modal') == 'true' or node.name == 'dialog':
            break
        if node.get('id'):
            parts.append('#' + re.sub(r"([^A-Za-z0-9_-])", r"\\\1", str(node['id'])))
            break
        sibling_index = len(node.find_previous_siblings(node.name)) + 1
        parts.append("%s:nth-of-type(%d)" % (node.name, sibling_index))
        if node.name == "form":
            break
        node = node.parent
    return "css", " > ".join(reversed(parts)), 0.55


def _options(element: Tag, field_id: str, registry: SemanticRegistry, soup: BeautifulSoup) -> List[SemanticOption]:
    result = []
    raw_options = element.select("option") if element.name == "select" else []
    if not raw_options and element.get("aria-controls"):
        controlled = soup.find(id=element.get("aria-controls"))
        raw_options = controlled.select('[role="option"]') if controlled else []
    for index, option in enumerate(raw_options):
        label = option.get_text(" ", strip=True)
        value = str(option.get("value") or label)
        if not label:
            continue
        option_id = "%so%d" % (field_id, index + 1)
        selected = option.has_attr("selected") or str(option.get("aria-selected") or "").casefold() == "true"
        disabled = (option.has_attr("disabled") or str(option.get("aria-disabled") or "").casefold() == "true"
                    or option.has_attr('value') and str(option.get('value')) == '')
        result.append(SemanticOption(id=option_id, label=label, selected=selected, disabled=disabled))
        option_locator = None
        if option.name != "option":
            strategy, selector, confidence = _selector(option, index)
            option_locator = LocatorSpec(strategy, selector, registry.fields[field_id].frame, confidence, {
                "role": option.get("role"), "visible_semantic_control": True,
            })
        registry.register_option(option_id, field_id, value, option_locator)
    return result


def _control_kind(kind: str, label: str = "", options: Optional[List[SemanticOption]] = None) -> str:
    mapping = {
        "text": "TEXT", "email": "EMAIL", "phone": "PHONE", "textarea": "TEXTAREA",
        "select": "NATIVE_SELECT", "multi_select": "MULTI_SELECT", "custom_select": "CUSTOM_SELECT",
        "contenteditable": "CONTENTEDITABLE", "rich_text": "RICH_TEXT",
        "combobox": "COMBOBOX", "single_choice": "RADIO_GROUP", "checkbox": "CHECKBOX",
        "toggle": "SWITCH", "file": "FILE", "date": "DATE", "month": "DATE",
        "number": "NUMBER", "range": "RATING",
    }
    control = mapping.get(kind, "UNKNOWN_INTERACTIVE")
    option_labels = [normalized_text(item.label).split(" ", 1)[0] for item in (options or [])]
    if control == "RADIO_GROUP" and len(option_labels) >= 3 and option_labels == [str(index) for index in range(1, len(option_labels) + 1)]:
        control = "RATING"
    consent_text = normalized_text(label)
    if control in {"CHECKBOX", "SWITCH"} and any(term in consent_text for term in (
        "zgod", "consent", "personal data", "data processing", "privacy", "acknowledge", "i agree",
    )):
        control = "CONSENT"
    return control


def _question_category(label: str, control_kind: str) -> str:
    # Structural hints only. The whole-form planner classifies question meaning.
    if control_kind == "CONSENT":
        text = normalized_text(label)
        if any(marker in text for marker in ("future recruitment", "future hiring", "przyszlych rekrut", "przyszłych rekrut")):
            return "OPTIONAL_FUTURE_RECRUITMENT_CONSENT"
        if any(marker in text for marker in ("marketing", "newsletter", "commercial information")):
            return "OPTIONAL_FUTURE_RECRUITMENT_CONSENT"
        if any(marker in text for marker in ("privacy policy", "privacy notice", "polityk prywat", "read and understood")):
            return "PRIVACY_ACKNOWLEDGEMENT"
        return "REQUIRED_RECRUITMENT_CONSENT"
    if control_kind == "RATING":
        return "SELF_ASSESSMENT"
    if control_kind == "CHECKBOX_GROUP" and any(marker in normalized_text(label) for marker in (
        "prefer", "preference", "work model", "working model", "tryb pracy",
    )):
        return "MULTI_SELECT_PREFERENCE"
    return "FACTUAL"


def _question_owner(element: Tag) -> Optional[Tag]:
    """Return the smallest structural region that owns this control."""
    node = element.parent
    fallback = None
    for _ in range(9):
        if not isinstance(node, Tag) or node.name in {"form", "body", "html"}:
            break
        if node.name == 'fieldset':
            return fallback if fallback is not None else node
        hidden_ids = node.select('input[type="hidden"]')
        markers = [item for item in hidden_ids if _QUESTION_ID.search(
            " ".join((str(item.get("name") or ""), str(item.get("id") or "")))
        )]
        if len(markers) == 1:
            return node
        classes = " ".join(node.get("class", []))
        if fallback is None and _QUESTION_CONTAINER.search(classes):
            fallback = node
        node = node.parent
    return fallback or element.find_parent("form")


def _owned_controls(owner: Optional[Tag], selector: str, soup: BeautifulSoup) -> List[Tag]:
    root = owner or soup
    controls = list(root.select(selector))
    if owner is None:
        return controls
    return [control for control in controls if _question_owner(control) is owner]


def _ownership_metadata(owner: Optional[Tag], index: int) -> Dict[str, Any]:
    if owner is None:
        return {"ownership": "document"}
    hidden = next((item for item in owner.select('input[type="hidden"]') if _QUESTION_ID.search(
        " ".join((str(item.get("name") or ""), str(item.get("id") or "")))
    )), None)
    return {
        "ownership": "fieldset" if owner.name == "fieldset" else "question_container",
        "ownership_selector": _selector(owner, index)[1],
        "question_identity_present": hidden is not None,
    }


def _role_radio_group(element: Tag) -> Optional[Tag]:
    group = element.find_parent(attrs={"role": "radiogroup"})
    if group is not None:
        return group
    node = element.parent
    for _ in range(5):
        if not isinstance(node, Tag):
            break
        if 2 <= len(node.select('[role="radio"]')) <= 12:
            return node
        node = node.parent
    return None


def _group_label(group: Tag, soup: BeautifulSoup) -> Tuple[str, float, str]:
    aria = str(group.get("aria-label") or "")
    if aria:
        return aria, 0.98, "group-aria-label"
    labelled_by = str(group.get("aria-labelledby") or "")
    if labelled_by:
        node = soup.find(id=labelled_by)
        if node:
            return node.get_text(" ", strip=True), 0.98, "group-aria-labelledby"
    legend = group.find("legend") if group.name == "fieldset" else None
    if legend:
        return legend.get_text(" ", strip=True), 0.98, "legend"
    label = group.select_one('label:not([for]), .question-label, .field-label, h3, h4')
    if label:
        return label.get_text(' ', strip=True), 0.9, 'question-label'
    previous = group.find_previous(["label", "p", "h1", "h2", "h3", "h4", "div"])
    if previous:
        text = previous.get_text(" ", strip=True)
        if text:
            return text[:500], 0.75, "preceding-text"
    return "Unlabelled choice", 0.2, "none"


def _required(element, soup):
    if element.has_attr('required') or element.get('aria-required') == 'true' or element.get('data-jh-required') == 'true':
        return True
    owner = _question_owner(element)
    node = element.parent
    while isinstance(node, Tag) and node.name not in {'form', 'body', 'html'}:
        if node.has_attr('required') or node.get('data-jh-required') == 'true' or node.get('aria-required') == 'true':
            return True
        if node is owner:
            break
        node = node.parent
    target = element.get('id')
    if target and any(node.get('controltovalidate') == target and node.get('enabled', 'true') != 'false'
                      and ('required' in str(node.get('evaluationfunction', '')).lower()
                           or node.get('data-jh-required') == 'true')
                      for node in soup.select('[controltovalidate]')):
        return True
    label = soup.find('label', attrs={'for': target}) if target else None
    return bool(label and (label.select_one('.required, .required-field, [aria-label=required]')
                          or label.get_text(strip=True).endswith('*')))


def extract_semantic_html(html: str, url: str = "", frame: str = "main") -> Tuple[SemanticForm, SemanticRegistry]:
    soup = BeautifulSoup(html, "html.parser")
    registry = SemanticRegistry()
    fields = []
    actions = []
    elements = soup.select(
        "input:not([type=hidden]), textarea, select, button, [role=combobox], [role=switch], "
        "[role=radio], [role=checkbox], a[role=button], a[onclick], [aria-haspopup], [contenteditable=true], [contenteditable=plaintext-only]"
    )
    replacements = {}
    backing = {}
    skipped = set()
    for native in soup.select('select'):
        widget = native.find_next_sibling(class_=re.compile(r'selectize-control|choices|select2'))
        if widget:
            visible = widget.select_one('input:not([type=hidden]), [role=combobox]')
            if visible:
                replacements[id(native)] = visible
                skipped.add(id(visible))
                backing[id(native)] = native
    for editor in soup.select('[contenteditable=true], [contenteditable=plaintext-only]'):
        container = editor.find_parent(class_=re.compile(r'form-group|field|editor|fr-box|ql-container'))
        hidden = container.select_one('textarea') if container else None
        if hidden is None and container:
            hidden = container.find_previous_sibling('textarea') or container.find_next_sibling('textarea')
        if hidden:
            skipped.add(id(hidden))
            backing[id(editor)] = hidden
    seen_radio_names = set()
    seen_role_groups = set()
    seen_checkbox_groups = set()
    claimed_controls = set()
    for index, element in enumerate(elements):
        if element.get('data-jh-component-part') == 'true':
            continue
        if soup.select_one('form input, form textarea, form select') and not element.find_parent('form'):
            continue
        if id(element) in skipped:
            continue
        if id(element) in claimed_controls:
            continue
        if element.has_attr("disabled") or (str(element.get("aria-hidden") or "").casefold() == "true" and id(element) not in replacements):
            continue
        kind = _field_type(element)
        checkbox_owner = _question_owner(element) if kind == 'checkbox' else None
        checkbox_nodes = _owned_controls(
            checkbox_owner, 'input[type=checkbox], [role=checkbox]', soup,
        ) if checkbox_owner else []
        checkbox_names = {str(node.get('name') or '') for node in checkbox_nodes}
        same_named_checkbox_group = len(checkbox_names) == 1 and '' not in checkbox_names
        generated_names = {re.sub(r'[$_]\d+$', '', name) for name in checkbox_names}
        generated_checkbox_group = len(generated_names) == 1 and '' not in generated_names
        factual_group = all(_control_kind('checkbox', _label_for(node, soup)[0]) != 'CONSENT' for node in checkbox_nodes)
        if (checkbox_owner and checkbox_owner.name != 'form' and len(checkbox_nodes) > 1
                and (same_named_checkbox_group or generated_checkbox_group) and factual_group
                and not checkbox_owner.select('fieldset')):
            if id(checkbox_owner) in seen_checkbox_groups:
                continue
            seen_checkbox_groups.add(id(checkbox_owner))
            claimed_controls.update(id(node) for node in checkbox_nodes)
            semantic_id = 'f%d' % (len(fields) + 1)
            group_selector = _selector(checkbox_owner, index)[1]
            registry.register(semantic_id, LocatorSpec('css', group_selector, frame, 0.95, {'field_type': 'multi_select'}))
            options = []
            for option_index, checkbox in enumerate(checkbox_nodes):
                option_label = _label_for(checkbox, soup)[0]
                option_id = '%so%d' % (semantic_id, option_index + 1)
                selected = checkbox.has_attr('checked') or checkbox.get('aria-checked') == 'true'
                options.append(SemanticOption(id=option_id, label=option_label, selected=selected, disabled=checkbox.has_attr('disabled')))
                registry.register_option(option_id, semantic_id, str(checkbox.get('value') or option_label),
                                         LocatorSpec('css', _selector(checkbox, option_index)[1], frame, 0.95))
            group_label = _group_label(checkbox_owner, soup)[0]
            fields.append(SemanticField(id=semantic_id, question=group_label,
                type='multi_select', control_kind='CHECKBOX_GROUP', options=options,
                semantic_group_id=stable_hash(group_selector)[:12], option_discovery_state='KNOWN',
                required=(_required(checkbox_owner, soup) or any(_required(node, soup) for node in checkbox_nodes)),
                question_category=_question_category(group_label, 'CHECKBOX_GROUP'),
                metadata={
                    'group_context': checkbox_owner.get_text(' ', strip=True)[:1200],
                    'group_name': sorted(generated_names if generated_checkbox_group else checkbox_names)[0],
                    **_ownership_metadata(checkbox_owner, index),
                }, frame=frame))
            event(logger, 'form.field_boundary_detected', field_id=semantic_id,
                  ownership=_ownership_metadata(checkbox_owner, index)['ownership'])
            event(logger, 'form.control_grouped', field_id=semantic_id,
                  control_kind='CHECKBOX_GROUP', option_count=len(options))
            continue
        if str(element.get("type") or "").casefold() == "radio" and element.find_parent(attrs={"role": "radio"}):
            continue
        if str(element.get("role") or "").casefold() == "radio":
            group = _role_radio_group(element)
            group_key = id(group) if group is not None else id(element.parent)
            if group_key in seen_role_groups:
                continue
            seen_role_groups.add(group_key)
            owner = group or element.parent
            radios = owner.select('[role="radio"]') if isinstance(owner, Tag) else [element]
            claimed_controls.update(id(node) for node in radios)
            semantic_id = "f" + str(len(fields) + 1)
            label, confidence, label_strategy = _group_label(owner, soup) if isinstance(owner, Tag) else _label_for(element, soup)
            strategy, selector, selector_confidence = _selector(element, index)
            locator = LocatorSpec(strategy, selector, frame, min(confidence, selector_confidence), {
                "label_strategy": label_strategy, "tag": element.name, "role": "radio",
                "field_type": "single_choice", "visible_semantic_control": True,
            })
            registry.register(semantic_id, locator)
            options = []
            selected_options = []
            for option_index, radio in enumerate(radios):
                option_label = str(radio.get("aria-label") or radio.get_text(" ", strip=True) or radio.get("value") or "")
                if not option_label:
                    continue
                option_id = "%so%d" % (semantic_id, option_index + 1)
                option_strategy, option_selector, option_confidence = _selector(radio, option_index)
                selected = str(radio.get("aria-checked") or "").casefold() == "true" or str(radio.get("data-state") or "").casefold() == "checked"
                disabled = radio.has_attr("disabled") or str(radio.get("aria-disabled") or "").casefold() == "true"
                options.append(SemanticOption(id=option_id, label=option_label, selected=selected, disabled=disabled))
                if selected:
                    selected_options.append(option_id)
                registry.register_option(
                    option_id, semantic_id, option_label,
                    LocatorSpec(option_strategy, option_selector, frame, option_confidence, {"role": "radio"}),
                )
            control = _control_kind("single_choice", label, options)
            fields.append(SemanticField(
                id=semantic_id, question=label[:500], type="single_choice", control_kind=control,
                question_category=_question_category(label, control), required=(owner.has_attr("required") if isinstance(owner, Tag) else False),
                value=(next((item.id for item in options if item.selected), None)), options=options,
                selected_options=selected_options, confidence=confidence,
                locator_strategy=label_strategy + "+" + strategy, frame=frame, role="radiogroup",
                aria={"labelledby": owner.get("aria-labelledby") if isinstance(owner, Tag) else None},
                section=(owner.find_previous(["h1", "h2", "h3"]).get_text(" ", strip=True) if isinstance(owner, Tag) and owner.find_previous(["h1", "h2", "h3"]) else None),
                metadata={
                    "group_role": "radiogroup", "source_tag": getattr(owner, "name", None),
                    **_ownership_metadata(owner if isinstance(owner, Tag) else None, index),
                },
            ))
            event(logger, 'form.field_boundary_detected', field_id=semantic_id,
                  ownership=_ownership_metadata(owner if isinstance(owner, Tag) else None, index)['ownership'])
            event(logger, 'form.control_grouped', field_id=semantic_id,
                  control_kind=control, option_count=len(options))
            continue
        radio_name = str(element.get("name") or "") if str(element.get("type") or "").casefold() == "radio" else ""
        radio_owner = _question_owner(element) if radio_name else None
        radio_key = (radio_name, id(radio_owner)) if radio_name else None
        if radio_key and radio_key in seen_radio_names:
            continue
        if radio_key:
            seen_radio_names.add(radio_key)
        semantic_id = ("a" if kind == "button" else "f") + str(len(actions) + 1 if kind == "button" else len(fields) + 1)
        label, confidence, label_strategy = _label_for(element, soup)
        if kind in {'rich_text', 'contenteditable'} and id(element) in backing:
            backing_label, backing_confidence, backing_strategy = _label_for(backing[id(element)], soup)
            if backing_confidence > confidence:
                label, confidence, label_strategy = backing_label, backing_confidence, backing_strategy
        strategy, selector, selector_confidence = _selector(element, index)
        interaction = replacements.get(id(element))
        if interaction is not None:
            strategy, selector, selector_confidence = _selector(interaction, index)
        locator = LocatorSpec(strategy, selector, frame, min(confidence, selector_confidence), {
            "label_strategy": label_strategy, "tag": element.name, "input_type": element.get("type"), "field_type": kind,
        })
        registry.register(semantic_id, locator)
        if element.has_attr('data-jh-country-ref'):
            locator.metadata['country_selector'] = '[data-jh-phone-trigger="%s"]' % element['data-jh-country-ref']
        if element.has_attr('data-jh-canonical-ref'):
            locator.metadata['canonical_selector'] = '[data-jh-phone-backing="%s"]' % element['data-jh-canonical-ref']
        if id(element) in backing:
            locator.metadata['backing_selector'] = _selector(backing[id(element)], index)[1]
        if interaction is not None:
            locator.metadata['widget'] = 'selectize'
        if kind == "button":
            text = normalized_text(label)
            action_kind = "reveal_textarea" if any(word in text for word in ("write it here", "type it here", "write here instead")) else "add_row" if any(word in text for word in ("add ", "dodaj", "another")) else "next" if any(word in text for word in ("next", "continue", "dalej")) else "submit" if any(word in text for word in ("submit", "send", "apply", "wyślij", "aplikuj")) else "button"
            actions.append({"id": semantic_id, "label": label, "type": action_kind, "locator_strategy": strategy,
                            "submits": element.name in {'input', 'button'} and bool(element.find_parent('form')) and str(element.get('type') or 'submit') == 'submit'})
            continue
        required = _required(element, soup)
        value = element.get("value")
        if element.name == "textarea":
            value = element.get_text()
        options = _options(element, semantic_id, registry, soup)
        described_by = str(element.get("aria-describedby") or "")
        described_nodes = [soup.find(id=item) for item in described_by.split()]
        help_text = " ".join(node.get_text(" ", strip=True) for node in described_nodes if node)
        heading = element.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
        parent_text = element.parent.get_text(" ", strip=True)[:800] if element.parent else ""
        metadata = {
            "accept": str(element.get("accept") or ""),
            "tag": element.name,
            "input_type": element.get("type"),
            "name": element.get("name"),
            "html_id": element.get("id"),
            "placeholder": element.get("placeholder"),
            "aria_label": element.get("aria-label"),
            "aria_describedby": described_by or None,
            "help_text": help_text or None,
            "section_heading": heading.get_text(" ", strip=True)[:300] if heading else None,
            "surrounding_text": parent_text or None,
            "autocomplete": element.get("autocomplete"),
            "pattern": element.get("pattern"),
            "min": element.get("min"),
            "max": element.get("max"),
            "page_language": (soup.html.get("lang") if soup.html else None),
        }
        group = element.find_parent(['fieldset', 'section'])
        if group is None:
            group = element.find_parent(class_=re.compile(r'form-group|field-group|repeatable|phone|iti'))
        group_id = stable_hash(_selector(group, index)[1])[:12] if group else None
        if group:
            metadata['group_context'] = group.get_text(' ', strip=True)[:1200]
            metadata['group_control_count'] = len(group.select('input:not([type=hidden]),select,textarea,[contenteditable=true]'))
        if kind == 'phone' and 'country_selector' not in locator.metadata:
            owner = element.parent
            for _ in range(3):
                if not isinstance(owner, Tag) or owner.name == 'form':
                    break
                trigger = owner.select_one('button, [role=combobox], .iti__selected-country, .selected-flag')
                if trigger is not None and len(owner.select('input[type=tel]')) == 1:
                    locator.metadata['country_selector'] = _selector(trigger, index)[1]
                    canonical = owner.select_one('input[type=hidden]')
                    if canonical:
                        locator.metadata['canonical_selector'] = _selector(canonical, index)[1]
                    metadata['composite_phone'] = True
                    break
                owner = owner.parent
        if radio_name:
            radio_selector = 'input[type="radio"][name="%s"]' % radio_name.replace('"', '\\"')
            group = _owned_controls(radio_owner, radio_selector, soup)
            if not group:
                group = [element]
            claimed_controls.update(id(node) for node in group)
            options = []
            for option_index, radio in enumerate(group):
                if str(radio.get('name') or '') != radio_name or _question_owner(radio) is not radio_owner:
                    event(logger, 'form.option_rejected_wrong_group', field_id=semantic_id,
                          input_type='radio', reason='ownership_or_name_mismatch')
                    continue
                option_label, _, _ = _label_for(radio, soup)
                option_value = str(radio.get("value") or option_label)
                option_id = "%so%d" % (semantic_id, option_index + 1)
                selected = radio.has_attr("checked")
                disabled = radio.has_attr("disabled")
                options.append(SemanticOption(id=option_id, label=option_label, selected=selected, disabled=disabled))
                option_strategy, option_selector, option_confidence = _selector(radio, option_index)
                registry.register_option(
                    option_id, semantic_id, option_value,
                    LocatorSpec(option_strategy, option_selector, frame, option_confidence, {
                        "input_type": "radio", "hidden_backing_control": False,
                    }),
                )
                event(logger, 'form.option_attached', field_id=semantic_id,
                      input_type='radio', option_index=option_index)
            if radio_owner is not None and radio_owner.name != 'form':
                label = _group_label(radio_owner, soup)[0]
                confidence = 0.95
                label_strategy = "legend"
            required = required or any(_required(node, soup) for node in group)
            metadata["group_name"] = radio_name
            metadata.update(_ownership_metadata(radio_owner, index))
            registry.fields[semantic_id].metadata["group_name"] = radio_name
            registry.fields[semantic_id].metadata.update(_ownership_metadata(radio_owner, index))
        fieldset = element.find_parent("fieldset")
        if fieldset:
            metadata["group"] = stable_hash(fieldset.get_text(" ", strip=True)[:500])[:12]
        control = _control_kind(kind, label, options)
        if interaction is not None:
            control = 'MULTI_SELECT' if element.has_attr('multiple') else 'CUSTOM_SELECT'
            metadata['custom_widget'] = True
        selected_options = [item.id for item in options if item.selected]
        validation_node = None
        if str(element.get("aria-invalid") or "").casefold() == "true":
            validation_node = next((node for node in described_nodes if node and node.get_text(" ", strip=True)), None)
        fields.append(SemanticField(
            id=semantic_id, question=label[:500], type=kind, required=required,
            control_kind=control, question_category=_question_category(label, control),
            semantic_group_id=group_id,
            option_discovery_state=('KNOWN' if options else 'LAZY' if control in {'CUSTOM_SELECT','MULTI_SELECT','COMBOBOX','AUTOCOMPLETE'} else 'NOT_REQUIRED'),
            verification_strategy=('RENDERED_AND_BACKING' if id(element) in backing else 'VALUE'),
            description=help_text or None, value=value, options=options,
            selected_options=selected_options, confidence=confidence,
            locator_strategy=label_strategy + "+" + strategy, frame=frame,
            max_length=int(element.get("maxlength")) if str(element.get("maxlength") or "").isdigit() else None,
            role=str(element.get("role") or "") or None,
            aria={
                "label": element.get("aria-label"), "labelledby": element.get("aria-labelledby"),
                "describedby": element.get("aria-describedby"), "haspopup": element.get("aria-haspopup"),
                "expanded": element.get("aria-expanded"), "selected": element.get("aria-selected"),
                "checked": element.get("aria-checked"), "controls": element.get("aria-controls"),
            },
            validation_message=(validation_node.get_text(" ", strip=True)[:500] if validation_node else None),
            disabled=element.has_attr("disabled") or str(element.get("aria-disabled") or "").casefold() == "true",
            visible=str(element.get("aria-hidden") or "").casefold() != "true",
            section=heading.get_text(" ", strip=True)[:300] if heading else None,
            metadata=metadata,
        ))
        event(logger, 'form.field_boundary_detected', field_id=semantic_id,
              ownership=metadata.get('ownership', 'single_control'))
        if radio_name:
            event(logger, 'form.control_grouped', field_id=semantic_id,
                  control_kind=control, option_count=len(options))
    # Identify a collection by its UI contract, independent of ATS host/IDs.
    for action in actions:
        if action['type'] != 'add_row' or action.get('submits'):
            continue
        if not re.search(r'language|język|jezyk', action['label'], re.I):
            continue
        button = soup.select_one(registry.resolve(action['id']).value)
        owner = button.parent if button else None
        for _ in range(6):
            if owner is None or owner.name in {'form', 'body', 'html'}:
                break
            selects = owner.select('select:not([multiple])')
            if len(selects) == 2:
                members = [f for f in fields if f.metadata.get('html_id') in {s.get('id') for s in selects}]
                levels = [f for f in members if re.search(r'level|poziom', f.question, re.I)
                          or any(re.fullmatch(r'[ABC][12]', o.label.strip(), re.I) for o in f.options)]
                if len(members) == 2 and len(levels) == 1:
                    level = levels[0]
                    language = next(f for f in members if f is not level)
                    collection_id = stable_hash(_selector(owner, 0)[1])[:16]
                    for f, role in ((language, 'language'), (level, 'level')):
                        if any(_required(hidden, soup) for hidden in owner.select('input[type=hidden]')):
                            f.required = True
                        f.metadata.update(compound_id=collection_id, compound_role=role)
                        registry.resolve(f.id).metadata['collection'] = {
                            'owner': _selector(owner, 0)[1], 'add': registry.resolve(action['id']).value,
                            'language': registry.resolve(language.id).value, 'level': registry.resolve(level.id).value,
                        }
                break
            owner = owner.parent
    error_nodes = soup.select('[aria-invalid="true"], [role="alert"], .error, .field-error')
    errors = [{"text": node.get_text(" ", strip=True)[:500]} for node in error_nodes if not node.has_attr("hidden") and node.get_text(" ", strip=True)]
    compact = {"fields": [field.model_dump() for field in fields], "actions": actions, "errors": errors}
    form = SemanticForm(
        url=url, fields=fields, actions=actions, validation_errors=errors,
        fingerprint=stable_hash(compact),
    )
    return form, registry
