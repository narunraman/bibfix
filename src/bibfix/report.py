"""One structured view of a pipeline run, shared by the web UI and the CLI.

Each entry of the input gets exactly one record: its lookup status (where the
verdict came from) and, if a replacement was proposed, the review tier.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .pipeline import PipelineResult
from .review import EntryChange, classify
from .splitter import assemble, protect_titles

TIERS = ("review", "arxiv", "safe", "unchanged")
DEFAULT_ACCEPT = ("arxiv", "safe")  # what the UI pre-checks and `fix` applies by default


@dataclass
class EntryRecord:
    key: str
    status: str                 # pipeline.EntryStatus
    detail: str = ""
    change: EntryChange | None = None

    @property
    def tier(self) -> str | None:
        return self.change.tier if self.change else None


@dataclass
class Report:
    entries: list[EntryRecord]
    rebiber_converted: int
    dblp_attempted: int
    notes: list[str] = field(default_factory=list)
    duplicates: list[list[str]] = field(default_factory=list)  # groups of keys with the same title

    def by_tier(self, tier: str) -> list[EntryRecord]:
        return [e for e in self.entries if e.tier == tier]

    def by_status(self, *statuses: str) -> list[EntryRecord]:
        return [e for e in self.entries if e.status in statuses and e.change is None]

    @property
    def proposed(self) -> list[EntryRecord]:
        return [e for e in self.entries if e.tier in ("review", "arxiv", "safe")]

    def counts(self) -> dict[str, int]:
        c = {t: len(self.by_tier(t)) for t in TIERS}
        c["not_found"] = len(self.by_status("not_found"))
        c["elsewhere"] = len(self.by_status("elsewhere"))
        c["error"] = len(self.by_status("error"))
        c["skipped"] = len(self.by_status("skipped"))
        c["ok"] = len([e for e in self.entries if e.change is None and e.status == "same"])
        c["duplicates"] = len(self.duplicates)
        c["total"] = len(self.entries)
        return c

    def to_json(self) -> dict:
        def rec(e: EntryRecord) -> dict:
            d = {"key": e.key, "status": e.status, "tier": e.tier, "detail": e.detail or None}
            if e.change:
                d["badges"] = e.change.badges
                d["fields"] = [asdict(fd) for fd in e.change.field_diffs if fd.status != "same"]
            return d
        return {"counts": self.counts(), "notes": self.notes, "duplicates": self.duplicates,
                "entries": [rec(e) for e in self.entries]}


def build_report(result: PipelineResult, notes: list[str] | None = None) -> Report:
    records = []
    for key, original in result.originals_by_key.items():
        status = result.status.get(key, "skipped")
        change = None
        if key in result.replacements:
            change = classify(key, original, result.replacements[key])
        records.append(EntryRecord(key=key, status=status, detail=result.detail.get(key, ""), change=change))
    return Report(records, result.rebiber_converted, result.reffix_attempted, notes or [],
                  duplicates=find_duplicates(result.originals_by_key))


def find_duplicates(originals_by_key: dict[str, str]) -> list[list[str]]:
    """Keys whose entries have the same (normalised) title: usually one paper cited twice,
    e.g. once as the arXiv preprint and once as the published version."""
    from .dblpindex import norm_title
    from .zmap import parse_entry

    groups: dict[str, list[str]] = {}
    for key, text in originals_by_key.items():
        e = parse_entry(text) or {}
        t = norm_title(e.get("title", ""))
        if len(t) >= 12:  # skip empty/very short titles ("Introduction", "Preface")
            groups.setdefault(t, []).append(key)
    return [keys for keys in groups.values() if len(keys) > 1]


def apply(original: str, replacements: dict[str, str], accepted: set[str]) -> str:
    """The original file with accepted replacements substituted in."""
    return assemble(original, {k: protect_titles(v) for k, v in replacements.items() if k in accepted})
