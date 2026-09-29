"""Map between BibTeX entries (bibtexparser dicts) and Zotero item data.

Only the fields bibfix proposes changes to are mapped; everything else on a
Zotero item (tags, notes, collections, attachments, relations) is left as is.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import bibtexparser
from bibtexparser.bibdatabase import BibDataString
from bibtexparser.bparser import BibTexParser
from bibtexparser.customization import convert_to_unicode

ARXIV_RE = re.compile(r"arxiv|corr|10\.48550", re.IGNORECASE)

# BibTeX field -> Zotero field, per Zotero item type where it differs.
_COMMON = {
    "title": "title", "doi": "DOI", "url": "url", "volume": "volume", "number": "issue",
    "pages": "pages", "publisher": "publisher", "abstract": "abstractNote", "isbn": "ISBN",
    "issn": "ISSN", "address": "place", "series": "series",
}
_BY_TYPE = {
    "journalArticle": {"journal": "publicationTitle"},
    "conferencePaper": {"booktitle": "proceedingsTitle"},
    "bookSection": {"booktitle": "bookTitle"},
    "thesis": {"school": "university"},
    "report": {"institution": "institution"},
    "preprint": {},
}


def _expand(value) -> str:
    """A field value as text. @string macros defined elsewhere in the file are unknown
    when one entry is parsed on its own; they are kept as their names."""
    if isinstance(value, str):
        return value
    parts = []
    for part in value.expr:
        if isinstance(part, BibDataString):
            try:
                part = part.get_value()
            except KeyError:
                part = part.name
        parts.append(part)
    return "".join(parts)


def parse_entry(text: str) -> dict[str, str] | None:
    """The first entry in `text` as a bibtexparser dict of strings, or None if it can't be parsed."""
    parser = BibTexParser(common_strings=True, interpolate_strings=False, ignore_nonstandard_types=False)
    try:
        db = bibtexparser.loads(text, parser=parser)
    except Exception:
        return None
    return {k: _expand(v) for k, v in db.entries[0].items()} if db.entries else None


def _clean(value: str) -> str:
    rec = convert_to_unicode({"v": value})
    v = rec["v"].replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", v).strip()


def is_preprint(entry: dict[str, str]) -> bool:
    blob = " ".join(entry.get(k, "") for k in ("journal", "eprinttype", "archiveprefix", "publisher", "doi", "howpublished", "note", "url"))
    return bool(ARXIV_RE.search(blob)) or entry.get("volume", "").lower().startswith("abs/")


def zotero_type(entry: dict[str, str]) -> str:
    t = entry.get("ENTRYTYPE", "misc").lower()
    if t in ("inproceedings", "conference"):
        return "conferencePaper"
    if t == "article":
        return "preprint" if is_preprint(entry) else "journalArticle"
    if t == "misc":
        return "preprint" if is_preprint(entry) else "document"
    return {
        "book": "book", "incollection": "bookSection", "inbook": "bookSection",
        "phdthesis": "thesis", "mastersthesis": "thesis", "techreport": "report",
        "online": "webpage", "unpublished": "manuscript",
    }.get(t, "document")


def split_names(field: str) -> list[tuple[str, str]]:
    """BibTeX name list -> [(first, last)], splitting on top-level ' and '."""
    names, depth, cur = [], 0, ""
    tokens = re.split(r"(\s+and\s+|[{}])", field)
    for tok in tokens:
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
        if depth == 0 and re.fullmatch(r"\s+and\s+", tok or ""):
            names.append(cur)
            cur = ""
        else:
            cur += tok or ""
    names.append(cur)
    out = []
    for n in names:
        raw = n.strip()
        n = _clean(n)
        if not n:
            continue
        if raw.startswith("{") and raw.endswith("}") and "," not in n:
            out.append(("", n))  # braced corporate/institutional name: one unit
            continue
        if "," in n:
            last, first = [p.strip() for p in n.split(",", 1)]
        else:
            parts = n.split()
            first, last = " ".join(parts[:-1]), parts[-1]
        out.append((first, last))
    return out


