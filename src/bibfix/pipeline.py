"""Two-phase bibfix pipeline: rebiber (local DB, fast) then DBLP via reffix (slow).

Phase 1: run rebiber on the full file. Parse its stdout to learn which entry
keys were converted (rebiber prints `Converted. ID: <key> ; Title: ...`).
For converted keys, take rebiber's reformatted entry as the fixed version.

Phase 2: for the leftover entries (those rebiber didn't touch), look each one
up on DBLP (see match.py). A small thread pool overlaps the round trips;
reffix paces the actual requests. Every entry gets a status, so callers can
report entries DBLP could not find as well as the ones it changed.

Final output: walk the original file, substituting each entry with its fixed
version (rebiber's, then DBLP's, else the original).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from .crossref import lookup as crossref_lookup
from .match import match_entry, reset_dblp_state
from .splitter import assemble, find_entry_spans, protect_titles


DBLP_WORKERS = 4
REBIBER_TIMEOUT = 120

REBIBER_CONVERTED_RE = re.compile(r"^Converted\. ID:\s*(\S+)\s*;", re.MULTILINE)

# Where each entry's verdict came from:
#   rebiber     matched in rebiber's proceedings database (and rewritten)
#   updated     found on DBLP, proposed entry differs from the original
#   same        found on DBLP, already equivalent
#   elsewhere   not on DBLP (or DBLP unreachable) but Crossref has a matching work;
#               normal for economics, statistics and other non-CS venues
#   not_found   neither DBLP nor Crossref has this title with an overlapping author
#   error       DBLP lookup failed and Crossref didn't find it either: unverified
#   skipped     not looked up (no title/authors, or lookups disabled)
EntryStatus = Literal["rebiber", "updated", "same", "elsewhere", "not_found", "error", "skipped"]


@dataclass
class PipelineResult:
    fixed: str
    rebiber_converted: int
    reffix_attempted: int
    reffix_changed: int
    # per-entry proposed replacements + original entry text by key,
    # so the review UI can classify and let the user accept/reject.
    replacements: dict[str, str] = field(default_factory=dict)
    originals_by_key: dict[str, str] = field(default_factory=dict)
    status: dict[str, EntryStatus] = field(default_factory=dict)
    detail: dict[str, str] = field(default_factory=dict)


def _venv_bin(name: str) -> str | None:
    candidate = Path(sys.executable).parent / name
    if candidate.exists():
        return str(candidate)
    return shutil.which(name)


def _run_rebiber(source: str) -> tuple[dict[str, str], set[str]]:
    """Run rebiber on the full source. Returns (entries_by_key, converted_keys)
    where entries_by_key contains every entry from the rebiber output keyed by
    bib key, and converted_keys is the subset rebiber reports as converted.
    """
    rebiber = _venv_bin("rebiber")
    if rebiber is None:
        raise RuntimeError("rebiber is not installed in this environment.")

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        in_path = d / "in.bib"
        out_path = d / "out.bib"
        in_path.write_text(source, encoding="utf-8")

        try:
            proc = subprocess.run(
                [rebiber, "-i", str(in_path), "-o", str(out_path)],
                capture_output=True,
                text=True,
                timeout=REBIBER_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"rebiber did not finish within {REBIBER_TIMEOUT}s; input was not changed.") from exc

        if proc.returncode:
            raise RuntimeError(f"rebiber failed (exit {proc.returncode}); input was not changed.")

        if not out_path.exists():
            raise RuntimeError("rebiber produced no bibliography output; input was not changed.")

        rebibered = out_path.read_text(encoding="utf-8")

    # rebiber prints to stdout; capture converted keys
    converted = set(REBIBER_CONVERTED_RE.findall(proc.stdout + proc.stderr))

    # index rebiber output by key
    entries_by_key: dict[str, str] = {}
    for span in find_entry_spans(rebibered):
        entries_by_key[span.key] = span.text(rebibered)

    return entries_by_key, converted


ProgressCallback = Callable[[str, dict], None]


def run_pipeline(
    source: str,
    *,
    replace_arxiv: bool = False,
    skip_reffix: bool = False,
    progress: ProgressCallback | None = None,
) -> PipelineResult:
    """Run the two-phase pipeline. `progress` is called with (phase, info):
    - ("rebiber_start", {})
    - ("rebiber_done", {"converted": N})
    - ("reffix_start", {"total": K})
    - ("reffix_tick", {"completed": i, "total": K})
    - ("reffix_done", {"changed": N})
    """
    notify = progress or (lambda *_: None)

    spans = find_entry_spans(source, strict=True)
    keys = [s.key for s in spans]
    if repeated := sorted({k for k in keys if keys.count(k) > 1}):
        raise ValueError(f"citation key used by more than one entry: {', '.join(repeated)}")
    keyed_originals = {s.key: s.text(source) for s in spans}
    status: dict[str, EntryStatus] = {}
    detail: dict[str, str] = {}
    reset_dblp_state()

    if not spans:
        return PipelineResult(source, 0, 0, 0)

    # --- phase 1: rebiber ---
    notify("rebiber_start", {})
    rebiber_entries, converted_keys = _run_rebiber(source)

    replacements: dict[str, str] = {}
    for key in converted_keys:
        if key in rebiber_entries and key in keyed_originals:
            if not replace_arxiv:
                from .zmap import is_preprint, parse_entry
                if (is_preprint(parse_entry(keyed_originals[key]) or {})
                        and not is_preprint(parse_entry(rebiber_entries[key]) or {})):
                    continue
            replacements[key] = rebiber_entries[key]
            status[key] = "rebiber"
    notify("rebiber_done", {"converted": len(replacements)})

    # --- phase 2: DBLP on leftovers (skippable) ---
    leftover_keys = [s.key for s in spans if s.key not in status]
    if skip_reffix:
        for key in leftover_keys:
            status[key] = "skipped"
            detail[key] = "DBLP pass disabled"
    total = 0 if skip_reffix else len(leftover_keys)
    notify("reffix_start", {"total": total})

    changed = 0
    if total:
        with ThreadPoolExecutor(max_workers=DBLP_WORKERS) as pool:
            futures = [
                pool.submit(match_entry, key, keyed_originals[key], replace_arxiv=replace_arxiv)
                for key in leftover_keys
            ]
            completed = 0
            for fut in as_completed(futures):
                m = fut.result()
                status[m.key] = m.status
                if m.detail:
                    detail[m.key] = m.detail
                if m.status == "updated" and m.proposed:
                    replacements[m.key] = m.proposed
                    changed += 1
                completed += 1
                notify("reffix_tick", {"completed": completed, "total": total})

        # --- phase 3: second opinion from Crossref for anything DBLP couldn't vouch for ---
        # (sequential: Crossref's public pool is rate-limited; crossref.py backs off on 429)
        for key in [k for k in leftover_keys if status.get(k) in ("not_found", "error")]:
            hit = crossref_lookup(keyed_originals[key])
            if hit.found:
                status[key] = "elsewhere"
                detail[key] = hit.detail
            elif "failed" in hit.detail:
                status[key] = "error"
                detail[key] = "; ".join(x for x in (detail.get(key, ""), hit.detail) if x)
            else:
                detail[key] = (("not on DBLP; " if status[key] == "not_found" else detail.get(key, "") + "; ")
                               + hit.detail)

    notify("reffix_done", {"changed": changed})

    fixed_text = assemble(source, {k: protect_titles(v) for k, v in replacements.items()})
    return PipelineResult(
        fixed=fixed_text,
        rebiber_converted=sum(s == "rebiber" for s in status.values()),
        reffix_attempted=total,
        reffix_changed=changed,
        replacements=replacements,
        originals_by_key=keyed_originals,
        status=status,
        detail=detail,
    )
