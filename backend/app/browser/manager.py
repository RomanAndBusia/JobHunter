import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from app.browser.runtime import BrowserStealthStatus, connect_chrome
from app.browser.chrome import ChromeProcessManager
from app.browser.challenge import ChallengeDetector, ChallengeState, ChallengeWaitResult
from app.config import REPOSITORY_ROOT, Settings, settings
from app.logging import event
from app.errors import JobHunterError


logger = logging.getLogger(__name__)


class BrowserSessionLostError(JobHunterError):
    def __init__(self, reason: str = "page_unavailable") -> None:
        super().__init__(
            "browser_session_lost",
            "The application browser session was lost. Retry to reopen the application safely.",
            409,
            "browser.session",
            {"reason": reason},
        )


class BrowserManager:
    """Owns the CDP connection to headed Chrome and tracks borrowed pages."""

    def __init__(self, config: Optional[Settings] = None) -> None:
        self.config = config or settings
        self._playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.browser = None
        self.chrome = ChromeProcessManager(self.config)
        self.pages: Dict[str, Page] = {}
        self.active_application_page_id: Optional[str] = None
        self.dashboard_page_id: Optional[str] = None
        self._dashboard_retry_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._resume = asyncio.Event()
        self._resume.set()
        self.session_key = str(uuid.uuid4())
        self.stealth_status = BrowserStealthStatus()
        self.challenge_detector = ChallengeDetector()
        self.active_challenge: Optional[ChallengeState] = None
        self.active_challenge_page_id: Optional[str] = None
        self._document_statuses: Dict[int, int] = {}
        self.navigation = {}
        self.challenges = {}
        self.session_state = {}
        self.application_pages = {}
        self.page_owners = {}
        self.page_openers = {}

    async def bind_application_page(self, application_id, page):
        """Transfer an application only to a target selected by trusted navigation."""
        self.ensure_page(page)
        page_id = await self._bind_page(page)
        owner = self.page_owners.get(page_id)
        if page_id == self.dashboard_page_id or owner not in (None, application_id):
            raise BrowserSessionLostError('page_owned_by_another_application')
        self.page_owners[page_id] = application_id
        self.application_pages[application_id] = {'page': page, 'page_id': page_id,
                                                  'url': page.url, 'session': self.session_key}
        event(logger, 'application.page_bound', application_id=application_id,
              browser_session_id=self.session_key, page_id=page_id, url=page.url)
        return page_id

    async def resolve_active_application_page(self, application_id, page_id=None, expected_url=None):
        """Resolve by CDP identity or an owned opener chain, never the active tab."""
        await self.ensure_started()
        binding = self.application_pages.get(application_id)
        resolved_id = binding['page_id'] if binding else page_id
        page = self.pages.get(resolved_id)
        expected = binding['url'] if binding else expected_url
        if page is not None and page.is_closed():
            page = None
        if page is not None:
            self.ensure_page(page)
            if self.page_owners.get(resolved_id) not in (None, application_id):
                raise BrowserSessionLostError('page_owned_by_another_application')
            if expected and page.url != expected:
                raise BrowserSessionLostError('unexpected_application_url')
            await self.bind_application_page(application_id, page)
            return page
        event(logger, 'application.page_stale', application_id=application_id,
              browser_session_id=self.session_key, page_id=resolved_id)
        # A replacement tab is eligible only if Chrome reports its opener as
        # this application's prior page. URL coincidence is not ownership.
        replacements = []
        if binding and self.context is not None:
            for candidate in self.context.pages:
                if candidate.is_closed() or candidate is binding['page']:
                    continue
                candidate_id = self._register(candidate)
                if candidate_id == self.dashboard_page_id or self.page_owners.get(candidate_id) not in (None, application_id):
                    continue
                try:
                    opener = self.page_openers.get(id(candidate)) or await candidate.opener()
                except Exception:
                    continue
                if opener is binding['page']:
                    replacements.append(candidate)
        if len(replacements) != 1:
            raise BrowserSessionLostError('owned_target_unavailable')
        page = replacements[0]
        await self.bind_application_page(application_id, page)
        event(logger, 'application.page_reacquired', application_id=application_id,
              browser_session_id=self.session_key,
              page_id=self.application_pages[application_id]['page_id'], url=page.url)
        return page

    async def release_application_page(self, application_id, page_id=None):
        binding = self.application_pages.pop(application_id, None)
        owned = [key for key, owner in self.page_owners.items() if owner == application_id]
        # Older sessions can be released by their persisted target ID, but an
        # explicit owner always wins over a stale database reference.
        if page_id and self.page_owners.get(page_id) in (None, application_id):
            owned.append(page_id)
        for key in set(owned):
            self.page_owners.pop(key, None)
            await self.close_page(key)
        return bool(owned or binding)

    async def ensure_started(self) -> None:
        if self.context is not None and (self.browser is None or self.browser.is_connected()):
            return
        async with self._lock:
            if self.context is not None and (self.browser is None or self.browser.is_connected()):
                return
            recovering = self.context is not None
            previous_urls = {page_id: page.url for page_id, page in self.pages.items() if not page.is_closed()}
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self.browser, self.context = await connect_chrome(self._playwright, self.chrome)
            self.pages.clear()
            self.context.on("page", self._on_page)
            existing = list(self.context.pages)
            # Reconnect may create new Python Page wrappers for unchanged Chrome
            # tabs. Restore an ID only when its URL identifies exactly one tab.
            for page_id, url in previous_urls.items():
                if page_id.startswith('cdp-'):
                    continue  # Stable CDP identities must never be restored by URL.
                matches = [page for page in existing if page.url == url]
                if len(matches) == 1:
                    self._register(matches[0], page_id)
            for page in existing:
                await self._bind_page(page)
            event(logger, 'browser.cdp_connected', browser_session_id=self.session_key,
                  context_count=len(self.browser.contexts) if hasattr(self.browser, 'contexts') else 1,
                  pages=self.page_summary(), **self.chrome.payload())
            if recovering:
                event(logger, "browser_reconnected", **self.chrome.payload())
                return
            dashboard = next((page for page in existing if page.url.rstrip('/') == self.config.frontend_url.rstrip('/')), None)
            if dashboard is None:
                dashboard = await self.context.new_page()
            self.dashboard_page_id = await self._bind_page(dashboard)
            try:
                await dashboard.goto(self.config.frontend_url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                event(logger, "dashboard_navigation_pending", page_url=self.config.frontend_url)
                self._dashboard_retry_task = asyncio.create_task(self._retry_dashboard(dashboard))
            if len(self.context.pages) < 2:
                application_page = await self.context.new_page()
                self.active_application_page_id = await self._bind_page(application_page)
            event(
                logger,
                "browser_started",
                browser_session=self.session_key,
                headless=False,
                cdp_endpoint=self.chrome.endpoint,
                stealth_configured=self.stealth_status.configured,
                stealth_applied=self.stealth_status.applied,
                stealth_error_type=self.stealth_status.error_type,
            )

    async def _retry_dashboard(self, page: Page) -> None:
        for _attempt in range(15):
            await asyncio.sleep(2)
            if page.is_closed():
                return
            try:
                await page.goto(self.config.frontend_url, wait_until="domcontentloaded", timeout=5000)
                event(logger, "dashboard_navigation_ready", page_url=self.config.frontend_url)
                return
            except Exception:
                continue
        event(logger, "dashboard_navigation_failed", page_url=self.config.frontend_url)

    def _register(self, page: Page, restored_id: Optional[str] = None) -> str:
        for page_id, known in self.pages.items():
            if known is page:
                return page_id
        page_id = restored_id or "page-" + uuid.uuid4().hex[:12]
        self.pages[page_id] = page
        page.on("close", lambda: self.pages.pop(page_id, None))
        page.on('popup', lambda popup: self.page_openers.__setitem__(id(popup), page))
        page.on("response", lambda response: self._observe_response(page, response))
        return page_id

    def _observe_response(self, page: Page, response: Any) -> None:
        try:
            if response.request.is_navigation_request() and response.request.frame == page.main_frame:
                self._document_statuses[id(page)] = int(response.status)
                info = {'url': getattr(response, 'url', page.url), 'result': response.status, 'timestamp': datetime.utcnow().isoformat() + 'Z'}
                self.navigation[self._register(page)] = info
                from app.services.diagnostics import emit
                emit('browser.navigation', browser_session_id=self.session_key, **info)
        except Exception:
            return

    def _on_page(self, page: Page) -> None:
        page_id = self._register(page)
        if page_id != self.dashboard_page_id:
            self.active_application_page_id = page_id
        event(logger, "browser_page_opened", browser_session=self.session_key, page_id=page_id)

    async def _bind_page(self, page):
        if not hasattr(self.context, 'new_cdp_session'):
            return self._register(page)
        session = await self.context.new_cdp_session(page)
        try:
            info = await session.send('Target.getTargetInfo')
            stable_id = 'cdp-' + info['targetInfo']['targetId']
        finally:
            await session.detach()
        for old_id, known in list(self.pages.items()):
            if known is page and old_id != stable_id:
                self.pages.pop(old_id, None)
        return self._register(page, stable_id)

    async def new_application_page(self) -> Page:
        await self.ensure_started()
        assert self.context is not None
        page = await self.context.new_page()
        self.active_application_page_id = await self._bind_page(page)
        return page

    def get_page(self, page_id: Optional[str] = None) -> Page:
        resolved = page_id or self.active_application_page_id
        page = self.pages.get(resolved or "")
        if page is None:
            raise BrowserSessionLostError("page_unavailable")
        self.ensure_page(page)
        return page

    def ensure_page(self, page: Page) -> None:
        if self.context is None:
            raise BrowserSessionLostError("context_unavailable")
        if self.browser is not None and not self.browser.is_connected():
            raise BrowserSessionLostError('cdp_disconnected')
        if page.is_closed():
            raise BrowserSessionLostError("page_unavailable")
        try:
            if page not in self.context.pages:
                raise BrowserSessionLostError("page_detached")
        except BrowserSessionLostError:
            raise
        except Exception as exc:
            raise BrowserSessionLostError("context_closed") from exc

    async def close_page(self, page_id: Optional[str]) -> bool:
        if not page_id or page_id == self.dashboard_page_id:
            return False
        page = self.pages.pop(page_id, None)
        if page is None:
            return False
        if self.active_application_page_id == page_id:
            self.active_application_page_id = None
        if self.active_challenge_page_id == page_id:
            self.active_challenge_page_id = None
            self.active_challenge = None
        self._document_statuses.pop(id(page), None)
        if not page.is_closed():
            try:
                await page.close()
            except Exception:
                pass
        event(logger, "application_page_released", browser_session=self.session_key, page_id=page_id)
        return True

    async def focus(self, page_id: Optional[str] = None) -> None:
        await self.get_page(page_id).bring_to_front()

    async def pause(self) -> None:
        self._resume.clear()

    async def resume(self) -> None:
        self._resume.set()

    async def wait_until_resumed(self) -> None:
        await self._resume.wait()

    async def challenge_state(self, page: Page) -> ChallengeState:
        self.ensure_page(page)
        return await self.challenge_detector.detect(page, self._document_statuses.get(id(page)))

    async def wait_for_manual_verification(
        self,
        page: Page,
        on_state: Optional[Callable[[str, ChallengeState], Awaitable[None]]] = None,
        include_pending: bool = False,
    ) -> ChallengeWaitResult:
        """Keep the visible page intact while a person completes a challenge."""
        initial = await asyncio.wait_for(self.challenge_detector.detect(page, self._document_statuses.get(id(page)), include_pending), timeout=15)
        page_id = await self._bind_page(page)
        initial.page_id = page_id
        self.challenges[page_id] = initial
        if not initial.detected:
            if self.active_challenge_page_id == self._register(page):
                self.active_challenge = None
                self.active_challenge_page_id = None
            return ChallengeWaitResult(initial, initial, True, False, 0.0)
        self.active_challenge = initial
        initial.state = 'WAITING_FOR_USER'
        self.active_challenge_page_id = self._register(page)
        await page.bring_to_front()
        if on_state is not None:
            await on_state("waiting", initial)
        result = await self.challenge_detector.wait_until_clear(
            page,
            self.config.browser_challenge_timeout_seconds,
            self.config.browser_challenge_poll_seconds,
            self._document_statuses.get(id(page)),
            include_pending,
        )
        result.final.page_id = page_id
        self.challenges[page_id] = result.final
        if result.cleared:
            self.active_challenge = None
            self.active_challenge_page_id = None
        if on_state is not None:
            if result.cleared:
                await on_state("completed", result.final)
                await on_state("continuing", result.final)
            else:
                await on_state("timeout", result.final)
        return result

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    def page_summary(self) -> List[Dict[str, Any]]:
        return [{"id": page_id, "url": page.url, "closed": page.is_closed()} for page_id, page in self.pages.items()]

    async def inspect_session_state(self, page_id=None):
        """Counts only: never return cookie names/values or storage contents."""
        if self.context is None or not self.browser or not self.browser.is_connected():
            return
        try:
            cookies = await asyncio.wait_for(self.context.cookies(), timeout=3)
            self.session_state = {'cookie_count': len(cookies),
                'justjoin_cookie_count': sum('justjoin.it' in c.get('domain', '') for c in cookies),
                'storage_policy': 'Dedicated persistent profile; storage is never cleared by JobHunter'}
            if page_id in self.pages:
                counts = await asyncio.wait_for(self.pages[page_id].evaluate(
                    "() => ({local_storage_keys: localStorage.length, session_storage_keys: sessionStorage.length})"), timeout=3)
                self.session_state.update(counts)
        except Exception as exc:
            self.session_state['inspection_status'] = type(exc).__name__

    async def capture_diagnostic(self, page: Page, label: str) -> Optional[str]:
        """Save a local screenshot for a failed browser step without altering the page."""
        safe_label = "".join(char if char.isalnum() or char in "-_" else "-" for char in label)[:80]
        folder = REPOSITORY_ROOT / "storage" / "diagnostics"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("%s-%s-%s.png" % (
            datetime.utcnow().strftime("%Y%m%d-%H%M%S"), safe_label, uuid.uuid4().hex[:8],
        ))
        try:
            await page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as exc:
            event(logger, "browser_diagnostic_screenshot_failed", error_type=type(exc).__name__)
            return None

    async def stop(self) -> None:
        if self._dashboard_retry_task is not None and not self._dashboard_retry_task.done():
            self._dashboard_retry_task.cancel()
        # Disconnect without destroying the user's tabs/session. The owned Chrome
        # process is reused after backend restart; crash recovery is explicit.
        self.context = None
        self.browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self.pages.clear()
        self._document_statuses.clear()
        self.active_challenge = None
        self.active_challenge_page_id = None


browser_manager = BrowserManager()
