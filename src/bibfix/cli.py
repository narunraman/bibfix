"""bibfix command line.

  bibfix check refs.bib [--json] [--fail-on review,not-found]   report only
  bibfix fix refs.bib --in-place [--accept arxiv,safe]           apply fixes to the file
  bibfix sync --from-tex main.tex -o refs.bib                    Zotero -> .bib for the keys the paper cites
  bibfix push refs.bib [--yes]                                   write accepted fixes back into Zotero
  bibfix add 2009.03300 10.1145/... conf/nips/X17 [--collection NAME]
  bibfix keys main.tex                                           list cited keys

Zotero commands read ZOTERO_API_KEY / ZOTERO_LIBRARY_ID (see zotero.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .report import Report

DEFAULT_ACCEPT = ("arxiv", "safe")

FAIL_CHOICES = {"review", "arxiv", "safe", "not-found", "error", "any-change", "duplicates", "undefined"}


# -- output helpers ---------------------------------------------------------
class _Style:
    def __init__(self, stream):
        self.on = stream.isatty()

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text


def _progress(stream):
    if not stream.isatty():
        return None

    def cb(event: str, info: dict) -> None:
        if event == "rebiber_start":
            stream.write("\r  matching against rebiber's proceedings database…")
        elif event == "reffix_tick":
            stream.write(f"\r  querying DBLP {info['completed']}/{info['total']}…" + " " * 30)
        elif event == "reffix_done":
            stream.write("\r" + " " * 70 + "\r")
        stream.flush()

    return cb


def _run(path: Path, args) -> tuple[str, "object", Report]:
    source = path.read_text(encoding="utf-8")
    from .match import dblp_unavailable
    from .pipeline import run_pipeline
    from .report import build_report

    result = run_pipeline(source, replace_arxiv=not args.keep_arxiv, skip_reffix=args.no_dblp,
                          progress=_progress(sys.stderr))
    notes = []
    if reason := dblp_unavailable():
        notes.append(f"DBLP lookups stopped early: {reason}.")
    return source, result, build_report(result, notes)


def _write_bib(path: Path, text: str) -> None:
    """Replace the destination only after a complete UTF-8 write succeeds."""
    destination = path.resolve() if path.is_symlink() else path
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=".bibfix-", delete=False) as handle:
            pending = Path(handle.name)
            handle.write(text)
        if destination.exists():
            os.chmod(pending, stat.S_IMODE(destination.stat().st_mode))
        pending.replace(destination)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def _field(change, name: str) -> str:
    for fd in change.field_diffs:
        if fd.name == name and fd.proposed:
            return re.sub(r"\s+", " ", fd.proposed.replace("{", "").replace("}", "")).strip()
    return ""


def _print_report(path: Path, report: Report, out=None) -> None:
    out = out or sys.stdout
    s = _Style(out)
    c = report.counts()
    out.write(f"{path}: {c['total']} entries\n")
    parts = [
        s(f"{c['ok'] + c['unchanged'] + c['elsewhere']} fine", "32"),
        s(f"{c['safe']} safe fixes", "36"),
        s(f"{c['arxiv']} arXiv→published", "34"),
        s(f"{c['review']} need review", "33;1") if c["review"] else f"{c['review']} need review",
        s(f"{c['not_found']} not found anywhere", "35;1") if c["not_found"] else f"{c['not_found']} not found anywhere",
    ]
    if c["error"]:
        parts.append(s(f"{c['error']} lookup errors", "31;1"))
    if c["skipped"]:
        parts.append(f"{c['skipped']} skipped")
    out.write("  " + " · ".join(parts) + "\n")

    def section(title, records, line):
        if records:
            out.write(f"\n{title}\n")
            for r in records:
                out.write("  " + line(r) + "\n")

    section(s("Needs review (not applied by default):", "33;1"), report.by_tier("review"),
            lambda r: f"{r.key}  [{', '.join(r.change.badges)}]")
    section(s("arXiv → published:", "34"), report.by_tier("arxiv"),
            lambda r: f"{r.key}  → {_field(r.change, 'booktitle') or _field(r.change, 'journal') or 'published version'}")
    section(s("Safe fixes:", "36"), report.by_tier("safe"),
            lambda r: f"{r.key}  [{', '.join(r.change.badges)}]")
    section(s("Not found on DBLP or Crossref (check these exist and the title/authors are right):", "35;1"),
            report.by_status("not_found"), lambda r: r.key)
    section("Verified on Crossref (not in DBLP):", report.by_status("elsewhere"),
            lambda r: f"{r.key}  {r.detail.removeprefix('on Crossref: ')}")
    errors = report.by_status("error")
    if errors and len({r.detail for r in errors}) == 1 and report.notes:
        out.write(f"\n{s(f'{len(errors)} entries were not checked against DBLP', '31')} (see note below).\n")
    else:
        section(s("Lookup errors:", "31"), errors, lambda r: f"{r.key}  {r.detail}")
    section("Not checked:", [r for r in report.by_status("skipped") if r.detail != "DBLP pass disabled"],
            lambda r: f"{r.key}  ({r.detail})")
    if report.duplicates:
        out.write(f"\n{s('Possible duplicates (same title, different keys):', '33;1')}\n")
        for keys in report.duplicates:
            out.write("  " + " = ".join(keys) + "\n")
    for note in report.notes:
        out.write(f"\n{s('note:', '33')} {note}\n")


def _accepted(report: Report, spec: str) -> set[str]:
    tiers = _accept_tiers(spec)
    return {e.key for e in report.proposed if e.tier in tiers}


def _accept_tiers(spec: str) -> set[str]:
    spec = spec.strip()
    if spec == "all":
        return {"review", "arxiv", "safe"}
    if spec == "none":
        return set()
    tiers = {value.strip() for value in spec.split(",")}
    bad = tiers - {"review", "arxiv", "safe"}
    if bad:
        raise argparse.ArgumentTypeError(f"unknown tier(s) {', '.join(sorted(bad))} (use review, arxiv, safe, all, none)")
    return tiers


def _accept_spec(spec: str) -> str:
    _accept_tiers(spec)
    return spec


def _fail_spec(spec: str) -> str:
    if not spec:
        return spec
    spec = ",".join(value.strip() for value in spec.split(","))
    bad = set(spec.split(",")) - FAIL_CHOICES
    if bad:
        raise argparse.ArgumentTypeError(f"unknown value(s) {', '.join(sorted(bad))}")
    return spec


# -- commands ---------------------------------------------------------------
def _tex_crosscheck(report: Report, tex: Path, out) -> tuple[list[str], list[str]]:
    """(cited but not in the .bib, in the .bib but never cited)."""
    from .texcites import cited_keys
    cited = cited_keys(tex)
    have = {e.key for e in report.entries}
    missing = [k for k in cited if k not in have]
    cited_set = set(cited)
    unused = [e.key for e in report.entries if e.key not in cited_set]
    s = _Style(out)
    if missing:
        out.write(f"\n{s(f'Cited in {tex.name} but not in the .bib ({len(missing)}):', '31;1')}\n  " + ", ".join(missing) + "\n")
    if unused:
        out.write(f"\n{s(f'In the .bib but not cited in {tex.name} ({len(unused)}):', '2')}\n  " + ", ".join(unused) + "\n")
    return missing, unused


def cmd_check(args) -> int:
    _, _, report = _run(args.file, args)
    if args.json:
        json.dump(report.to_json(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print_report(args.file, report)
    missing = []
    if args.tex:
        missing, _ = _tex_crosscheck(report, args.tex, sys.stderr if args.json else sys.stdout)
    fail = set(args.fail_on.split(",")) if args.fail_on else set()
    c = report.counts()
    hit = ((("review" in fail) and c["review"]) or (("arxiv" in fail) and c["arxiv"])
           or (("safe" in fail) and c["safe"]) or (("not-found" in fail) and c["not_found"])
           or (("error" in fail) and c["error"]) or (("any-change" in fail) and report.proposed)
           or (("duplicates" in fail) and c["duplicates"]) or (("undefined" in fail) and missing))
    return 1 if hit else 0


def cmd_fix(args) -> int:
    from .report import apply

    source, result, report = _run(args.file, args)
    accepted = _accepted(report, args.accept)
    text = apply(source, result.replacements, accepted)
    if args.in_place:
        _write_bib(args.file, text)
        dest = str(args.file)
    elif args.output:
        _write_bib(args.output, text)
        dest = str(args.output)
    else:
        sys.stdout.write(text)
        dest = None
    _print_report(args.file, report, out=sys.stderr)
    skipped = len(report.proposed) - len(accepted)
    if dest:
        sys.stderr.write(f"\nApplied {len(accepted)} fix(es) to {dest}"
                         + (f"; left {skipped} unapplied (use --accept, or review in the web UI)." if skipped else ".") + "\n")
    return 0


def _zotero():
    from .zotero import ZoteroClient, ZoteroConfig
    return ZoteroClient(ZoteroConfig.from_env())


def cmd_sync(args) -> int:
    from .splitter import find_entry_spans
    from .texcites import cited_keys
    from .zotero import ZoteroError

    keys = cited_keys(args.from_tex)
    client = _zotero()
    index, unkeyed = client.index_by_citekey()
    existing = {}
    if args.output.exists():
        text = args.output.read_text(encoding="utf-8")
        spans = find_entry_spans(text, strict=True)
        existing = {s.key: s.text(text) for s in spans}
        if len(existing) != len(spans):
            raise ValueError("citation key used by more than one entry")

    in_zotero = [k for k in keys if k in index]
    bib = client.bibtex(index[k]["key"] for k in in_zotero)
    blocks, kept, missing = [], [], []
    for k in keys:
        if k in index:
            entry = bib.get(index[k]["key"], "")
            if not entry:
                raise ZoteroError(f"Zotero returned no BibTeX for {k}; the output file was not changed.")
            # Zotero's export generates its own keys; restore the pinned citation key.
            entry = re.sub(r"^(\s*@\w+\s*\{)[^,]*,", lambda m: f"{m.group(1)}{k},", entry, count=1)
            blocks.append(entry)
        elif k in existing:
            blocks.append(existing[k])
            kept.append(k)
        else:
            missing.append(k)

    header = ("% Generated by bibfix sync from Zotero; edit synced entries in Zotero.\n"
              "% Entries marked as kept are not in Zotero yet.\n")
    body = "\n\n".join(blocks) + "\n"
    _write_bib(args.output, header + ("% kept (not in Zotero): " + ", ".join(kept) + "\n" if kept else "") + "\n" + body)
    print(f"{args.output}: {len(in_zotero)} from Zotero, {len(kept)} kept from the existing file, {len(missing)} undefined")
    if kept:
        print("  not in Zotero yet (add with `bibfix add` or in Zotero): " + ", ".join(kept))
    if unkeyed and args.verbose:
        print(f"  {len(unkeyed)} Zotero items have no pinned citation key (Better BibTeX)")
    if missing:
        print("  UNDEFINED (cited but in neither Zotero nor the old file): " + ", ".join(missing), file=sys.stderr)
        return 1
    return 0


def cmd_push(args) -> int:
    from .zmap import parse_entry, plan_update, zotero_type

    source, result, report = _run(args.file, args)
    accepted = _accepted(report, args.accept)
    client = _zotero()
    index, _ = client.index_by_citekey()
    planned = []
    for key in sorted(accepted):
        if key not in index:
            print(f"  {key}: not in Zotero, skipped")
            continue
        proposed = parse_entry(result.replacements[key])
        if proposed is None:
            continue
        item = index[key]
        valid = client.type_fields(zotero_type(proposed))
        new, changes = plan_update(item["data"], proposed, valid, update_creators=args.authors)
        if changes:
            planned.append((item, new))
            print(f"  {key}:")
            for field, old, value in changes:
                print(f"      {field}: {old or '∅'!s:.60} → {value!s:.60}")
    if not planned:
        print("Nothing to push.")
        return 0
    if not args.yes:
        print(f"\nDry run: {len(planned)} item(s) would change. Re-run with --yes to write them to Zotero.")
        return 0
    for item, new in planned:
        client.update(item, new)
    print(f"\nUpdated {len(planned)} Zotero item(s). Run `bibfix sync` to regenerate the .bib.")
    return 0


def cmd_add(args) -> int:
    from .resolve import bibtex_for, kind_of
    from .zmap import creators, fields_for, make_citekey, parse_entry, zotero_type
    from .zotero import ZoteroError

    for identifier in args.identifiers:
        kind_of(identifier)

    client = _zotero()
    index, _ = client.index_by_citekey()
    taken = set(index)
    if args.key and args.key in taken:
        raise ZoteroError(f"Citation key {args.key!r} already exists in Zotero; choose a unique key.")
    collection = client.collection_key(args.collection) if args.collection else None
    to_create, keys = [], []
    for ident in args.identifiers:
        entry = parse_entry(bibtex_for(ident))
        if entry is None:
            print(f"  {ident}: could not parse the BibTeX it resolved to", file=sys.stderr)
            return 1
        itype = zotero_type(entry)
        data = client.template(itype)
        data.update({k: v for k, v in fields_for(entry, itype).items() if k in data})
        data["creators"] = [c for c in creators(entry) if c["creatorType"] == "author"] or data.get("creators", [])
        key = args.key if (args.key and len(args.identifiers) == 1) else make_citekey(entry, taken)
        taken.add(key)
        if "citationKey" in data:  # Zotero 8: native field, synced and used by the BibTeX export
            data["citationKey"] = key
        else:
            data["extra"] = (f"{data.get('extra', '')}\nCitation Key: {key}").strip()
        if collection:
            data["collections"] = [collection]
        to_create.append(data)
        keys.append(key)
        print(f"  {key}  {data.get('title', '')[:70]}  ({itype})")
    if args.dry_run:
        print("Dry run; nothing added.")
        return 0
    client.create(to_create)
    print(f"Added {len(keys)} item(s) to Zotero. Cite with: \\cite{{{','.join(keys)}}}")
    return 0


def cmd_dblp_update(args) -> int:
    from .dblpindex import build, db_path, download
    fetched = download(force=args.force)
    if fetched or args.force or not db_path().exists():
        build()
    else:
        print(f"Index is up to date: {db_path()}")
    # dblp.xml.gz is kept: its timestamp is how the next run knows whether DBLP has published a newer dump.
    return 0


def cmd_keys(args) -> int:
    from .texcites import cited_keys
    for k in cited_keys(args.tex):
        print(k)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bibfix", description="Check and fix BibTeX against DBLP and Crossref; sync with Zotero.",
                                epilog="Run `bibfix dblp-update` once before the first check.")
    from . import __version__
    p.add_argument("--version", action="version", version=f"bibfix {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def pipeline_opts(sp):
        sp.add_argument("file", type=Path, help="the .bib file")
        sp.add_argument("--no-dblp", action="store_true", help="only rebiber's bundled database; no network lookups")
        sp.add_argument("--keep-arxiv", action="store_true", help="never replace arXiv preprints with published versions")

    sp = sub.add_parser("check", help="report problems and proposed fixes; changes nothing")
    pipeline_opts(sp)
    sp.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    sp.add_argument("--tex", type=Path, help="main .tex file: also report undefined and never-cited keys")
    sp.add_argument("--fail-on", default="", type=_fail_spec, metavar="LIST",
                    help=f"exit 1 if any of these occur (comma-separated): {', '.join(sorted(FAIL_CHOICES))}")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("fix", help="apply the proposed fixes (to stdout, -o FILE or -i)")
    pipeline_opts(sp)
    g = sp.add_mutually_exclusive_group()
    g.add_argument("-i", "--in-place", action="store_true", help="overwrite the input file")
    g.add_argument("-o", "--output", type=Path, metavar="FILE", help="write the fixed .bib here")
    sp.add_argument("--accept", default=",".join(DEFAULT_ACCEPT), type=_accept_spec, metavar="TIERS",
                    help="tiers to apply: some of review,arxiv,safe, or all, or none (default: arxiv,safe)")
    sp.set_defaults(func=cmd_fix)

    sp = sub.add_parser("sync", help="write the .bib for a paper from Zotero")
    sp.add_argument("--from-tex", type=Path, required=True, help="main .tex file; \\input files are followed")
    sp.add_argument("-o", "--output", type=Path, default=Path("references.bib"), metavar="FILE",
                    help="the .bib to write (default: references.bib)")
    sp.add_argument("-v", "--verbose", action="store_true", help="also count Zotero items without a citation key")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("push", help="write accepted fixes back into Zotero (dry run unless --yes)")
    pipeline_opts(sp)
    sp.add_argument("--accept", default=",".join(DEFAULT_ACCEPT), type=_accept_spec, metavar="TIERS",
                    help="tiers to push (default: arxiv,safe)")
    sp.add_argument("--authors", action="store_true", help="also replace author lists")
    sp.add_argument("--yes", action="store_true", help="actually write to Zotero")
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser("add", help="add papers to Zotero by DOI, arXiv ID, or DBLP key")
    sp.add_argument("identifiers", nargs="+", metavar="ID", help="DOI, arXiv ID or DBLP key")
    sp.add_argument("--collection", help="Zotero collection name to file them under")
    sp.add_argument("--key", help="citation key to use (single identifier only)")
    sp.add_argument("--dry-run", action="store_true", help="show what would be added")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("dblp-update", help="download DBLP's data dump and (re)build the local lookup index")
    sp.add_argument("--force", action="store_true", help="re-download and rebuild even if current")
    sp.set_defaults(func=cmd_dblp_update)

    sp = sub.add_parser("keys", help="list citation keys a .tex file uses")
    sp.add_argument("tex", type=Path, help="main .tex file; \\input files are followed")
    sp.set_defaults(func=cmd_keys)

    args = p.parse_args(argv)
    if args.cmd == "add" and args.key and len(args.identifiers) != 1:
        p.error("--key requires a single identifier")
    if args.cmd == "add" and args.key and re.search(r"[\s,{}()\\]", args.key):
        p.error("--key must be a single valid BibTeX citation key")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nbibfix: interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        import requests
        from .resolve import ResolveError
        from .zotero import ZoteroError
        if isinstance(exc, requests.RequestException):
            print(f"bibfix: network request failed ({type(exc).__name__}); try again later.", file=sys.stderr)
            return 2
        if isinstance(exc, OSError) and exc.filename:
            print(f"bibfix: {exc.filename}: {exc.strerror or exc}", file=sys.stderr)
            return 2
        bib = getattr(args, "file", None) or getattr(args, "output", None)
        if isinstance(exc, ValueError) and bib:
            print(f"bibfix: {bib}: {exc}; nothing was changed.", file=sys.stderr)
            return 2
        if isinstance(exc, (ZoteroError, ResolveError, OSError, ValueError, RuntimeError)):
            print(f"bibfix: {exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
