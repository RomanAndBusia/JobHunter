"""Deterministic browser drivers for normalized semantic form controls.

Drivers never accept selectors from AI output.  They resolve the short-lived
locator recipes produced by :mod:`app.forms.semantic` immediately before every
interaction so React re-renders do not leave stale element handles behind.
"""

from abc import ABC, abstractmethod
from pathlib import Path
import re
from typing import Any, Iterable, Set

from app.schemas.api import SemanticField


class ControlDriver(ABC):
    kinds: Set[str] = set()

    def supports(self, field: SemanticField, action: str) -> bool:
        return field.control_kind in self.kinds

    async def inspect(self, executor: Any, field: SemanticField) -> SemanticField:
        return field

    @abstractmethod
    async def interact(self, executor: Any, field: SemanticField, action: str, value: Any) -> None:
        raise NotImplementedError

    @abstractmethod
    async def verify(self, executor: Any, field: SemanticField, action: str, value: Any) -> Any:
        raise NotImplementedError


class NativeInputDriver(ControlDriver):
    kinds = {"TEXT", "EMAIL", "PHONE", "NUMBER", "DATE", "TEXTAREA", "UNKNOWN_INTERACTIVE"}

    async def interact(self, executor: Any, field: SemanticField, action: str, value: Any) -> None:
        locator = executor.locator(field.id)
        if action == "focus":
            await locator.focus()
        elif action == "clear":
            await locator.fill("")
        elif action == "press":
            if str(value).casefold() in {'enter', 'return'}:
                from app.browser.action_safety import require_submission_allowed
                require_submission_allowed()
            await locator.press(str(value))
        elif action == "click":
            await locator.click()
        elif action in {"fill", "type"}:
            current = await locator.input_value()
            if str(current) != str(value):
                await locator.fill("")
                if action == "type":
                    await locator.press_sequentially(str(value), delay=25)
                else:
                    await locator.fill(str(value))
        else:
            raise ValueError("Unsupported input action: %s" % action)

    async def verify(self, executor: Any, field: SemanticField, action: str, value: Any) -> Any:
        locator = executor.locator(field.id)
        if action in {"focus", "press", "click"}:
            return True
        actual = await locator.input_value()
        wanted = "" if action == "clear" else str(value)
        executor.require(actual == wanted, "value mismatch")
        return actual


class TextareaDriver(NativeInputDriver):
    kinds = {"TEXTAREA"}


