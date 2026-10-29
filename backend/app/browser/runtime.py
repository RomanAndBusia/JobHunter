"""Central CDP attachment. Direct Chromium launch and stealth are retired."""
from dataclasses import asdict, dataclass
from typing import Optional

from app.browser.chrome import BrowserConnectionError, ChromeProcessManager


@dataclass
class BrowserStealthStatus:
    # Retained as a response-shape compatibility alias; no stealth runs.
    configured: bool = False
    implementation: str = 'google-chrome-cdp'
    available: Optional[bool] = True
    applied: bool = False
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    def payload(self):
        return asdict(self)


async def connect_chrome(playwright, process_manager):
    endpoint = await process_manager.ensure_started()
    try:
        browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=15000)
        if not browser.contexts:
            raise BrowserConnectionError('Chrome did not expose its persistent context.')
        return browser, browser.contexts[0]
    except BrowserConnectionError:
        raise
    except Exception as exc:
        raise BrowserConnectionError('Playwright could not connect to the owned Google Chrome CDP session. Retry to reconnect.') from exc
