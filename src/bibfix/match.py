"""DBLP matching for one entry, using reffix's library functions in-process.

The pipeline used to shell out to the `reffix` CLI once per entry. Calling the
library directly avoids a Python start-up per entry, shares reffix's DBLP
request pacing across worker threads, and tells us *why* an entry was left
alone (no DBLP match vs. already identical vs. request failure), which the
CLI's output file cannot.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Literal

import bibtexparser
import requests
from bibtexparser.bwriter import BibTexWriter
from reffix import utils as ut
from reffix.reffix import select_entry

from .zmap import is_preprint, parse_entry

# reffix configures root logging on import and logs every retry and traceback;
# silence it. Outcomes are reported through DblpMatch.status/detail instead.
for _name in ("reffix", "reffix.utils", "reffix.reffix"):
    logging.getLogger(_name).setLevel(logging.CRITICAL)

DblpStatus = Literal["updated", "same", "not_found", "error", "skipped"]

# dblp.org is sometimes unreachable (outages, per-IP throttling) while its
# official mirror still answers. BIBFIX_DBLP_HOST forces a host; otherwise the
# first lookup probes dblp.org and falls back to the mirror.
DBLP_HOSTS = ("https://dblp.org", "https://dblp.uni-trier.de")
PROBE_TIMEOUT = 8
# reffix's own request loop waits up to 30 s per attempt and backs off 64-1024 s
# on errors; cap it so one bad entry can't stall a run for many minutes.
REQUEST_LIMITS = dict(timeout=15, max_attempts_429=2, wait_default_time_429=10,
                      wait_multiply_factor_429=2, max_attempts_5xx=2, wait_default_time_5xx=2)
_host_lock = threading.Lock()
_dblp_host: str | None = None


def dblp_host() -> str:
    global _dblp_host
    with _host_lock:
        if _dblp_host is None:
            forced = os.environ.get("BIBFIX_DBLP_HOST", "").rstrip("/")
            _dblp_host = forced or _probe_hosts()
            _point_reffix_at(_dblp_host)
        return _dblp_host


def _probe_hosts() -> str:
    """First host that answers a search with JSON within a few seconds. If none
    does, DBLP is marked unavailable up front so a run reports that in seconds
    instead of sitting in reffix's retry backoff for every entry."""
    problems = []
    for host in DBLP_HOSTS:
        try:
            r = requests.get(f"{host}/search/publ/api", params={"q": "test", "format": "json", "h": 1},
                             timeout=PROBE_TIMEOUT)
            if r.ok and "json" in r.headers.get("content-type", ""):
                return host
            problems.append(f"{host} returned {r.status_code} {r.headers.get('content-type', '').split(';')[0]}"
                            + (" (anti-bot check)" if "html" in r.headers.get("content-type", "") else ""))
        except requests.RequestException as exc:
            problems.append(f"{host}: {type(exc).__name__}")
    _mark_unavailable("there is no local DBLP index and DBLP's API is not answering scripts ("
                      + "; ".join(problems) + "). Run `bibfix dblp-update` once: it downloads DBLP's "
                      "data dump (about 1 GB) and builds a local index, which every later check uses")
    return DBLP_HOSTS[0]


# Once DBLP answers with its anti-bot challenge page (HTML instead of JSON),
# every further query in this process will too; stop asking instead of
# spending minutes in reffix's retry backoff for each remaining entry.
_dblp_unavailable: str | None = None


def dblp_unavailable() -> str | None:
    return _dblp_unavailable


def reset_dblp_state() -> None:
    """Forget the chosen host and any outage, so the next lookup re-probes.
    Called at the start of each pipeline run (the web app is long-lived)."""
    global _dblp_host, _dblp_unavailable
    with _host_lock:
        _dblp_host = None
        _dblp_unavailable = None


def _mark_unavailable(reason: str) -> None:
    global _dblp_unavailable
    _dblp_unavailable = reason


def _point_reffix_at(host: str) -> None:
    """reffix hard-codes dblp.org for search and for record (.bib) URLs; rewrite both."""
    ut.DBLP_API = f"{host}/search/publ/api"

    request = ut._request_dblp
    if not getattr(request, "_bibfix_wrapped", False):
        def limited(url, **kw):
            return request(url, **{**kw, **REQUEST_LIMITS})
        limited._bibfix_wrapped = True
        ut._request_dblp = limited

    original = ut._derive_bib_url
    if getattr(original, "_bibfix_wrapped", False):
        original = original.__wrapped__

    def derive(hit, bibtex_format="standard"):
        url = original(hit, bibtex_format=bibtex_format)
        return url and re.sub(r"^https?://(www\.)?dblp\.(org|uni-trier\.de)", host, url)

    derive._bibfix_wrapped = True
    derive.__wrapped__ = original
    ut._derive_bib_url = derive


