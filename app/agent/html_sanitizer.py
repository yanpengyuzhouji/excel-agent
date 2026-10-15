"""Strict sanitizer for model-generated workbook reports.

Reports are intentionally reduced to a small, passive HTML subset. Model-generated
inline CSS is preserved after rejecting remote resources and active CSS features;
scripts and other active/remote content are never persisted.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Iterable, Tuple


MAX_REPORT_CHARS = 2_000_000

_ALLOWED_TAGS = {
    "html", "head", "meta", "title", "body", "main", "section", "article",
    "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "br", "hr", "div", "span", "strong", "em", "b", "i",
    "u", "small", "mark", "blockquote", "pre", "code", "ul", "ol",
    "li", "dl", "dt", "dd", "table", "caption", "colgroup", "col",
    "thead", "tbody", "tfoot", "tr", "th", "td", "a",
}
_VOID_TAGS = {"br", "hr", "col"}
_DROP_WITH_CONTENT = {
    "script", "iframe", "object", "embed", "svg", "math",
    "template", "noscript", "form", "button", "textarea", "select",
}
_GLOBAL_ATTRS = {"class", "id", "title", "lang", "dir"}
_TAG_ATTRS = {
    "meta": {"charset"},
    "a": {"href"},
    "ol": {"start", "reversed"},
    "li": {"value"},
    "th": {"colspan", "rowspan", "scope"},
    "td": {"colspan", "rowspan"},
    "col": {"span"},
}
_SAFE_TOKEN = re.compile(r"^[\w\-: .]{0,200}$", re.UNICODE)
_SAFE_INTEGER = re.compile(r"^[0-9]{1,4}$")
_UNSAFE_CSS = re.compile(
    r"(?is)"
    r"(?:@import\b|@font-face\b|\burl\s*\(|\bexpression\s*\(|"
    r"\b(?:javascript|vbscript|data)\s*:|-moz-binding\s*:|\bbehavior\s*:|"
    r"</?style\b)"
)


def _sanitize_inline_css(css: str) -> str:
    """Keep self-contained presentation CSS and reject active/remote CSS."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)
    if _UNSAFE_CSS.search(css) or '\\' in css or '/*' in css or '*/' in css:
        return ""
    # Do not let malformed model output escape the style element.
    return re.sub(r"(?is)</?style\b[^>]*>", "", css).strip()


class _ReportSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self._blocked_depth = 0
        self._style_depth = 0
        self._style_chunks: list[str] = []

    def handle_decl(self, decl: str) -> None:
        if not self._blocked_depth and decl.strip().lower() == "doctype html":
            self.output.append("<!DOCTYPE html>")

    def handle_starttag(
        self, tag: str, attrs: Iterable[Tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        if self._blocked_depth:
            if tag not in _VOID_TAGS:
                self._blocked_depth += 1
            return
        if self._style_depth:
            return
        if tag == "style":
            self._style_depth = 1
            self._style_chunks = []
            return
        if tag in _DROP_WITH_CONTENT:
            self._blocked_depth = 1
            return
        if tag not in _ALLOWED_TAGS:
            return

        safe_attrs: list[str] = []
        allowed = _GLOBAL_ATTRS | _TAG_ATTRS.get(tag, set())
        for raw_name, raw_value in attrs:
            name = raw_name.lower()
            value = raw_value or ""
            if name == 'style':
                safe_css = _sanitize_inline_css(value)
                if safe_css:
                    safe_attrs.append(f' style="{html.escape(safe_css, quote=True)}"')
                continue
            if name not in allowed or name.startswith("on"):
                continue
            if name == "href":
                # Reports can link to an in-document anchor only.
                if not value.startswith("#") or not _SAFE_TOKEN.fullmatch(value[1:]):
                    continue
            elif name in {"colspan", "rowspan", "span", "start", "value"}:
                if not _SAFE_INTEGER.fullmatch(value):
                    continue
            elif name == "reversed":
                value = "reversed"
            elif not _SAFE_TOKEN.fullmatch(value):
                continue
            safe_attrs.append(f' {name}="{html.escape(value, quote=True)}"')
        self.output.append(f"<{tag}{''.join(safe_attrs)}>")

    def handle_startendtag(
        self, tag: str, attrs: Iterable[Tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._style_depth:
            if tag == "style":
                css = _sanitize_inline_css("".join(self._style_chunks))
                if css:
                    self.output.append(f"<style>{css}</style>")
                self._style_depth = 0
                self._style_chunks = []
            return
        if self._blocked_depth:
            self._blocked_depth -= 1
            return
        if tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._style_depth:
            self._style_chunks.append(data)
            return
        if not self._blocked_depth:
            self.output.append(html.escape(data, quote=False))


def sanitize_report_html(content: str) -> str:
    """Return passive, self-contained report HTML or reject an oversized report."""
    if len(content) > MAX_REPORT_CHARS:
        raise ValueError(
            f"HTML report exceeds the {MAX_REPORT_CHARS}-character safety limit"
        )
    parser = _ReportSanitizer()
    parser.feed(content)
    parser.close()
    return "".join(parser.output)
