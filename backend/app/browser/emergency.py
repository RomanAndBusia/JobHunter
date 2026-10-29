import logging
from typing import Any, Dict, List, Optional

from playwright.async_api import Page

from app.forms.sanitize import sanitize_html
from app.logging import event


logger = logging.getLogger(__name__)


class EmergencyBrowserTools:
    """Restricted, logged browser operations used only after semantic fallbacks fail."""

    def __init__(self, page: Page, application_id: int):
        self.page = page
        self.application_id = application_id

    def _log(self, action: str, **details: Any) -> None:
        event(logger, "emergency_browser_action", application_id=self.application_id, fallback_level=5, action=action, **details)

    async def inspect_html(self, selector: Optional[str] = None, max_chars: int = 30000) -> str:
        self._log("inspect_html", selector=selector, max_chars=max_chars)
        html = await self.page.content()
        return sanitize_html(html, min(max_chars, 50000), selector)

    async def find_text(self, text: str) -> int:
        self._log("find_text", text=text[:200])
        return await self.page.get_by_text(text, exact=False).count()

    async def find_role(self, role: str, name: Optional[str] = None) -> int:
        self._log("find_role", role=role, name=(name or "")[:200])
        return await self.page.get_by_role(role, name=name).count()

    async def get_value(self, selector: str) -> str:
        self._log("get_value", selector=selector[:300])
        return await self.page.locator(selector).first.input_value()

    async def get_visible_text(self, selector: str) -> str:
        self._log("get_visible_text", selector=selector[:300])
        return (await self.page.locator(selector).first.inner_text())[:5000]

    async def list_visible_elements(self, selector: str = "input,select,textarea,button,[role]") -> List[Dict[str, Any]]:
        self._log("list_visible_elements", selector=selector[:300])
        return await self.page.locator(selector).evaluate_all(
            "els => els.filter(e => !!(e.offsetWidth || e.offsetHeight)).slice(0, 200).map(e => ({tag:e.tagName, role:e.getAttribute('role'), name:e.getAttribute('aria-label') || e.innerText || e.name || '', type:e.type || ''}))"
        )

    async def safe_action(self, action: str, selector: str, value: Optional[str] = None) -> None:
        if action not in {"click", "fill", "press", "hover", "scroll"}:
            raise ValueError("Emergency action is not allowed")
        self._log(action, selector=selector[:300])
        locator = self.page.locator(selector).first
        if action == "click":
            from app.browser.action_safety import guard_action
            await guard_action(locator, self.application_id)
            await locator.click()
        elif action == "fill":
            await locator.fill(value or "")
        elif action == "press":
            if (value or 'Enter').casefold() in {'enter', 'return'}:
                from app.browser.action_safety import guard_action
                await guard_action(locator, self.application_id)
            await locator.press(value or "Enter")
        elif action == "hover":
            await locator.hover()
        else:
            await locator.scroll_into_view_if_needed()