_index_lock = threading.Lock()
_index_cache: list = []  # [(path, mtime_ns, size), DblpIndex | None]


def _local_index():
    """The local DBLP index (see dblpindex.py) if it has been built, else None."""
    from .dblpindex import DblpIndex, db_path
    with _index_lock:
        path = db_path()
        try:
            stat = path.stat()
            signature = (path, stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            signature = (path, None, None)
        if not _index_cache or _index_cache[0] != signature:
            _index_cache[:] = [signature, DblpIndex(path) if signature[1] is not None else None]
        return _index_cache[1]


@dataclass
class DblpMatch:
    key: str
    status: DblpStatus
    proposed: str | None = None  # new entry text when status == "updated"
    detail: str = ""


def _write(entry: dict) -> str:
    db = bibtexparser.bibdatabase.BibDatabase()
    db.entries = [entry]
    writer = BibTexWriter()
    writer.indent = "  "
    writer.align_values = True
    writer.order_entries_by = None
    return bibtexparser.dumps(db, writer=writer).strip()


def _norm(entry: dict) -> dict[str, str]:
    skip = {"timestamp", "bibsource", "biburl"}
    return {
        k: re.sub(r"\s+", " ", re.sub(r"[{}]", "", str(v))).strip().lower()
        for k, v in entry.items()
        if k not in skip
    }


def match_entry(key: str, entry_text: str, *, replace_arxiv: bool) -> DblpMatch:
    orig = parse_entry(entry_text)
    if orig is None:
        return DblpMatch(key, "skipped", detail="could not parse entry")
    if not orig.get("title"):
        return DblpMatch(key, "skipped", detail="no title")
    try:
        if not ut.get_authors_canonical(orig):
            return DblpMatch(key, "skipped", detail="no authors")
    except Exception:
        return DblpMatch(key, "skipped", detail="could not read authors")

    index = _local_index()
    if index is not None:
        rows = index.by_title(orig["title"])
        # reffix's get_best_entry falls back to the *last* candidate: order so that
        # preprints come first and the newest published record comes last.
        rows = sorted(rows, key=lambda r: (r["journal"] != "CoRR", r["year"]))
        candidates = [index.as_entry(r) for r in rows]
        return _choose(key, orig, candidates, replace_arxiv, source="local DBLP index")

    dblp_host()
    if _dblp_unavailable:
        return DblpMatch(key, "error", detail=_dblp_unavailable)
    try:
        candidates = ut.get_dblp_results(ut.build_dblp_query(orig), bibtex_format="standard")
    except requests.exceptions.JSONDecodeError:
        _mark_unavailable("DBLP is serving its anti-bot check instead of results (rate-limited); try again later")
        return DblpMatch(key, "error", detail=_dblp_unavailable)
    except Exception as exc:  # noqa: BLE001 — network errors are per-entry, not fatal
        return DblpMatch(key, "error", detail=f"{type(exc).__name__}: {exc}")
    if candidates is None:  # reffix already retried with backoff
        _mark_unavailable("DBLP did not respond after several retries; try again later")
        return DblpMatch(key, "error", detail=_dblp_unavailable)

    return _choose(key, orig, candidates, replace_arxiv, source="DBLP API")


def _choose(key: str, orig: dict, candidates: list[dict], replace_arxiv: bool, *, source: str) -> DblpMatch:
    # select_entry only accepts candidates whose title matches exactly (ignoring
    # case/punctuation) and that share at least one author with the original.
    preprint = is_preprint(orig)
    if preprint and not replace_arxiv:
        candidates = [entry for entry in candidates if is_preprint(entry)]
    elif not preprint:
        candidates = [entry for entry in candidates if not is_preprint(entry)]
    best = select_entry(candidates, orig_entry=dict(orig), replace_arxiv=replace_arxiv or not preprint)
    if best is None:
        why = "no record with this title" if not candidates else f"{len(candidates)} with this title, no shared author"
        return DblpMatch(key, "not_found", detail=f"{source}: {why}")

    best = dict(best)
    best["ID"] = orig["ID"]
    best = ut.preserve_original_authors(orig, best)
    best["title"] = ut.protect_titlecase(best["title"])
    best = ut.clean_entry(best)

    if _norm(best) == _norm(orig):
        return DblpMatch(key, "same")
    return DblpMatch(key, "updated", proposed=_write(best))