class FileUploadDriver(ControlDriver):
    kinds = {"FILE"}

    def document_path(self, executor, value, field=None):
        if executor.application_id is not None:
            from app.applications.cover_letters import is_cv_field, letter_field_mode, generated_path
            binding = executor.document_bindings.get(field.id) if field else None
            if binding is not None:
                executor.require(letter_field_mode(field) == 'txt_attachment', 'Generated letter is not accepted by this field')
                executor.require(Path(binding['path']).resolve() == generated_path(executor.application_id).resolve(), 'Generated letter path is not owned by this application')
                return Path(binding['path']).resolve()
            executor.require(field is not None and is_cv_field(field), 'This upload is not a CV field')
            executor.require(bool(executor.selected_document), 'No selected application document was resolved')
            return Path(executor.selected_document['path']).resolve()
        # Standalone fixture/executor callers are trusted code, not AI application plans.
        return Path(str(value)).resolve()

    async def interact(self, executor: Any, field: SemanticField, action: str, value: Any) -> None:
        path = self.document_path(executor, value, field)
        if executor.application_id is not None and field.id not in executor.document_bindings:
            from app.candidate.document_upload import resolve_upload_document
            executor.selected_document = await resolve_upload_document(executor.selected_document, field)
            path = Path(executor.selected_document['path']).resolve()
        if not path.is_file():
            raise ValueError("Upload file does not exist")
        executor.upload_states[field.id] = {
            'name': path.name,
            'owner_selector': await executor.locator(field.id).evaluate('''el => {
              let owner=el.closest('fieldset,[role=group]')||el.parentElement.parentElement;
              const labelled=owner.getAttribute('aria-labelledby');
              if(owner.getAttribute('role')==='group'&&labelled){
                const selector='[role="group"][aria-labelledby="'+CSS.escape(labelled)+'"]';
                if(document.querySelectorAll(selector).length===1)return selector;
              }
              const parts=[];
              for(let node=owner;node&&node.tagName!=='HTML';node=node.parentElement){
                if(node.id&&document.querySelectorAll('#'+CSS.escape(node.id)).length===1){parts.unshift('#'+CSS.escape(node.id));break;}
                const same=Array.from(node.parentElement.children).filter(e=>e.tagName===node.tagName);
                parts.unshift(node.tagName.toLowerCase()+':nth-of-type('+(same.indexOf(node)+1)+')');
              }
              return parts.join(' > ');
            }'''),
        }
        await executor.locator(field.id).set_input_files(str(path))

    async def verify(self, executor: Any, field: SemanticField, action: str, value: Any) -> Any:
        actual = None
        if await executor.locator(field.id).count():
            try:
                actual = await executor.locator(field.id).evaluate("el => Array.from(el.files || []).map(f => f.name)", timeout=1000)
            except Exception:
                if await executor.locator(field.id).count():
                    raise
        if actual is None:
            state = executor.upload_states[field.id]
            owner = executor.frame(field).locator(state['owner_selector'])
            retained = False
            for _ in range(20):
                executor.require(await owner.count()==1, 'Upload owner no longer resolves uniquely')
                retained = await owner.evaluate('''(owner,name) => {
                  if(!owner.isConnected)return false;
                  const visible=e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0};
                  const filename=Array.from(owner.querySelectorAll('*')).some(e=>visible(e)&&(e.innerText||'').trim()===name);
                  const removal=Array.from(owner.querySelectorAll('button,[role=button]')).some(e=>visible(e)&&/remove|delete|usuń/i.test(e.getAttribute('aria-label')||e.innerText||''));
                  return filename&&removal;
                }''', state['name'])
                if retained:
                    break
                import asyncio
                await asyncio.sleep(.2)
            executor.require(retained, 'Uploaded file replacement is not verified in its owned field')
            actual = [state['name']]
        executor.require(actual == [self.document_path(executor, value, field).name], "file upload was not retained")
        return actual


class CheckboxDriver(ControlDriver):
    kinds = {"CHECKBOX", "SWITCH", "CONSENT"}

    async def interact(self, executor: Any, field: SemanticField, action: str, value: Any) -> None:
        locator = executor.locator(field.id)
        wanted = action == "check" if action in {"check", "uncheck"} else bool(value)
        role = str(await locator.get_attribute("role") or "").casefold()
        if role in {"checkbox", "switch"}:
            actual = await executor.checked_state(locator)
            if actual != wanted:
                await locator.click()
        else:
            await locator.set_checked(wanted)

    async def verify(self, executor: Any, field: SemanticField, action: str, value: Any) -> Any:
        wanted = action == "check" if action in {"check", "uncheck"} else bool(value)
        actual = await executor.checked_state(executor.locator(field.id))
        executor.require(actual == wanted, "checked state mismatch")
        return actual


class RadioGroupDriver(ControlDriver):
    kinds = {"RADIO_GROUP", "RATING"}

    async def interact(self, executor: Any, field: SemanticField, action: str, value: Any) -> None:
        option = executor.option_locator(field, value)
        role = str(await option.get_attribute("role") or "").casefold()
        if role == "radio":
            await option.click()
        else:
            await option.check()

    async def verify(self, executor: Any, field: SemanticField, action: str, value: Any) -> Any:
        option = executor.option_locator(field, value)
        actual = await executor.checked_state(option)
        executor.require(actual, "radio selection was not retained")
        return executor.option_label(field, value)


class CheckboxGroupDriver(ControlDriver):
    kinds = {'CHECKBOX_GROUP'}

    async def interact(self, executor, field, action, value):
        wanted = set(value if isinstance(value, list) else [value])
        for option in field.options:
            if option.disabled:
                continue
            locator = executor.option_locator(field, option.id)
            expected = option.id in wanted
            if await executor.checked_state(locator) != expected:
                role = str(await locator.get_attribute('role') or '').casefold()
                if role == 'checkbox':
                    await locator.click()
                else:
                    # Styled native inputs are commonly covered by their visible label.
                    # Force is safe here because the exact inspected option is verified below.
                    await locator.set_checked(expected, force=True)

    async def verify(self, executor, field, action, value):
        wanted = set(value if isinstance(value, list) else [value])
        actual = set()
        for option in field.options:
            if await executor.checked_state(executor.option_locator(field, option.id)):
                actual.add(option.id)
        executor.require(actual == wanted, 'Checkbox group selection differs from intended set')
        return sorted(actual)


