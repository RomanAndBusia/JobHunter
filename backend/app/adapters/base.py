from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import Page

from app.browser.manager import BrowserManager
from app.models import Job


ChallengeCallback = Callable[[str, Any], Awaitable[None]]


@dataclass
class ApplicationTarget:
    page: Page
    page_id: str
    target_type: str
    target_url: str
    target_host: str


class BaseAdapter(ABC):
    name = "base"

    @abstractmethod
    def matches(self, url: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def open_application(
        self,
        manager: BrowserManager,
        page: Page,
        job: Job,
        challenge_callback: Optional[ChallengeCallback] = None,
    ) -> ApplicationTarget:
        raise NotImplementedError