def creators(entry: dict[str, str]) -> list[dict[str, str]]:
    out = [{"creatorType": "author", "firstName": f, "lastName": l} for f, l in split_names(entry.get("author", ""))]
    out += [{"creatorType": "editor", "firstName": f, "lastName": l} for f, l in split_names(entry.get("editor", ""))]
    return out


def fields_for(entry: dict[str, str], item_type: str) -> dict[str, str]:
    """Zotero field values carried by a BibTeX entry, for `item_type`."""
    mapping = {**_COMMON, **_BY_TYPE.get(item_type, {})}
    out = {z: _clean(entry[b]) for b, z in mapping.items() if entry.get(b)}
    if "pages" in out:
        out["pages"] = out["pages"].replace("--", "-")
    if entry.get("year"):
        out["date"] = _clean(entry["year"]) + (f"-{_clean(entry['month'])}" if entry.get("month", "").isdigit() else "")
    if item_type == "preprint" and entry.get("eprint"):
        out["archiveID"] = f"arXiv:{_clean(entry['eprint'])}"
        out["repository"] = "arXiv"
    return out


def _norm(v: Any) -> str:
    return re.sub(r"[^0-9a-z]+", "", unicodedata.normalize("NFKD", str(v)).lower())


def plan_update(data: dict, proposed: dict[str, str], valid_fields: set[str], *,
                update_creators: bool = False) -> tuple[dict, list[tuple[str, str, str]]]:
    """Return (new item data, [(field, old, new)]) applying `proposed` (a
    BibTeX entry) to Zotero item `data`. Changing item type keeps only fields
    valid for the new type. Values equal up to case/punctuation are left alone
    so DBLP's capitalisation never overwrites a curated Zotero title."""
    new_type = zotero_type(proposed)
    new = {k: v for k, v in data.items() if k not in ("key", "version")}
    changes: list[tuple[str, str, str]] = []
    if new_type != data.get("itemType"):
        changes.append(("itemType", data.get("itemType", ""), new_type))
        new = {k: v for k, v in new.items()
               if k in valid_fields or k in ("itemType", "creators", "tags", "collections", "relations", "extra", "citationKey")}
        new["itemType"] = new_type
        for k in valid_fields:
            new.setdefault(k, "")
    for field, value in fields_for(proposed, new_type).items():
        if field not in valid_fields:
            continue
        old = str(data.get(field, "") or "")
        if _norm(old) != _norm(value):
            changes.append((field, old, value))
            new[field] = value
    if update_creators:
        proposed_creators = creators(proposed)
        old_names = [(c.get("firstName", ""), c.get("lastName", c.get("name", ""))) for c in data.get("creators", [])]
        if proposed_creators and [(c["firstName"], c["lastName"]) for c in proposed_creators] != old_names:
            changes.append(("creators", "; ".join(" ".join(n).strip() for n in old_names),
                            "; ".join(f"{c['firstName']} {c['lastName']}".strip() for c in proposed_creators)))
            new["creators"] = proposed_creators
    return new, changes


_STOP = {"a", "an", "the", "of", "on", "in", "for", "and", "to", "with", "from", "at", "by", "is", "are", "do", "does"}


def make_citekey(entry: dict[str, str], taken: set[str]) -> str:
    """Better BibTeX-style default key: authorlowercase + ThreeTitleWords + year."""
    names = split_names(entry.get("author", "") or entry.get("editor", ""))
    last = names[0][1] if names else "anon"
    last = re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", last).encode("ascii", "ignore").decode().lower()) or "anon"
    words = [w for w in re.findall(r"[A-Za-z0-9]+", _clean(entry.get("title", ""))) if w.lower() not in _STOP][:3]
    base = last + "".join(w[:1].upper() + w[1:].lower() for w in words) + _clean(entry.get("year", ""))
    key, suffix = base, 0
    while key in taken:
        suffix += 1
        number, letters = suffix, ""
        while number:
            number, digit = divmod(number - 1, 26)
            letters = chr(ord("a") + digit) + letters
        key = base + letters
    return key
