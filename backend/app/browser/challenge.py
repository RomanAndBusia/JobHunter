import asyncio
import time
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional
from app.logging import event


CHALLENGE_URL_MARKERS = (
    "/captcha", "/challenge", "/cdn-cgi/challenge-platform/", "challenge?",
    "recaptcha", "hcaptcha", "turnstile",
)
CHALLENGE_TITLE_MARKERS = (
    "just a moment", "checking your browser", "attention required",
    "verify you are human", "security check", "human verification",
    "cierpliwości", "proszę czekać", "sprawdzanie przeglądarki",
)
CHALLENGE_TEXT_MARKERS = (
    "performing security verification", "verifies you are not a bot",
    "verify you are human", "checking your browser", "complete the security check",
    "i'm not a robot", "i am not a robot", "nie jestem robotem",
    "potwierdź, że jesteś człowiekiem", "weryfikacja bezpieczeństwa",
    "sprawdzamy twoją przeglądarkę", "weryfikujemy twoją przeglądarkę",
)
CHALLENGE_SELECTORS = (
    'iframe[src*="recaptcha" i]', 'iframe[src*="hcaptcha" i]',
    'iframe[src*="challenges.cloudflare.com" i]', '[class*="cf-turnstile" i]',
    '[data-sitekey]', '#challenge-form', '[id*="captcha" i]', '[class*="captcha" i]',
)


