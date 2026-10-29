from dataclasses import dataclass
from typing import Any, Dict, Optional

from playwright.async_api import Page

from app.ai.provider import OpenAIResponsesProvider, ProviderUnavailable
from app.forms.sanitize import sanitize_html
from app.schemas.api import SemanticForm
from app.utils.hashing import stable_hash


@dataclass
class FallbackResult:
    level: int
    analysis: Dict[str, Any]
    input_chars: int
    cache_hit: bool = False


class FormFallbackAnalyzer:
    """Escalates compact context before larger sanitized HTML, with per-state caching."""

    _cache: Dict[str, FallbackResult] = {}

    def __init__(self, provider: OpenAIResponsesProvider):
        self.provider = provider

    async def analyze(self, page: Page, semantic_form: SemanticForm, problem_field: Optional[str] = None) -> FallbackResult:
        contexts = []
        semantic_payload = semantic_form.model_dump()
        if problem_field:
            semantic_payload["problem_field"] = problem_field
        contexts.append((2, {"semantic_form": semantic_payload}))
        html = await page.content()
        contexts.append((3, {"semantic_form": semantic_payload, "html": sanitize_html(html, 12000, "form")}))
        contexts.append((4, {"semantic_form": semantic_payload, "html": sanitize_html(html, 30000, "form")}))
        contexts.append((5, {"semantic_form": semantic_payload, "html": sanitize_html(html, 50000)}))
        last = FallbackResult(5, {"actions": [], "confidence": 0.0, "reason": "fallback unavailable"}, 0)
        for level, context in contexts:
            key = stable_hash({"level": level, "context": context})
            if key in self._cache:
                cached = self._cache[key]
                return FallbackResult(cached.level, cached.analysis, cached.input_chars, True)
            try:
                analysis = await self.provider.interpret_form_fragment(context)
            except ProviderUnavailable:
                return FallbackResult(level, {"actions": [], "confidence": 0.0, "reason": "AI provider is not configured"}, len(str(context)))
            result = FallbackResult(level, analysis, len(str(context)))
            self._cache[key] = result
            last = result
            if float(analysis.get("confidence", 0)) >= 0.8 and analysis.get("actions"):
                return result
        return last

