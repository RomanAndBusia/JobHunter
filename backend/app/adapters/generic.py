import asyncio
from typing import Optional
from urllib.parse import urlsplit

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from app.adapters.base import ApplicationTarget, BaseAdapter, ChallengeCallback
from app.browser.challenge import ManualVerificationPending
from app.browser.manager import BrowserManager
from app.models import Job


class GenericAdapter(BaseAdapter):
    name = "generic"

    def __init__(self, provider=None):
        self.provider = provider

    def matches(self, url: str) -> bool:
        return True

    async def _find_apply(self, page: Page, job: Job):
        from app.ai.provider import get_ai_provider
        from app.browser.page_planner import SemanticPagePlanner
        return await SemanticPagePlanner(self.provider or get_ai_provider()).inspect(page, job)

    @staticmethod
    async def _has_visible_form(page: Page) -> bool:
        controls = page.locator(
            "form input:not([type=hidden]):not([type=submit]), form textarea, form select, "
            "form [role=combobox], form input[type=file], [contenteditable=true], [role=textbox]"
        )
        for index in range(min(await controls.count(), 30)):
            control = controls.nth(index)
            excluded = await control.evaluate("el => !!el.closest('header, nav, [role=search]') || el.type === 'search'")
            if not excluded and await control.is_visible():
                return True
        return False

    async def navigate_job_page(
        self,
        manager: BrowserManager,
        page: Page,
        job: Job,
        challenge_callback: Optional[ChallengeCallback] = None,
    ) -> None:
        await page.goto(job.canonical_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(500)
        result = await manager.wait_for_manual_verification(page, challenge_callback)
        if result.timed_out:
            raise ManualVerificationPending("Manual verification is still required")

    async def open_loaded_application(
        self,
        manager: BrowserManager,
        page: Page,
        job: Job,
        challenge_callback: Optional[ChallengeCallback] = None,
        entry_depth: int = 0,
    ) -> ApplicationTarget:
        before_url = page.url
        before_text = await page.locator('body').inner_text()
        if await self._has_visible_form(page):
            page_id = manager._register(page)
            manager.active_application_page_id = page_id
            return ApplicationTarget(
                page, page_id, "direct_application_form", page.url,
                urlsplit(page.url).netloc.casefold(),
            )
        if entry_depth >= 4:
            raise RuntimeError("Application entry exceeded four verified navigation steps")
        apply = await self._find_apply(page, job)
        if apply is None:
            raise RuntimeError("No visible Apply action or application form found")
        popup_task = None
        if manager.context is not None:
            popup_task = asyncio.create_task(page.wait_for_event("popup", timeout=3000))
            await asyncio.sleep(0)  # Install the opener-specific listener before clicking.
        try:
            await apply.click()
        except BaseException:
            if popup_task is not None:
                popup_task.cancel()
                await asyncio.gather(popup_task, return_exceptions=True)
            raise
        target_page = page
        if popup_task is not None:
            try:
                target_page = await popup_task
                await target_page.wait_for_load_state("domcontentloaded", timeout=30000)
            except PlaywrightTimeoutError:
                popup_task.cancel()
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                except PlaywrightTimeoutError:
                    pass
        await target_page.wait_for_timeout(500)
        verification = await manager.wait_for_manual_verification(target_page, challenge_callback)
        if verification.timed_out:
            raise ManualVerificationPending("Manual verification is still required")
        if not await self._has_visible_form(target_page):
            if target_page is page and target_page.url == before_url and await target_page.locator('body').inner_text() == before_text:
                raise RuntimeError('NAVIGATION_FAILED: application entry produced no observable change')
            return await self.open_loaded_application(manager, target_page, job, challenge_callback, entry_depth + 1)
        page_id = await manager._bind_page(target_page) if hasattr(manager, '_bind_page') else manager._register(target_page)
        manager.active_application_page_id = page_id
        host = urlsplit(target_page.url).netloc.casefold()
        if target_page is not page:
            target_type = "new_tab_internal" if host == urlsplit(job.canonical_url).netloc.casefold() else "new_tab_external"
        elif target_page.url != before_url:
            target_type = "same_tab_internal" if host == urlsplit(job.canonical_url).netloc.casefold() else "same_tab_external"
        else:
            target_type = "modal_or_inline"
        return ApplicationTarget(target_page, page_id, target_type, target_page.url, host)

    async def open_application(
        self,
        manager: BrowserManager,
        page: Page,
        job: Job,
        challenge_callback: Optional[ChallengeCallback] = None,
    ) -> ApplicationTarget:
        await self.navigate_job_page(manager, page, job, challenge_callback)
        return await self.open_loaded_application(manager, page, job, challenge_callback)
