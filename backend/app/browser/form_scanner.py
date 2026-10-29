import logging
from typing import Any, List, Tuple

from playwright.async_api import Frame, Page

from app.forms.semantic import LocatorSpec, SemanticRegistry, extract_semantic_html
from app.logging import event
from app.schemas.api import SemanticField, SemanticForm, SemanticOption
from app.utils.hashing import stable_hash


class BrowserFormScanner:
    logger = logging.getLogger(__name__)

    @staticmethod
    async def _actual_value(locator: Any, field: SemanticField, frame: Frame, registry: SemanticRegistry) -> Any:
        control = field.control_kind
        if control == 'CHECKBOX_GROUP':
            selected = []
            for option in field.options:
                spec = registry.option_locators[option.id]
                node = frame.locator(spec.value).first
                if await node.is_checked() if await node.get_attribute('role') != 'checkbox' else await node.get_attribute('aria-checked') == 'true':
                    selected.append(option.id)
            return selected
        if control in {'CONTENTEDITABLE', 'RICH_TEXT'}:
            return await locator.inner_text()
        if control == 'MULTI_SELECT':
            backing = registry.resolve(field.id).metadata.get('backing_selector')
            target = frame.locator(backing) if backing else locator
            return await target.evaluate('el => Array.from(el.selectedOptions || []).map(o => o.value)')
        if control in {"CHECKBOX", "CONSENT", "SWITCH"}:
            role = await locator.get_attribute("role")
            if role in {"checkbox", "switch"}:
                return (
                    str(await locator.get_attribute("aria-checked") or "").casefold() == "true"
                    or str(await locator.get_attribute("data-state") or "").casefold() == "checked"
                )
            return await locator.is_checked()
        if control in {"RADIO_GROUP", "RATING"}:
            for option in field.options:
                option_spec = registry.option_locators.get(option.id)
                if option_spec is None:
                    continue
                option_locator = frame.locator(option_spec.value).first
                checked = (
                    str(await option_locator.get_attribute("aria-checked") or "").casefold() == "true"
                    or str(await option_locator.get_attribute("data-state") or "").casefold() == "checked"
                )
                if not checked:
                    try:
                        checked = await option_locator.is_checked()
                    except Exception:
                        checked = False
                if checked:
                    return option.label
            return None
        if control in {"CUSTOM_SELECT", "COMBOBOX", "AUTOCOMPLETE"}:
            from app.browser.control_state import display_value
            return await display_value(locator)
        if field.type != "file":
            return await locator.input_value()
        return await locator.evaluate('el => Array.from(el.files || []).map(file => file.name)')

    async def scan(self, page: Page) -> Tuple[SemanticForm, SemanticRegistry]:
        from app.browser.manager import BrowserSessionLostError
        if page.is_closed():
            raise BrowserSessionLostError('form_target_closed')
        registry = SemanticRegistry()
        fields: List[SemanticField] = []
        actions = []
        errors = []
        frames = [page.main_frame] + [frame for frame in page.frames if frame is not page.main_frame]
        for frame_index, frame in enumerate(frames):
            if any(marker in frame.url.casefold() for marker in ('recaptcha', 'hcaptcha', 'challenges.cloudflare.com')):
                continue
            frame_key = "main" if frame is page.main_frame else "frame:%d:%s" % (frame_index, frame.url)
            try:
                if frame is not page.main_frame and await page.locator('[aria-modal=true]:visible,dialog[open]:visible').count():
                    owner = await frame.frame_element()
                    if not await owner.evaluate("e => !!e.closest('[aria-modal=true],dialog[open]')"):
                        continue
                await frame.evaluate("""() => {
                  for (const validator of Array.from(window.Page_Validators || [])) {
                    if (validator.enabled === false || !validator.controltovalidate) continue;
                    if (/required/i.test(String(validator.evaluationfunction && validator.evaluationfunction.name || ''))) {
                      const target = document.getElementById(validator.controltovalidate);
                      if (target) target.setAttribute('data-jh-required','true');
                    }
                  }
                  const visible = el => {const r=el.getBoundingClientRect();return r.width>0 && r.height>0 && getComputedStyle(el).visibility!=='hidden'};
                  for (const [index,input] of Array.from(document.querySelectorAll('input[type=tel]')).entries()) {
                    if (!visible(input)) continue;
                    let owner=input.parentElement;
                    for (let depth=0; owner && owner.tagName!=='FORM' && depth<7; depth++,owner=owner.parentElement) {
                      const phones=Array.from(owner.querySelectorAll('input[type=tel]')).filter(visible);
                      const triggers=Array.from(owner.querySelectorAll('button,[role=combobox],.iti__selected-country,.selected-flag')).filter(visible);
                      if(phones.length!==1 || triggers.length!==1) continue;
                      const trigger=triggers[0]; trigger.setAttribute('data-jh-phone-trigger',String(index));
                      trigger.setAttribute('data-jh-component-part','true');
                      input.setAttribute('data-jh-country-ref',String(index));
                      const backing=Array.from(owner.querySelectorAll('input')).find(el=>el!==input&&!visible(el)
                        && (el.type==='tel' || (el.type==='hidden' && /phone|tel/i.test(el.name || el.id)))
                        && !/country|dial|code/i.test(el.name || el.id));
                      if(backing){backing.setAttribute('data-jh-phone-backing',String(index));input.setAttribute('data-jh-canonical-ref',String(index));break;}
                    }
                  }
                }""")
                html = await frame.evaluate("""() => {
                  const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden'};
                  const dialogs=Array.from(document.querySelectorAll('[aria-modal=true],dialog[open]')).filter(visible);
                  return (dialogs[dialogs.length-1] || document.documentElement).outerHTML;
                }""")
            except Exception:
                if page.is_closed() or frame is page.main_frame:
                    raise BrowserSessionLostError('form_target_unavailable')
                continue
            local_form, local_registry = extract_semantic_html(html, page.url, frame_key)
            field_map = {}
            for field in local_form.fields:
                new_id = "f%d" % (len(fields) + 1)
                field_map[field.id] = new_id
                spec = local_registry.resolve(field.id)
                try:
                    locator = frame.locator(spec.value).first
                    # ATS upload controls commonly hide the native input behind a visible
                    # button. Playwright can still set that file input directly and verify it.
                    if spec.metadata.get("field_type") != "file" and not await locator.is_visible():
                        continue
                except Exception:
                    continue
                registry.register(new_id, LocatorSpec(spec.strategy, spec.value, frame_key, spec.confidence, spec.metadata))
                options = []
                for option_index, option in enumerate(field.options):
                    new_option_id = "%so%d" % (new_id, option_index + 1)
                    option_ref = local_registry.options.get(option.id)
                    option_value = option_ref[1] if option_ref else option.label
                    local_option_locator = local_registry.option_locators.get(option.id)
                    option_locator = None
                    if local_option_locator is not None:
                        option_locator = LocatorSpec(
                            local_option_locator.strategy, local_option_locator.value, frame_key,
                            local_option_locator.confidence, local_option_locator.metadata,
                        )
                    registry.register_option(new_option_id, new_id, option_value, option_locator)
                    options.append(SemanticOption(
                        id=new_option_id, label=option.label,
                        selected=option.selected, disabled=option.disabled,
                    ))
                actual_value = field.value
                try:
                    locator = frame.locator(spec.value).first
                    local_with_renamed_options = field.model_copy(update={"id": new_id, "options": options})
                    actual_value = await self._actual_value(locator, local_with_renamed_options, frame, registry)
                except Exception:
                    pass
                selected_options = [item.id for item in options if item.selected]
                scanned = field.model_copy(update={
                    "id": new_id, "options": options, "frame": frame_key,
                    "value": actual_value, "selected_options": selected_options,
                })
                fields.append(scanned)
                event(
                    self.logger, "form.field.discovered", field_id=new_id,
                    control_kind=scanned.control_kind, required=scanned.required,
                    frame=frame_key,
                )
                event(
                    self.logger, "form.control_classified", field_id=new_id,
                    control_kind=scanned.control_kind,
                    question_category=scanned.question_category,
                )
                if scanned.semantic_group_id:
                    event(self.logger, 'control.grouped', field_id=new_id, group_id=scanned.semantic_group_id)
            for action in local_form.actions:
                new_id = "a%d" % (len(actions) + 1)
                spec = local_registry.resolve(action["id"])
                try:
                    if not await frame.locator(spec.value).first.is_visible():
                        continue
                except Exception:
                    continue
                registry.register(new_id, LocatorSpec(spec.strategy, spec.value, frame_key, spec.confidence, spec.metadata))
                actions.append(dict(action, id=new_id, frame=frame_key))
            errors.extend(local_form.validation_errors)
        compact = {"fields": [field.model_dump() for field in fields], "actions": actions, "errors": errors}
        return SemanticForm(
            url=page.url, fields=fields, actions=actions, validation_errors=errors,
            fingerprint=stable_hash(compact),
        ), registry

    @staticmethod
    def frame_for(page: Page, frame_key: str) -> Frame:
        if frame_key == "main":
            return page.main_frame
        parts = frame_key.split(":", 2)
        if len(parts) > 1 and parts[1].isdigit():
            index = int(parts[1])
            frames = [page.main_frame] + [frame for frame in page.frames if frame is not page.main_frame]
            if index < len(frames):
                return frames[index]
        raise RuntimeError("Form frame is no longer available")