@dataclass
class ChallengeState:
    detected: bool
    provider: Optional[str] = None
    requires_manual_action: bool = False
    url: str = ""
    title: str = ""
    http_status: Optional[int] = None
    evidence: List[str] = field(default_factory=list)
    integration_detected: bool = False
    active_visible: bool = False
    blocking: bool = False
    completed: bool = False
    state: str = 'NONE'
    visible_element_count: int = 0
    frame_count: int = 0
    page_id: Optional[str] = None

    def __post_init__(self):
        if self.detected and self.state == 'NONE':
            self.state = 'DETECTED_VISIBLE'

    def payload(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ChallengeWaitResult:
    initial: ChallengeState
    final: ChallengeState
    cleared: bool
    timed_out: bool
    elapsed_seconds: float


class ManualVerificationPending(RuntimeError):
    """Signals a manual gate that remains open without failing the application."""


def classify_challenge_signals(
    url: str,
    title: str,
    text: str,
    iframe_sources: List[str],
    selector_hits: List[str],
    http_status: Optional[int] = None,
) -> ChallengeState:
    haystack_url = (url or "").casefold()
    haystack_title = (title or "").casefold()
    haystack_text = " ".join((text or "").casefold().split())[:12000]
    iframe_text = " ".join(iframe_sources).casefold()
    evidence: List[str] = []
    provider: Optional[str] = None

    if any(marker in haystack_url for marker in CHALLENGE_URL_MARKERS):
        evidence.append("url_pattern")
    if any(marker in haystack_title for marker in CHALLENGE_TITLE_MARKERS):
        evidence.append("page_title")
    if any(marker in haystack_text for marker in CHALLENGE_TEXT_MARKERS):
        evidence.append("visible_text")
    if iframe_sources:
        evidence.append("challenge_iframe")
    if selector_hits:
        evidence.append("challenge_dom")
    if http_status in {403, 429, 503} and (evidence or "cloudflare" in haystack_text):
        evidence.append("http_status_%d" % http_status)

    combined = " ".join((haystack_url, haystack_title, haystack_text, iframe_text, " ".join(selector_hits)))
    if "cloudflare" in combined or "turnstile" in combined or "cdn-cgi" in combined:
        provider = "cloudflare"
    elif "recaptcha" in combined or "g-recaptcha" in combined:
        provider = "recaptcha"
    elif "hcaptcha" in combined or "h-captcha" in combined:
        provider = "hcaptcha"
    elif evidence:
        provider = "unknown"

    # A bare 403/429/503 is not enough: it may be an ordinary site/API failure.
    # Incidental URL/body strings and passive integrations are not a blocking gate.
    detected = bool(iframe_sources or selector_hits or ('page_title' in evidence and 'visible_text' in evidence))
    return ChallengeState(
        detected=detected,
        provider=provider if detected else None,
        requires_manual_action=detected,
        url=url or "",
        title=title or "",
        http_status=http_status,
        evidence=evidence if detected else [],
        active_visible=detected,
        blocking=detected,
        visible_element_count=len(selector_hits), frame_count=len(iframe_sources),
    )


class ChallengeDetector:
    """Recognizes human-verification interstitials without interacting with them."""

    async def detect(self, page: Any, http_status: Optional[int] = None, include_pending: bool = False) -> ChallengeState:
        if hasattr(page, 'is_closed') and page.is_closed():
            from app.browser.manager import BrowserSessionLostError
            raise BrowserSessionLostError('page_closed_during_verification')
        async def active(locator: Any) -> bool:
            try:
                if not await locator.is_visible():
                    return False
                passive = await locator.evaluate("""(el, includePending) => {
                    const visible = node => {
                      if (!node) return false;
                      const style=getComputedStyle(node), box=node.getBoundingClientRect();
                      return style.display!=='none' && style.visibility!=='hidden'
                        && style.opacity!=='0' && box.width>1 && box.height>1;
                    };
                    if (el.closest('.grecaptcha-badge, [data-size="invisible"]')) return true;
                    if ((el.getAttribute('src') || '').includes('size=invisible')) return true;
                    const src=el.getAttribute('src') || '';
                    const checkboxFrame = /hcaptcha/i.test(src) && (/frame=checkbox|type=checkbox/i.test(src) || /checkbox/i.test(el.title || ''));
                    const anchor = src.includes('/anchor') || checkboxFrame || el.querySelector('iframe[src*="/anchor"]');
                    if (anchor) {
                      if (!includePending) return true;
                      const token = document.querySelector('textarea[name="g-recaptcha-response"]');
                      if (token && token.value) return true;
                    }
                    for (let n=el; n; n=n.parentElement) {
                        if (getComputedStyle(n).opacity === '0') return true;
                    }
                    const rect=el.getBoundingClientRect();
                    if (rect.bottom<=0 || rect.right<=0 || rect.top>=innerHeight || rect.left>=innerWidth) return true;
                    const genericContainer = !el.matches('iframe, input, button, [role="checkbox"], [role="dialog"]')
                      && /captcha|challenge|turnstile/i.test((el.id || '') + ' ' + (el.className || ''));
                    if (genericContainer) {
                      const frames=Array.from(el.querySelectorAll('iframe'));
                      if (frames.length && !frames.some(visible)) return true;
                      const interactive=Array.from(el.querySelectorAll('input:not([type="hidden"]),button,[role="checkbox"],[role="dialog"]')).some(visible);
                      const challengeText=/verify you are human|security check|not a robot|captcha|weryfikacj/i.test(el.innerText || '');
                      if (!interactive && !challengeText && !frames.some(visible)) return true;
                    }
                    return false;
                }""", include_pending)
                if passive:
                    return False
                box = await locator.bounding_box()
                return bool(box and box.get("width", 0) > 1 and box.get("height", 0) > 1)
            except Exception:
                return False

        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            body_text = await page.locator("body").inner_text(timeout=2000)
        except Exception:
            body_text = ""
        iframe_sources: List[str] = []
        for selector in ('iframe[src*="captcha" i]', 'iframe[src*="recaptcha" i]',
                         'iframe[src*="hcaptcha" i]', 'iframe[src*="challenges.cloudflare.com" i]'):
            try:
                locator = page.locator(selector)
                for index in range(min(await locator.count(), 5)):
                    frame = locator.nth(index)
                    if not await active(frame):
                        continue
                    source = await frame.get_attribute("src")
                    if source:
                        iframe_sources.append(source)
            except Exception:
                continue
        selector_hits: List[str] = []
        for selector in CHALLENGE_SELECTORS:
            try:
                locator = page.locator(selector)
                count = await locator.count()
                if count and any([await active(locator.nth(index)) for index in range(min(count, 3))]):
                    selector_hits.append(selector)
            except Exception:
                continue
        result = classify_challenge_signals(
            getattr(page, "url", ""), title, body_text,
            iframe_sources, selector_hits, http_status,
        )
        integration_selectors = (
            'script[src*="captcha" i], script[src*="recaptcha" i], script[src*="hcaptcha" i], '
            'iframe[src*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i], '
            '.grecaptcha-badge, .h-captcha, .g-recaptcha, [data-sitekey], [class*="cf-turnstile" i]'
        )
        result.integration_detected = bool(await page.locator(integration_selectors).count())
        response_nodes = page.locator(
            'textarea[name="g-recaptcha-response"], textarea[name="h-captcha-response"], '
            'input[name="cf-turnstile-response"]'
        )
        result.completed = bool(
            result.integration_detected and not result.detected
            and await response_nodes.evaluate_all(
                "nodes => nodes.some(node => String(node.value || '').trim().length > 0)"
            )
        )
        if result.integration_detected:
            event(logging.getLogger(__name__), 'captcha.integration_detected', provider=result.provider)
        event(
            logging.getLogger(__name__), 'captcha.challenge_visibility',
            integration_detected=result.integration_detected,
            active_visible=result.active_visible,
            evidence=result.evidence,
        )
        event(
            logging.getLogger(__name__), 'captcha.active',
            active=result.detected, blocking=result.blocking, provider=result.provider,
        )
        return result

    async def wait_until_clear(
        self,
        page: Any,
        timeout_seconds: float,
        poll_seconds: float,
        http_status: Optional[int] = None,
        include_pending: bool = False,
    ) -> ChallengeWaitResult:
        started = time.monotonic()
        initial = await asyncio.wait_for(self.detect(page, http_status, include_pending), timeout=max(.1, timeout_seconds))
        current = initial
        if not initial.detected:
            return ChallengeWaitResult(initial, current, True, False, 0.0)
        initial.state = 'WAITING_FOR_USER'
        from app.services.diagnostics import emit
        emit('challenge.transition', state='WAITING_FOR_USER', url=initial.url, evidence=initial.evidence)
        while time.monotonic() - started < timeout_seconds:
            remaining = timeout_seconds - (time.monotonic() - started)
            await asyncio.sleep(min(max(.01, poll_seconds), max(0, remaining)))
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                break
            try:
                current = await asyncio.wait_for(self.detect(page, http_status, include_pending), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if current.url and initial.url and current.url != initial.url:
                emit('challenge.transition', state='NAVIGATION_CHANGED', url=current.url)
            if not current.detected:
                if hasattr(page, 'evaluate'):
                    remaining = timeout_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                    try:
                        ready = await asyncio.wait_for(page.evaluate("document.readyState"), timeout=min(remaining, 2))
                    except asyncio.TimeoutError:
                        break
                    if ready == 'loading':
                        continue
                current.state = 'SOLVED'
                emit('challenge.transition', state='SOLVED', url=current.url)
                return ChallengeWaitResult(
                    initial, current, True, False, time.monotonic() - started,
                )
        current.state = 'TIMEOUT'
        emit('challenge.transition', 'WARNING', state='TIMEOUT', url=current.url)
        return ChallengeWaitResult(
            initial, current, False, True, time.monotonic() - started,
        )