class NativeSelectDriver(ControlDriver):
    kinds = {"NATIVE_SELECT"}

    async def interact(self, executor: Any, field: SemanticField, action: str, value: Any) -> None:
        locator = executor.locator(field.id)
        option_value = executor.option_value(field, value)
        option_label = executor.option_label(field, value)
        try:
            await locator.select_option(value=str(option_value))
        except Exception:
            await locator.select_option(label=option_label)

    async def verify(self, executor: Any, field: SemanticField, action: str, value: Any) -> Any:
        actual = await executor.locator(field.id).input_value()
        expected = executor.option_value(field, value)
        executor.require(executor.values_match(actual, expected), "selected option was not retained")
        return actual


class CustomSelectDriver(ControlDriver):
    kinds = {"CUSTOM_SELECT", "COMBOBOX", "AUTOCOMPLETE"}

    async def inspect(self, executor: Any, field: SemanticField) -> SemanticField:
        return await executor.discover_dynamic_options(field)

    async def interact(self, executor: Any, field: SemanticField, action: str, value: Any) -> None:
        await executor.open_popup(field)
        option = executor.option_locator(field, value)
        await option.wait_for(state="visible", timeout=5000)
        await option.click()
        await executor.wait_popup_closed(field, option)

    async def verify(self, executor: Any, field: SemanticField, action: str, value: Any) -> Any:
        expected = executor.option_label(field, value)
        actual = await executor.control_display_value(field)
        if executor.values_match(actual, expected):
            return actual
        try:
            await executor.open_popup(field)
            option = executor.option_locator(field, value)
            await option.wait_for(state='visible', timeout=5000)
            if not await option.count():
                raise ValueError('selected option was not observable')
            selected = (
                str(await option.get_attribute("aria-selected") or "").casefold() == "true"
                or str(await option.get_attribute("data-state") or "").casefold() in {"checked", "selected"}
            )
        except Exception:
            selected = False
        finally:
            if await executor.locator(field.id).get_attribute('aria-expanded') == 'true':
                await executor.locator(field.id).press('Escape')
        executor.require(selected, "selected option was not observable")
        return expected


class AutocompleteDriver(CustomSelectDriver):
    kinds = {"AUTOCOMPLETE", "COMBOBOX"}


class DateDriver(NativeInputDriver):
    kinds = {"DATE"}


class NumberDriver(NativeInputDriver):
    kinds = {"NUMBER"}


class RichTextDriver(ControlDriver):
    kinds = {"CONTENTEDITABLE", "RICH_TEXT"}

    async def interact(self, executor, field, action, value):
        editor = executor.locator(field.id)
        await editor.fill(str(value))
        await editor.press('Tab')

    async def verify(self, executor, field, action, value):
        actual = await executor.locator(field.id).inner_text()
        executor.require(' '.join(actual.split()) == ' '.join(str(value).split()), 'Editor text was not retained')
        backing = executor.registry.resolve(field.id).metadata.get('backing_selector')
        if backing:
            from bs4 import BeautifulSoup
            frame = executor.frame(field)
            raw = await frame.locator(backing).input_value()
            text = BeautifulSoup(raw, 'html.parser').get_text(' ', strip=True)
            executor.require(' '.join(text.split()) == ' '.join(str(value).split()), 'Editor backing value is not synchronized')
        return actual


class MultiSelectDriver(CustomSelectDriver):
    kinds = {"MULTI_SELECT"}

    async def interact(self, executor, field, action, value):
        wanted = value if isinstance(value, list) else [value]
        locator = executor.locator(field.id)
        if await locator.evaluate('el => el.tagName') == 'SELECT':
            await locator.select_option(value=[str(executor.option_value(field, item)) for item in wanted])
            return
        for item in wanted:
            await executor.open_popup(field)
            option = executor.option_locator(field, item)
            await option.click()
        await locator.press('Escape')

    async def verify(self, executor, field, action, value):
        wanted = value if isinstance(value, list) else [value]
        backing = executor.registry.resolve(field.id).metadata.get('backing_selector')
        locator = executor.frame(field).locator(backing) if backing else executor.locator(field.id)
        if await locator.evaluate('el => el.tagName') == 'SELECT':
            actual = await locator.evaluate('el => Array.from(el.selectedOptions).map(o => o.value)')
            expected = [str(executor.option_value(field, item)) for item in wanted]
        else:
            actual = await locator.evaluate("el => Array.from(el.parentElement.querySelectorAll('[data-value], [aria-selected=true]')).map(n => n.innerText.trim())")
            expected = [executor.option_label(field, item) for item in wanted]
        executor.require(set(actual) == set(expected), 'Multi-select selected set differs from the planned set')
        return actual


