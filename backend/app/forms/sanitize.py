from typing import Optional

from bs4 import BeautifulSoup, Comment


REMOVE_TAGS = {"script", "style", "svg", "canvas", "noscript", "footer", "nav"}
KEEP_ATTRS = {
    "id", "name", "type", "role", "for", "value", "placeholder", "required", "disabled",
    "aria-label", "aria-labelledby", "aria-describedby", "aria-required", "aria-invalid",
    "aria-expanded", "aria-controls", "aria-selected", "autocomplete", "maxlength", "multiple",
    "data-testid", "data-test",
}


def sanitize_html(html: str, max_chars: int = 30000, selector: Optional[str] = None) -> str:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one(selector) if selector else soup
    if root is None:
        root = soup
    for tag in root.find_all(REMOVE_TAGS):
        tag.decompose()
    for comment in root.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for tag in root.find_all(True):
        for attr in list(tag.attrs):
            if attr not in KEEP_ATTRS:
                del tag.attrs[attr]
        if tag.has_attr("value") and len(str(tag["value"])) > 500:
            tag["value"] = str(tag["value"])[:500]
    compact = " ".join(str(root).split())
    return compact[:max_chars]

