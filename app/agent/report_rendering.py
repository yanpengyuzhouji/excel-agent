"""Passive Markdown report rendering; values are supplied by publication code."""

import html
from pathlib import Path
import re

CSS = Path(__file__).with_name("report.css").read_text(encoding="utf-8")
INLINE = re.compile(r"\\([\\`*_\[\]<>&|])|`([^`\n]+)`|\*\*([^*\n]+)\*\*|__([^_\n]+)__|(?<!\w)\*([^*\n]+)\*(?!\w)")
SEPARATOR = re.compile(r"^:?-{3,}:?$")
NUMBER = re.compile(r"^[+\-−]?[\d,]+(?:\.\d+)?\s*(?:%|元|万元|亿元|条|台|kVA|mm|mm2)?$")


def inline(text):
    out, start = [], 0
    for match in INLINE.finditer(text):
        out.append(html.escape(text[start:match.start()]))
        escaped, code, strong, bold, emphasis = match.groups()
        value = html.escape(next(v for v in match.groups() if v is not None))
        if escaped is not None:
            out.append(value)
        else:
            tag = "code" if code is not None else "strong" if strong is not None or bold is not None else "em"
            out.append(f"<{tag}>{value}</{tag}>")
        start = match.end()
    out.append(html.escape(text[start:]))
    return "".join(out)


def cells(line):
    return [part.strip() for part in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def render_html(text, title):
    lines = text.splitlines()
    out, index, first_content = [], 0, True
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        heading = re.match(r"^\s*(#{1,6})\s+(.+)$", line)
        if heading:
            level = min(6, max(2, len(heading[1])))
            name = heading[2].strip().rstrip("#").strip()
            if not (first_content and name.strip("*` ") == title.strip()):
                out.append(f"<h{level}>{inline(name)}</h{level}>")
            first_content = False
            index += 1
        elif stripped.startswith("```"):
            code = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            index += 1
            first_content = False
        elif "|" in line and index + 1 < len(lines) and all(SEPARATOR.fullmatch(c) for c in cells(lines[index + 1])):
            header = cells(line)
            out.append('<div class="table-wrap"><table><thead><tr>' + "".join("<th>" + inline(c) + "</th>" for c in header) + "</tr></thead><tbody>")
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                row = cells(lines[index])
                out.append("<tr>" + "".join(('<td class="number">' if NUMBER.fullmatch(c) else "<td>") + inline(c) + "</td>" for c in row) + "</tr>")
                index += 1
            out.append("</tbody></table></div>")
            first_content = False
        elif re.match(r"^\s*>\s?", line):
            quote = []
            while index < len(lines) and re.match(r"^\s*>\s?", lines[index]):
                quote.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            out.append("<blockquote><p>" + "<br>".join(inline(s) for s in quote) + "</p></blockquote>")
            first_content = False
        elif re.match(r"^\s*(?:[-+*]|\d+[.)])\s+\S", line):
            ordered = bool(re.match(r"^\s*\d+[.)]\s+", line))
            pattern = r"^\s*\d+[.)]\s+(.+)$" if ordered else r"^\s*[-+*]\s+(.+)$"
            tag = "ol" if ordered else "ul"
            # Preserve the supplied start number; do not silently renumber data.
            start = re.match(r"^\s*(\d+)", line) if ordered else None
            out.append(f'<{tag} start="{start[1]}">' if start else f"<{tag}>")
            while index < len(lines):
                item = re.match(pattern, lines[index])
                if not item:
                    break
                number = re.match(r"^\s*(\d+)", lines[index]) if ordered else None
                opening = f'<li value="{number[1]}">' if number else "<li>"
                out.append(opening + inline(item[1]) + "</li>")
                index += 1
            out.append(f"</{tag}>")
            first_content = False
        elif re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", stripped):
            out.append("<hr>")
            index += 1
            first_content = False
        else:
            out.append("<p>" + inline(line) + "</p>")
            index += 1
            first_content = False
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + html.escape(title) + '</title><style>' + CSS + '</style></head>'
            '<body class="report-document"><main class="report-shell"><header class="report-header">'
            '<p class="report-eyebrow">数据分析报告</p><h1>' + html.escape(title) + '</h1></header>'
            '<article>' + "".join(out) + '</article></main></body></html>')
