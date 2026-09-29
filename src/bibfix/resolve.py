"""Turn an identifier (DOI, arXiv ID, DBLP key) into a BibTeX entry.

DOIs and arXiv IDs resolve through doi.org content negotiation (arXiv papers
have DataCite DOIs 10.48550/arXiv.<id>), so `add` works even when DBLP is
rate-limiting. DBLP keys (e.g. conf/nips/VaswaniSPUJGKP17) use DBLP itself.
"""

from __future__ import annotations

import re

import requests


_DOI_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)?(10\.\d{4,9}/\S+)$", re.IGNORECASE)
_ARXIV_RE = re.compile(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:)?(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?:\.pdf)?$", re.IGNORECASE)
_DBLP_RE = re.compile(r"^(?:https?://dblp\.[a-z\-.]+/rec/)?((?:conf|journals|books|phd|series|reference|tr|informal)/\S+?)(?:\.html|\.bib)?$")


class ResolveError(RuntimeError):
    pass


def kind_of(identifier: str) -> tuple[str, str]:
    s = identifier.strip()
    if m := _DOI_RE.match(s):
        return "doi", m.group(1)
    if m := _ARXIV_RE.match(s):
        return "arxiv", m.group(1)
    if m := _DBLP_RE.match(s):
        return "dblp", m.group(1)
    raise ResolveError(f"Not a DOI, arXiv ID, or DBLP key: {identifier!r}")


def _fetch_bibtex(identifier: str) -> str:
    kind, value = kind_of(identifier)
    if kind == "arxiv":
        kind, value = "doi", f"10.48550/arXiv.{value}"
    if kind == "doi":
        r = requests.get(f"https://doi.org/{value}", headers={"Accept": "application/x-bibtex; charset=utf-8"},
                         timeout=30, allow_redirects=True)
        if r.status_code == 404:
            raise ResolveError(f"doi.org does not know {value}")
        r.raise_for_status()
        text = r.text.strip()
    else:
        from .match import dblp_host
        r = requests.get(f"{dblp_host()}/rec/{value}.bib", params={"param": 1}, timeout=30)
        if r.status_code == 404:
            raise ResolveError(f"DBLP has no record {value}")
        r.raise_for_status()
        text = r.text.strip()
    if not text.startswith("@"):
        raise ResolveError(f"Expected BibTeX for {identifier}, got something else (bot check or error page?)")
    return text


def bibtex_for(identifier: str) -> str:
    try:
        return _fetch_bibtex(identifier)
    except requests.RequestException as exc:
        raise ResolveError(f"Could not resolve identifier ({type(exc).__name__}); try again later.") from exc
