"""Split a BibTeX source into entries while preserving file order.

We split by entry boundaries (`@type{key,...}`) using brace-counting rather
than a real parser so that whitespace, comments, and `@string`/`@preamble`
blocks between entries are preserved roughly as-is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BLOCK_START = re.compile(r"@([A-Za-z]+)\s*([({])")
_ENTRY_KEY = re.compile(r"\s*([^,\s]+)\s*,")
_SPECIAL_BLOCKS = {"comment", "string", "preamble"}


@dataclass
class EntrySpan:
    start: int  # character offset of `@`
    end: int    # character offset just past the closing delimiter
    key: str    # bib key (e.g. "vaswani2017attention")

    def text(self, source: str) -> str:
        return source[self.start : self.end]


def _escaped(text: str, index: int) -> bool:
    start = index
    while start and text[start - 1] == "\\":
        start -= 1
    return (index - start) % 2 == 1


def find_entry_spans(text: str, *, strict: bool = False) -> list[EntrySpan]:
    """Return complete real entries, preserving comments and special blocks.

    Both BibTeX delimiters are supported. Skip whole blocks so apparent entries
    inside an @comment or a quoted/braced field never become replacements.
    """
    spans: list[EntrySpan] = []
    cursor = 0
    while match := _BLOCK_START.search(text, cursor):
        # Ignore percent-commented lines outside entries.
        line_start = max(cursor, text.rfind("\n", 0, match.start()) + 1)
        if any(text[i] == "%" and not _escaped(text, i) for i in range(line_start, match.start())):
            end_line = text.find("\n", match.end())
            cursor = len(text) if end_line < 0 else end_line + 1
            continue
        root_brace = match.group(2) == "{"
        depth = int(root_brace)
        in_string = False
        quote_depth = 0
        i = match.end()
        end = None
        while i < len(text):
            ch = text[i]
            if not _escaped(text, i):
                if in_string:
                    if ch == "{":
                        quote_depth += 1
                    elif ch == "}" and quote_depth:
                        quote_depth -= 1
                    elif ch == '"' and not quote_depth:
                        in_string = False
                elif ch == '"' and depth == int(root_brace):
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if root_brace and depth == 0:
                        end = i + 1
                        break
                elif ch == ")" and not root_brace and depth == 0:
                    end = i + 1
                    break
            i += 1
        if end is None:
            if strict:
                line = text.count("\n", 0, match.start()) + 1
                raise ValueError(f"line {line}: this @{match.group(1)} entry is never closed (unbalanced braces?)")
            break  # Incomplete input is left intact, never replaced partially.
        key = _ENTRY_KEY.match(text, match.end())
        if match.group(1).lower() not in _SPECIAL_BLOCKS and key:
            spans.append(EntrySpan(match.start(), end, key.group(1)))
        elif match.group(1).lower() not in _SPECIAL_BLOCKS and strict:
            line = text.count("\n", 0, match.start()) + 1
            raise ValueError(f"line {line}: this @{match.group(1)} entry has no citation key")
        cursor = end
    return spans


def count_entries(text: str) -> int:
    return len(find_entry_spans(text))


_TITLE_FIELD_RE = re.compile(r"(\btitle\s*=\s*)\{", re.IGNORECASE)


def _balanced_braces(s: str) -> bool:
    depth = 0
    for i, c in enumerate(s):
        if c == "{" and not _escaped(s, i):
            depth += 1
        elif c == "}" and not _escaped(s, i):
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _wrap_title_in_entry(entry: str) -> str:
    """Ensure the `title = {...}` field is double-wrapped (`{{...}}`) so
    BibTeX styles don't lowercase the title. Idempotent."""
    m = _TITLE_FIELD_RE.search(entry)
    if not m:
        return entry
    open_pos = m.end() - 1  # position of `{`
    depth = 0
    i = open_pos
    close_pos = -1
    while i < len(entry):
        c = entry[i]
        if c == "{" and not _escaped(entry, i):
            depth += 1
        elif c == "}" and not _escaped(entry, i):
            depth -= 1
            if depth == 0:
                close_pos = i
                break
        i += 1
    if close_pos < 0:
        return entry  # unbalanced; leave alone

    inner = entry[open_pos + 1 : close_pos]
    # already fully double-wrapped: starts with `{`, ends with `}`, and the
    # inner content (without those) is brace-balanced.
    if inner.startswith("{") and inner.endswith("}") and _balanced_braces(inner[1:-1]):
        return entry
    new_value = "{" + inner + "}"
    return entry[: open_pos + 1] + new_value + entry[close_pos:]


def protect_titles(bib_text: str) -> str:
    """Apply title brace-protection to every entry in a bib document."""
    spans = find_entry_spans(bib_text)
    if not spans:
        return bib_text
    out: list[str] = []
    cursor = 0
    for span in spans:
        out.append(bib_text[cursor : span.start])
        out.append(_wrap_title_in_entry(span.text(bib_text)))
        cursor = span.end
    out.append(bib_text[cursor:])
    return "".join(out)


def assemble(original: str, replacements: dict[str, str]) -> str:
    """Walk the original text and substitute each entry whose key is in
    `replacements` with the replacement text. Non-entry content (comments,
    `@string`, blank lines) is preserved verbatim.
    """
    spans = find_entry_spans(original)
    out: list[str] = []
    cursor = 0
    for span in spans:
        out.append(original[cursor : span.start])
        out.append(replacements.get(span.key, span.text(original)))
        cursor = span.end
    out.append(original[cursor:])
    return "".join(out)
