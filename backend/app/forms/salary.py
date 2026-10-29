import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.config import settings
from app.schemas.api import SemanticField
from app.utils.hashing import normalized_text


@dataclass
class SalaryValue:
    amount: float
    currency: str
    period: str
    gross_net: Optional[str]
    option_id: Optional[str] = None


def parse_salary(text: str, option_id: Optional[str] = None) -> Optional[SalaryValue]:
    normalized = normalized_text(text).replace(" ", "")
    match = re.search(r"(?<!\d)(\d{1,3}(?:[.,]\d{3})+|\d{2,6})(?!\d)", normalized)
    if not match:
        return None
    raw = match.group(1)
    amount = float(raw.replace(".", "").replace(",", ""))
    source = text.casefold()
    currency = "PLN" if any(value in source for value in ("pln", "zł", "zl")) else "EUR" if "eur" in source or "€" in source else "USD" if "usd" in source or "$" in source else "UNKNOWN"
    period = "hour" if any(value in source for value in ("hour", "hourly", "/h", "godz")) else "year" if any(value in source for value in ("year", "annual", "rocz")) else "month"
    gross_net = "gross" if any(value in source for value in ("gross", "brutto")) else "net" if any(value in source for value in ("net", "netto")) else None
    return SalaryValue(amount, currency, period, gross_net, option_id)


class SalaryPolicyEngine:
    EXPERIENCE_TEXT = "My main priority is gaining relevant professional experience, so I am flexible regarding compensation and open to the employer's entry-level range."

    def choose_option(self, field: SemanticField) -> Optional[str]:
        parsed = [value for value in (parse_salary(option.label, option.id) for option in field.options) if value]
        if not parsed:
            return None
        compatible = [item for item in parsed if item.currency != "UNKNOWN"] or parsed
        return min(compatible, key=lambda item: item.amount).option_id

    def answer(self, field: SemanticField, job: Dict[str, Any]) -> Optional[Any]:
        question = normalized_text(field.question)
        if not any(token in question for token in ("salary", "wynagrod", "rate", "stawka", "compensation")):
            return None
        if field.options:
            return self.choose_option(field)
        if field.type in {"textarea", "text"} and settings.salary_experience_text_enabled and field.type != "number":
            numeric_only = any(token in question for token in ("number", "numeric", "amount", "kwota"))
            if not numeric_only:
                return self.EXPERIENCE_TEXT
        listing = " ".join(str(value) for value in (job.get("salary"), job.get("salary_range"), job.get("description")) if value)
        parsed = parse_salary(listing)
        if parsed:
            return str(int(parsed.amount)) if parsed.amount.is_integer() else str(parsed.amount)
        fallback = settings.salary_fallback_hourly_pln if "hour" in question or "godz" in question else settings.salary_fallback_monthly_pln
        return fallback or None
