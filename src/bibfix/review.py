"""Classify a proposed entry change as review-required, safe, or unchanged
and compute a per-field diff for rendering.

We parse each entry with bibtexparser, normalize values (strip braces,
collapse whitespace, lowercase) for comparison, and apply a few heuristics:

  - arxiv→pub: original looked like an arxiv preprint, proposed has a real
    venue. Classified separately so callers can decide whether to accept it.
  - title or author surnames diverge: flagged for review.
  - only new fields added (no existing values changed): "safe".
  - all normalized fields match the original: "unchanged" (we drop the
    proposal — it's just reformatting).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Literal

from .zmap import parse_entry


Tier = Literal["review", "arxiv", "safe", "unchanged"]
FieldStatus = Literal["same", "changed", "added", "removed"]


@dataclass
class FieldDiff:
    name: str
    original: str
    proposed: str
    status: FieldStatus


@dataclass
class EntryChange:
    key: str
    tier: Tier
    badges: list[str] = dc_field(default_factory=list)
    field_diffs: list[FieldDiff] = dc_field(default_factory=list)
    original_text: str = ""
    proposed_text: str = ""


_TRIVIAL_FIELDS = {"timestamp", "bibsource", "biburl"}  # noise; ignore for classification


def _norm(s: str) -> str:
    s = re.sub(r"[{}]", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _norm_title(s: str) -> str:
    """Title comparison is more permissive than generic field comparison —
    we also treat hyphens, slashes, and underscores as whitespace so that
    `Chain-of-Thought` matches `Chain of Thought`. Trailing punctuation is
    also stripped.
    """
    s = _norm(s)
    s = re.sub(r"[\-/_]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[.,:;!?]+$", "", s)
    return s


def _norm_surnames(authors: str) -> set[str]:
    parts = re.split(r"\s+and\s+", _norm(authors))
    surnames: set[str] = set()
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if "," in p:
            surnames.add(p.split(",", 1)[0].strip())
        else:
            toks = p.split()
            if toks:
                surnames.add(toks[-1])
    return {s for s in surnames if s}


def _looks_like_arxiv(orig: dict[str, str]) -> bool:
    """True for arXiv/CoRR preprints in any of the common encodings, including
    DBLP's (`journal = {CoRR}`, `eprinttype = {arXiv}`, `volume = {abs/...}`,
    DOI prefix 10.48550)."""
    blob = " ".join(
        orig.get(k, "")
        for k in ("journal", "note", "eprint", "eprinttype", "archiveprefix", "howpublished", "publisher", "url", "doi")
    ).lower()
    if "arxiv" in blob or "preprint" in blob or "10.48550" in blob:
        return True
    if _norm(orig.get("journal", "")) == "corr" or orig.get("volume", "").lower().startswith("abs/"):
        return True
    if "arxiv" in orig.get("ID", "").lower():
        return True
    return False


def _looks_like_published(proposed: dict[str, str]) -> bool:
    """A non-preprint record: a journal article (TMLR, JMLR... often have no DOI or
    publisher in DBLP) or proceedings/book chapter with a venue."""
    if _looks_like_arxiv(proposed):
        return False
    entry_type = proposed.get("ENTRYTYPE", "").lower()
    if entry_type == "article":
        return bool(proposed.get("journal"))
    if entry_type in {"inproceedings", "incollection"}:
        return bool(proposed.get("booktitle") or proposed.get("publisher") or proposed.get("doi"))
    return False


def classify(key: str, original_text: str, proposed_text: str) -> EntryChange:
    change = EntryChange(
        key=key,
        tier="safe",
        original_text=original_text,
        proposed_text=proposed_text,
    )

    orig = parse_entry(original_text)
    prop = parse_entry(proposed_text)
    if orig is None or prop is None:
        change.tier = "review"
        change.badges.append("parse error")
        return change

    # build per-field diffs (skip the bibtexparser bookkeeping keys)
    skip = {"ID", "ENTRYTYPE"} | _TRIVIAL_FIELDS
    all_fields = (set(orig) | set(prop)) - skip
    n_changed = 0
    n_added = 0
    if orig.get("ENTRYTYPE") != prop.get("ENTRYTYPE"):
        change.field_diffs.append(FieldDiff("ENTRYTYPE", orig.get("ENTRYTYPE", ""), prop.get("ENTRYTYPE", ""), "changed"))
        n_changed += 1
    for f in sorted(all_fields):
        o = orig.get(f, "")
        p = prop.get(f, "")
        if not o and p:
            change.field_diffs.append(FieldDiff(f, "", p, "added"))
            n_added += 1
        elif o and not p:
            change.field_diffs.append(FieldDiff(f, o, "", "removed"))
        elif _norm(o) != _norm(p):
            change.field_diffs.append(FieldDiff(f, o, p, "changed"))
            n_changed += 1
        else:
            change.field_diffs.append(FieldDiff(f, o, p, "same"))

    # all-same (and no real adds/removals): unchanged
    if n_changed == 0 and n_added == 0 and not any(
        fd.status == "removed" for fd in change.field_diffs
    ):
        change.tier = "unchanged"
        return change

    # flag detection (independent of tier decision)
    is_arxiv_pub = _looks_like_arxiv(orig) and _looks_like_published(prop)
    title_changed = (
        orig.get("title") and prop.get("title")
        and _norm_title(orig["title"]) != _norm_title(prop["title"])
    )

    o_surnames = _norm_surnames(orig.get("author", ""))
    p_surnames = _norm_surnames(prop.get("author", ""))
    added_authors = p_surnames - o_surnames
    removed_authors = o_surnames - p_surnames
    # "very different" lists -> review; mere additions are usually safe.
    authors_diverge = False
    if o_surnames and p_surnames:
        overlap = len(o_surnames & p_surnames)
        if overlap == 0 or overlap < min(len(o_surnames), len(p_surnames)) / 2:
            authors_diverge = True

    type_changed = orig.get("ENTRYTYPE", "").lower() != prop.get("ENTRYTYPE", "").lower()
    # @misc{arXiv} vs DBLP's @article{journal=CoRR}: the same preprint, just encoded differently.
    if type_changed and _looks_like_arxiv(orig) and _looks_like_arxiv(prop):
        type_changed = False

    # tier decision: title drift or a wholly different author list -> review.
    # A preprint swapped for its published version -> arxiv. Any other change
    # of entry type means a different publication record was matched, which is
    # never a "safe enrichment" -> review. Author removals and substantive
    # edits to existing metadata also require review.
    both_preprints = _looks_like_arxiv(orig) and _looks_like_arxiv(prop)
    preprint_fields = {"eprint", "eprinttype", "archiveprefix", "journal", "volume", "howpublished", "note"}
    substantive_fields = [fd.name for fd in change.field_diffs
                          if fd.status in ("changed", "removed")
                          and fd.name not in {"title", "author", "ENTRYTYPE"}
                          and not (both_preprints and fd.name in preprint_fields)]
    if title_changed or authors_diverge or removed_authors:
        change.tier = "review"
    elif is_arxiv_pub:
        change.tier = "arxiv"
    elif type_changed or substantive_fields or any(fd.name == "title" and fd.status == "removed" for fd in change.field_diffs):
        change.tier = "review"

    # badges
    if is_arxiv_pub:
        change.badges.append("ARXIV→PUB")
    if title_changed:
        change.badges.append("title changed")

    # Author changes: when the entry is already in review, surface any author
    # delta with the warn-styled "authors changed" badge (more prominent);
    # otherwise stick with the subtle "+N author(s)" / "−N author(s)" badge.
    if authors_diverge:
        change.badges.append("authors changed")
    elif removed_authors or added_authors:
        if change.tier == "review":
            change.badges.append("authors changed")
        elif removed_authors and added_authors:
            change.badges.append(f"−{len(removed_authors)}/+{len(added_authors)} authors")
        elif removed_authors:
            change.badges.append(f"−{len(removed_authors)} author{'s' if len(removed_authors) != 1 else ''}")
        else:
            change.badges.append(f"+{len(added_authors)} author{'s' if len(added_authors) != 1 else ''}")

    if type_changed:
        change.badges.append(f"@{orig.get('ENTRYTYPE')} → @{prop.get('ENTRYTYPE')}")
    if change.tier == "review" and substantive_fields:
        change.badges.append("changed metadata: " + ", ".join(substantive_fields))

    # safe-tier summary badges (only when nothing more informative applies)
    if change.tier == "safe":
        if n_added and not n_changed:
            change.badges.append(f"+{n_added} field{'s' if n_added != 1 else ''}")
        elif n_changed:
            change.badges.append(f"{n_changed} edit{'s' if n_changed != 1 else ''}")

    return change