class CompositePhoneDriver(NativeInputDriver):
    kinds = {"PHONE"}

    async def interact(self, executor, field, action, value):
        import phonenumbers
        parsed = phonenumbers.parse(str(value), None)
        executor.require(phonenumbers.is_possible_number(parsed), 'Candidate phone is not a complete international number')
        spec = executor.registry.resolve(field.id)
        country = spec.metadata.get('country_selector')
        number = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        if country:
            trigger = executor.frame(field).locator(country).first
            from phonenumbers import geocoder
            current = ' '.join((
                str(await trigger.get_attribute('aria-label') or ''),
                str(await trigger.inner_text() or ''),
            )).casefold()
            current_input = str(await executor.locator(field.id).input_value() or field.value or '')
            country_name = geocoder.country_name_for_number(parsed, 'en').casefold()
            current_matches = bool(country_name and country_name in current) or bool(
                re.search(r'\+' + str(parsed.country_code) + r'(?!\d)', current)
            ) or bool(re.match(r'^\s*\+' + str(parsed.country_code) + r'(?!\d)', current_input))
            if not current_matches:
                await trigger.click()
                options = executor.frame(field).locator('[role=option]:visible, .iti__country:visible, .country:visible')
                matches = []
                for index in range(await options.count()):
                    option = options.nth(index)
                    dial = await option.get_attribute('data-dial-code') or await option.get_attribute('data-dial')
                    label = await option.inner_text()
                    if str(dial or '') == str(parsed.country_code) or re.search(r'\+' + str(parsed.country_code) + r'(?!\d)', label):
                        matches.append(option)
                # Shared calling codes require an exact region when available; never guess.
                if len(matches) > 1:
                    region = (phonenumbers.region_code_for_number(parsed) or '').lower()
                    precise = []
                    for option in matches:
                        option_region = str(await option.get_attribute('data-country-code') or '').lower()
                        option_label = (await option.inner_text()).casefold()
                        if option_region == region or (country_name and country_name in option_label):
                            precise.append(option)
                    all_named = bool(precise and country_name)
                    for option in precise:
                        if country_name not in (await option.inner_text()).casefold():
                            all_named = False
                            break
                    if all_named:
                        matches = precise[:1]
                    else:
                        matches = precise or matches
                if len(matches) > 1:
                    identities = {
                        ' '.join((await option.inner_text()).casefold().split())
                        for option in matches
                    }
                    if len(identities) == 1:
                        matches = matches[:1]
                executor.require(len(matches) == 1, 'Phone country could not be selected unambiguously')
                await matches[0].click()
            number = phonenumbers.national_significant_number(parsed)
        await executor.locator(field.id).fill(number)
        await executor.locator(field.id).press('Tab')

    async def verify(self, executor, field, action, value):
        import phonenumbers
        expected = phonenumbers.parse(str(value), None)
        spec = executor.registry.resolve(field.id)
        canonical = spec.metadata.get('canonical_selector')
        actual = await executor.frame(field).locator(canonical).input_value() if canonical else await executor.locator(field.id).input_value()
        if not actual.startswith('+') and spec.metadata.get('country_selector'):
            actual = '+' + str(expected.country_code) + re.sub(r'\D', '', actual)
        parsed = phonenumbers.parse(actual, None)
        executor.require(parsed.country_code == expected.country_code and parsed.national_number == expected.national_number, 'Complete phone number was not retained')
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def default_control_drivers() -> Iterable[ControlDriver]:
    # Specific drivers precede their more general parents.
    return (
        RichTextDriver(), MultiSelectDriver(), CompositePhoneDriver(), NumberDriver(),
        FileUploadDriver(), CheckboxGroupDriver(), CheckboxDriver(), RadioGroupDriver(), NativeSelectDriver(),
        AutocompleteDriver(), CustomSelectDriver(), TextareaDriver(), DateDriver(), NativeInputDriver(),
    )
