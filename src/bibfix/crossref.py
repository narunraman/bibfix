"""Second-opinion existence check against Crossref.

DBLP covers computer science only, so economics, statistics and most journal
papers are "not found" there even when they are real. Before calling an entry
unverifiable, bibfix asks Crossref (DOI-registered works, no API key).
An entry is only reported as not found if neither source knows it.

Set BIBFIX_MAILTO to an email address to use Crossref's "polite" pool (higher,
more reliable limits); without it requests go to the public pool.
"""

from __future__ import annotations

import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

import requests

from .zmap import parse_entry, split_names

API = "https://api.crossref.org/works"
TITLE_SIMILARITY = 0.93  # normalized-title ratio needed to call it the same work
# Crossref's public pool is tightly rate-limited: send one request at a time
# and honour Retry-After on 429. The mailto ("polite") pool is more generous.
_one_at_a_time = threading.Lock()
PREFERRED_TYPES = ("journal-article", "proceedings-article", "book", "book-chapter", "monograph")


@dataclass
class CrossrefHit:
    found: bool
    detail: str = ""
    doi: str = ""


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[{}\\]", "", s).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _surnames(names: list[str]) -> set[str]:
    return {_norm(n).split()[-1] for n in names if _norm(n)}


def lookup(entry_text: str, *, timeout: float = 15) -> CrossrefHit:
    entry = parse_entry(entry_text)
    if not entry or not entry.get("title"):
        return CrossrefHit(False, "no title to search for")
    title = _norm(entry["title"])
    ours = {_norm(last).split()[-1] for _, last in split_names(entry.get("author", "")) if _norm(last)}

    params = {"query.bibliographic": entry["title"], "rows": 5,
              "select": "DOI,title,author,container-title,issued,type"}
    if ours:
        params["query.author"] = " ".join(sorted(ours))
    if mailto := os.environ.get("BIBFIX_MAILTO"):
        params["mailto"] = mailto
    try:
        with _one_at_a_time:
            for attempt in range(4):
                r = requests.get(API, params=params, timeout=timeout,
                                 headers={"User-Agent": "bibfix (https://github.com/narunraman/bibfix)"})
                if r.status_code != 429:
                    break
                if attempt == 3:
                    break
                try:
                    wait = float(r.headers.get("Retry-After") or 2 ** attempt)
                except ValueError:
                    wait = 2 ** attempt
                time.sleep(max(0, min(wait, 30)))
        r.raise_for_status()
        items = r.json()["message"]["items"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        return CrossrefHit(False, f"Crossref lookup failed ({code or type(exc).__name__})")

    try:
        year = int(re.sub(r"\D", "", entry.get("year", ""))[:4])
    except ValueError:
        year = None
    matches = []
    for it in items:
        cand = _norm(" ".join(it.get("title") or []))
        if not cand or SequenceMatcher(None, title, cand).ratio() < TITLE_SIMILARITY:
            continue
        theirs = _surnames([a.get("family", "") for a in it.get("author") or []])
        if ours and not (ours & theirs):
            continue  # same title, different people: not a match
        matches.append(it)
    if not matches:
        return CrossrefHit(False, "not on Crossref either")

    def rank(it):
        y = ((it.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        return (y != year if year else 0, it.get("type") not in PREFERRED_TYPES)

    best = min(matches, key=rank)
    venue = " ".join(best.get("container-title") or []) or best.get("type", "")
    y = ((best.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    where = ", ".join(str(x) for x in (venue, y) if x)
    return CrossrefHit(True, f"on Crossref: {where} (doi:{best['DOI']})", best["DOI"])
