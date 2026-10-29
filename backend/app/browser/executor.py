import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from playwright.async_api import Locator, Page

from app.browser.control_drivers import ControlDriver, default_control_drivers
from app.browser.form_scanner import BrowserFormScanner
from app.forms.semantic import LocatorSpec, SemanticRegistry
from app.logging import event
from app.schemas.api import SemanticField, SemanticOption
from app.utils.hashing import normalized_text


@dataclass
class ActionResult:
    success: bool
    actual_value: Any = None
    error: Optional[str] = None


class BrowserActionExecutor:
    logger = logging.getLogger(__name__)

    def __init__(self, page: Page, registry: SemanticRegistry, application_id: Optional[int] = None, selected_document=None, document_bindings=None):
        self.page = page
        self.registry = registry
        self.application_id = application_id
        self.selected_document = selected_document
        self.document_bindings = document_bindings or {}
        self.upload_states = {}
        self.drivers = list(default_control_drivers())

    def _locator(self, field_id: str) -> Locator:
        spec = self.registry.resolve(field_id)
        frame = BrowserFormScanner.frame_for(self.page, spec.frame)
        if spec.strategy == "css":
            return frame.locator(spec.value).first
        if spec.strategy == "label":
            return frame.get_by_label(spec.value).first
        if spec.strategy == "role":
            role, name = spec.value.split("|", 1)
            return frame.get_by_role(role, name=name).first
        if spec.strategy == "text":
            return frame.get_by_text(spec.value, exact=True).first
        raise ValueError("Unsupported locator strategy: %s" % spec.strategy)

    def locator(self, field_id: str) -> Locator:
        return self._locator(field_id)

    def frame(self, field):
        return BrowserFormScanner.frame_for(self.page, self.registry.resolve(field.id).frame)

    @staticmethod
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    @staticmethod
    def values_match(actual: Any, expected: Any) -> bool:
        left = normalized_text(str(actual or ""))
        right = normalized_text(str(expected or ""))
        return bool(left and right) and left == right

    async def checked_state(self, locator: Locator) -> bool:
        role = str(await locator.get_attribute("role") or "").casefold()
        if role in {"radio", "checkbox", "switch"}:
            return (
                str(await locator.get_attribute("aria-checked") or "").casefold() == "true"
                or str(await locator.get_attribute("data-state") or "").casefold() == "checked"
            )
        return await locator.is_checked()

    def option_value(self, field: SemanticField, value: Any) -> Any:
        if isinstance(value, str) and value in self.registry.options:
            owner, option_value = self.registry.options[value]
            if owner != field.id:
                raise ValueError("Option does not belong to the requested field")
            return option_value
        return value

    def option_label(self, field: SemanticField, value: Any) -> str:
        return next((option.label for option in field.options if option.id == value), str(self.option_value(field, value)))

    def _locator_from_spec(self, spec: LocatorSpec) -> Locator:
        frame = BrowserFormScanner.frame_for(self.page, spec.frame)
        if spec.strategy == "css":
            return frame.locator(spec.value).first
        if spec.strategy == "role":
            role, name = spec.value.split("|", 1)
            return frame.get_by_role(role, name=name, exact=True).first
        if spec.strategy == "text":
            return frame.get_by_text(spec.value, exact=True).first
        raise ValueError("Unsupported option locator strategy: %s" % spec.strategy)

    def option_locator(self, field: SemanticField, value: Any) -> Locator:
        option_id = str(value)
        spec = self.registry.option_locators.get(option_id)
        if spec is not None:
            if spec.metadata.get('overlay_id'):
                escaped = spec.metadata['overlay_id'].replace('\\', '\\\\').replace('"', '\\"')
                import re
                label_pattern = re.compile(r'^\s*' + r'\s*'.join(re.escape(part) for part in self.option_label(field, value).split()) + r'\s*$')
                return self.frame(field).locator('[id="%s"] [role=option], [id="%s"] [role=menuitem], [id="%s"] button' % (escaped, escaped, escaped)).filter(has_text=label_pattern).first
            return self._locator_from_spec(spec)
        label = self.option_label(field, value)
        field_spec = self.registry.resolve(field.id)
        frame = BrowserFormScanner.frame_for(self.page, field_spec.frame)
        if field_spec.metadata.get('widget') == 'selectize':
            return frame.locator('.selectize-dropdown .option:visible').filter(has_text=__import__('re').compile('^' + __import__('re').escape(label) + '$')).first
        return frame.get_by_role("option", name=label, exact=True).first

    def _driver(self, field: SemanticField, action: str) -> ControlDriver:
        for driver in self.drivers:
            if driver.supports(field, action):
                return driver
        raise ValueError("No control driver supports %s" % field.control_kind)

    async def collection_contains(self, language_field, language_id, level_field, level_id):
        import json
        spec = self.registry.resolve(language_field.id)
        owner = self.frame(language_field).locator(spec.metadata['collection']['owner']).first
        state = await owner.evaluate('''el => {
          const clone = el.cloneNode(true);
          clone.querySelectorAll('select,input,button,label,a').forEach(n => n.remove());
          return {rows:Array.from(clone.querySelectorAll('li,[role=listitem],tbody tr,.selected-item,.language-entry')).map(n=>n.textContent),
                  values:Array.from(el.querySelectorAll('input[type=hidden]')).map(n=>n.value)};
        }''')
        name = self.option_label(language_field, language_id)
        level = self.option_label(level_field, level_id)
        if any(name.casefold() in row.casefold() and level.casefold() in row.casefold() for row in state['rows']):
            return True
        expected = [str(self.option_value(language_field, language_id)), str(self.option_value(level_field, level_id))]
        def contains_pair(value):
            if isinstance(value, dict):
                scalar = [str(v) for v in value.values() if not isinstance(v, (dict, list))]
                return (all(scalar.count(v) >= expected.count(v) for v in expected)
                        or any(contains_pair(v) for v in value.values()))
            if isinstance(value, list):
                return any(contains_pair(v) for v in value)
            return False
        for raw in state['values']:
            try:
                if contains_pair(json.loads(raw)):
                    return True
            except (ValueError, TypeError):
                continue
        return False

    async def commit_language_collection(self, language_field, language_id, level_field, level_id):
        """Use the Add action once and verify rendered/backing pair state."""
        if await self.collection_contains(language_field, language_id, level_field, level_id):
            return ActionResult(True)
        spec = self.registry.resolve(language_field.id)
        try:
            self.require(await self.locator(language_field.id).input_value() == str(self.option_value(language_field, language_id)),
                         'Language selection changed before Add')
            self.require(await self.locator(level_field.id).input_value() == str(self.option_value(level_field, level_id)),
                         'Language level changed before Add')
            add = self.frame(language_field).locator(spec.metadata['collection']['add']).first
            from app.browser.action_safety import guard_action
            await guard_action(add, self.application_id)
            await add.click()
            # Read-only bounded polling; never repeat the non-idempotent Add.
            for _ in range(20):
                if await self.collection_contains(language_field, language_id, level_field, level_id):
                    return ActionResult(True)
                await asyncio.sleep(.1)
            return ActionResult(False, error='Language addition could not be verified')
        except Exception as exc:
            return ActionResult(False, error='Language addition failed: %s' % type(exc).__name__)

    async def execute(self, action: str, field: SemanticField, value: Any = None) -> ActionResult:
        event(
            self.logger, "executor.action_started", application_id=self.application_id,
            field_id=field.id, control_kind=field.control_kind, action=action,
        )
        try:
            driver = self._driver(field, action)
            event(self.logger, 'control.driver_selected', field_id=field.id, driver=type(driver).__name__)
            if action != "upload":
                await self.locator(field.id).scroll_into_view_if_needed(timeout=5000)
            for attempt in range(2):
                try:
                    await driver.interact(self, field, action, value)
                    actual = await driver.verify(self, field, action, value)
                    break
                except Exception:
                    if attempt or action not in {'fill', 'check', 'uncheck', 'clear'}:
                        raise
                    # Retry only idempotent interactions; every driver re-resolves live locators.
                    await asyncio.sleep(0.15)
            event(
                self.logger, "executor.action_verified", application_id=self.application_id,
                field_id=field.id, control_kind=field.control_kind, action=action,
            )
            return ActionResult(True, actual)
        except Exception as exc:
            event(
                self.logger, "executor.action_failed", application_id=self.application_id,
                field_id=field.id, control_kind=field.control_kind, action=action,
                error_type=type(exc).__name__,
            )
            return ActionResult(False, error="%s: %s" % (type(exc).__name__, str(exc)))

    async def discover_options(self, field: SemanticField, query: str = "") -> SemanticField:
        """Open a lazy control and return only options observed on the live page."""
        driver = self._driver(field, "select_option")
        event(self.logger, 'control.options_requested', application_id=self.application_id, field_id=field.id)
        if query and field.control_kind in {"AUTOCOMPLETE", "COMBOBOX"}:
            locator = self.locator(field.id)
            tag = await locator.evaluate("el => el.tagName.toLowerCase()")
            if tag in {"input", "textarea"}:
                await locator.fill("")
                await locator.press_sequentially(str(query), delay=25)
        discovered = await driver.inspect(self, field)
        event(
            self.logger, "control.options_discovered", application_id=self.application_id,
            field_id=field.id, control_kind=field.control_kind, option_count=len(discovered.options),
        )
        return discovered

    async def open_popup(self, field: SemanticField) -> None:
        trigger = self.locator(field.id)
        await trigger.scroll_into_view_if_needed(timeout=5000)
        expanded = str(await trigger.get_attribute("aria-expanded") or "").casefold()
        if expanded != "true":
            await trigger.click()
        for _ in range(20):
            expanded = str(await self.locator(field.id).get_attribute("aria-expanded") or "").casefold()
            candidates = await self._popup_candidates(field)
            if expanded == "true" or await candidates.count():
                return
            await asyncio.sleep(0.1)
        raise TimeoutError("Custom control did not expose an option overlay")

    async def _popup_candidates(self, field: SemanticField) -> Locator:
        spec = self.registry.resolve(field.id)
        frame = BrowserFormScanner.frame_for(self.page, spec.frame)
        trigger = self.locator(field.id)
        controlled = await trigger.get_attribute('aria-controls') or await trigger.get_attribute('aria-owns')
        if controlled:
            escaped = controlled.replace('\\', '\\\\').replace('"', '\\"')
            overlay = frame.locator('[id="%s"]:visible' % escaped)
            if await overlay.count():
                return overlay.locator('[role=option], [role=menuitem], button')
        return frame.locator(
            '[role="option"]:visible, [role="menuitem"]:visible, '
            '[role="dialog"] button:visible, [role="listbox"] button:visible, '
            '[role="menu"] button:visible'
            ', .selectize-dropdown .option:visible'
        )

    async def discover_dynamic_options(self, field: SemanticField) -> SemanticField:
        await self.open_popup(field)
        spec = self.registry.resolve(field.id)
        candidates = await self._popup_candidates(field)
        overlay_id = await self.locator(field.id).get_attribute('aria-controls') or await self.locator(field.id).get_attribute('aria-owns')
        observed = await candidates.evaluate_all('''nodes => nodes.slice(0,300).map(e=>({
          label:e.innerText,role:e.getAttribute('role')||'button',disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true',
          selected:e.getAttribute('aria-selected')==='true'||['selected','checked'].includes(e.getAttribute('data-state'))
        }))''')
        options = []
        seen = set()
        for row in observed:
            label = " ".join(row['label'].split())
            if (
                not label or normalized_text(label) in seen
                or normalized_text(label) in {"close", "cancel", "anuluj", "zamknij"}
            ):
                continue
            seen.add(normalized_text(label))
            option_id = "%sd%d" % (field.id, len(options) + 1)
            role, disabled, selected = row['role'], row['disabled'], row['selected']
            options.append(SemanticOption(id=option_id, label=label, selected=selected, disabled=disabled))
            self.registry.register_option(
                option_id, field.id, label,
                LocatorSpec("role", "%s|%s" % (role, label), spec.frame, 0.9, {
                    "dynamic": True, "role": role, "overlay_id":overlay_id,
                }),
            )
        try:
            if str(await self.locator(field.id).get_attribute("aria-expanded") or "").casefold() == "true":
                await self.locator(field.id).press("Escape")
        except Exception:
            pass
        return field.model_copy(update={"options": options, "option_discovery_state": "KNOWN" if options else "EMPTY"})

    async def wait_popup_closed(self, field: SemanticField, option: Locator) -> None:
        for _ in range(20):
            expanded = str(await self.locator(field.id).get_attribute("aria-expanded") or "").casefold()
            try:
                option_visible = await option.is_visible()
            except Exception:
                option_visible = False
            if expanded == "false" or not option_visible:
                return
            await asyncio.sleep(0.1)

    async def control_display_value(self, field: SemanticField) -> str:
        from app.browser.control_state import display_value
        return str(await display_value(self.locator(field.id)))

    async def click_action(self, action_id: str, expected_change: bool = False, approved_final: bool = False) -> ActionResult:
        locator = self._locator(action_id)
        from app.browser.action_safety import guard_action
        await guard_action(locator, self.application_id, approved_final)
        before_url = self.page.url
        before_text = await self.page.locator("body").inner_text()
        try:
            await locator.click()
            if expected_change:
                for _ in range(20):
                    await asyncio.sleep(0.15)
                    if self.page.url != before_url or await self.page.locator("body").inner_text() != before_text:
                        return ActionResult(True, self.page.url)
                return ActionResult(False, error="form did not change after action")
            return ActionResult(True, True)
        except Exception as exc:
            return ActionResult(False, error="%s: %s" % (type(exc).__name__, str(exc)))
