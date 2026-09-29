"""Local DBLP index built from the official XML dump.

DBLP's search API and record (.bib) pages may present anti-bot checks to
scripted clients, and DBLP asks tools that do many lookups to use
the dump instead (https://dblp.org/faq/Am+I+allowed+to+crawl+the+dblp+website.html).
This module downloads the dump (only when DBLP has published a newer one), streams
it into SQLite keyed by normalised title, and turns records back into BibTeX in
DBLP's own style. Lookups are then local, instant and never rate-limited.

  bibfix dblp-update          # large download and local index build; re-run periodically
Index location: $BIBFIX_DBLP_DIR or $XDG_CACHE_HOME/bibfix (default ~/.cache/bibfix).
"""

from __future__ import annotations

import gzip
import os
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

DUMP_URL = "https://dblp.org/xml/dblp.xml.gz"
DTD_URL = "https://dblp.org/xml/dblp.dtd"
RECORD_TYPES = ("article", "inproceedings", "proceedings", "book", "incollection", "phdthesis", "mastersthesis")
FIELDS = ("key", "type", "title", "authors", "editors", "year", "journal", "booktitle", "volume", "number",
          "pages", "publisher", "series", "school", "isbn", "crossref", "ee", "doi", "tnorm")


def data_dir() -> Path:
    return Path(os.environ.get("BIBFIX_DBLP_DIR") or
                Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "bibfix")


def db_path() -> Path:
    return data_dir() / "dblp.sqlite"


def norm_title(title: str) -> str:
    """Case-, accent-, brace- and punctuation-insensitive title key."""
    s = re.sub(r"\\[a-zA-Z]+\s*", " ", title or "")          # \emph, \textit ...
    s = re.sub(r"[{}]", "", s)                                 # {H}olistic -> Holistic, not "h olistic"
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# -- download ---------------------------------------------------------------
def download(dest: Path | None = None, *, force: bool = False, log=print) -> bool:
    """Fetch the dump if DBLP has a newer one. Returns True if a new file was fetched."""
    dest = dest or data_dir()
    dest.mkdir(parents=True, exist_ok=True)
    gz = dest / "dblp.xml.gz"
    head = requests.head(DUMP_URL, timeout=30, allow_redirects=True)
    head.raise_for_status()
    remote = parsedate_to_datetime(head.headers["Last-Modified"]).timestamp() if "Last-Modified" in head.headers else None
    if gz.exists() and not force and remote and gz.stat().st_mtime >= remote:
        log(f"DBLP dump is current ({time.strftime('%Y-%m-%d', time.localtime(gz.stat().st_mtime))}).")
        return False
    size = int(head.headers.get("Content-Length", 0))
    log(f"Downloading DBLP dump ({size / 1e9:.1f} GB)…")
    with requests.get(DTD_URL, timeout=60) as dtd:
        dtd.raise_for_status()
        (dest / "dblp.dtd").write_bytes(dtd.content)
    part = gz.with_suffix(".gz.part")
    with requests.get(DUMP_URL, stream=True, timeout=60) as r, open(part, "wb") as f:
        r.raise_for_status()
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    part.replace(gz)
    if remote:
        os.utime(gz, (remote, remote))
    return True


# -- build --------------------------------------------------------------------
def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def build(gz: Path | None = None, db: Path | None = None, *, log=print) -> int:
    """Stream the dump into a fresh SQLite index; swap it in atomically. Returns record count."""
    from lxml import etree

    gz = gz or data_dir() / "dblp.xml.gz"
    db = db or db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(prefix=".dblp-", suffix=".sqlite.building", dir=db.parent)
    os.close(descriptor)
    tmp = Path(tmp_name)
    con = None
    stream = None
    try:
        con = sqlite3.connect(tmp)
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute(f"CREATE TABLE rec ({', '.join(f + (' TEXT PRIMARY KEY' if f == 'key' else ' TEXT') for f in FIELDS)})")

        # The DTD (which defines the dump's character entities, e.g. &auml;) is resolved
        # relative to the file object's name, i.e. dblp.dtd next to dblp.xml.gz.
        stream = gzip.open(gz, "rb")
        ctx = etree.iterparse(stream, events=("end",), tag=RECORD_TYPES, load_dtd=True,
                              resolve_entities=True, huge_tree=True, no_network=True)
        batch, n, t0 = [], 0, time.time()
        for _, el in ctx:
            rec = dict.fromkeys(FIELDS, "")
            rec["key"], rec["type"] = el.get("key", ""), el.tag
            authors, editors, ees = [], [], []
            for child in el:
                tag = child.tag
                if tag == "author":
                    authors.append(_text(child))
                elif tag == "editor":
                    editors.append(_text(child))
                elif tag == "ee":
                    ees.append(_text(child))
                elif tag in ("title", "year", "journal", "booktitle", "volume", "number", "pages",
                             "publisher", "series", "school", "isbn", "crossref") and not rec[tag]:
                    rec[tag] = _text(child)
            rec["authors"], rec["editors"] = "\t".join(authors), "\t".join(editors)
            doi = next((e for e in ees if "doi.org/" in e), "")
            rec["doi"] = doi.split("doi.org/", 1)[1] if doi else ""
            rec["ee"] = next((e for e in ees if "doi.org/" not in e), ees[0] if ees else "")
            rec["tnorm"] = norm_title(rec["title"])
            batch.append(tuple(rec[f] for f in FIELDS))
            n += 1
            # free memory: the element and everything before it
            el.clear()
            while el.getprevious() is not None:
                del el.getparent()[0]
            if len(batch) >= 50_000:
                con.executemany(f"INSERT OR REPLACE INTO rec VALUES ({','.join('?' * len(FIELDS))})", batch)
                batch.clear()
                if n % 1_000_000 < 50_000:
                    log(f"  {n / 1e6:.1f}M records ({time.time() - t0:.0f}s)")
        if batch:
            con.executemany(f"INSERT OR REPLACE INTO rec VALUES ({','.join('?' * len(FIELDS))})", batch)
        log("  building title index…")
        con.execute("CREATE INDEX rec_tnorm ON rec(tnorm)")
        con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        con.execute("INSERT INTO meta VALUES ('dump_mtime', ?), ('built', ?), ('records', ?)",
                    (str(int(gz.stat().st_mtime)), str(int(time.time())), str(n)))
        con.commit()
        con.close()
        tmp.replace(db)
    finally:
        if stream is not None:
            stream.close()
        if con is not None:
            con.close()
        tmp.unlink(missing_ok=True)
    log(f"Indexed {n:,} DBLP records in {time.time() - t0:.0f}s → {db}")
    return n


# -- query --------------------------------------------------------------------
class DblpIndex:
    def __init__(self, path: Path | None = None):
        self.path = path or db_path()
        self._local = threading.local()  # SQLite connections must not be shared across threads

    @property
    def con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            self._local.con = con
        return con

    def by_title(self, title: str) -> list[sqlite3.Row]:
        key = norm_title(title)
        if not key:
            return []
        return self.con.execute("SELECT * FROM rec WHERE tnorm = ? AND type != 'proceedings'", (key,)).fetchall()

    def get(self, key: str) -> sqlite3.Row | None:
        return self.con.execute("SELECT * FROM rec WHERE key = ?", (key,)).fetchone()

    def as_entry(self, row: sqlite3.Row) -> dict[str, str]:
        """A bibtexparser-style dict in DBLP's BibTeX conventions (what reffix/DBLP .bib gives)."""
        t = row["type"]
        e: dict[str, str] = {"ENTRYTYPE": {"phdthesis": "phdthesis", "mastersthesis": "mastersthesis"}.get(t, t),
                             "ID": "DBLP:" + row["key"]}
        # DBLP disambiguates homonyms with a numeric suffix ("Mark Chen 0003"); its own
        # BibTeX export drops it, and so do we.
        def names(field: str) -> str:
            return " and\n".join(re.sub(r"\s+\d{4}$", "", n) for n in field.split("\t"))
        if row["authors"]:
            e["author"] = names(row["authors"])
        if row["editors"]:
            e["editor"] = names(row["editors"])
        e["title"] = row["title"].rstrip(".")
        if t == "inproceedings" or t == "incollection":
            parent = self.get(row["crossref"]) if row["crossref"] else None
            e["booktitle"] = (parent["title"].rstrip(".") if parent else row["booktitle"])
            if parent:
                for f in ("publisher", "series", "volume"):
                    if parent[f] and not row[f]:
                        e[f] = parent[f]
        elif t == "article":
            e["journal"] = row["journal"]
            if row["journal"] == "CoRR" and row["volume"].startswith("abs/"):
                e["eprinttype"], e["eprint"] = "arXiv", row["volume"][4:]
        for f in ("volume", "number", "pages", "year", "publisher", "series", "school", "isbn"):
            if row[f] and f not in e:
                e[f] = row[f]
        if row["doi"]:
            e["doi"] = row["doi"]
        if row["ee"] or row["doi"]:
            e["url"] = row["ee"] or f"https://doi.org/{row['doi']}"
        e["biburl"] = f"https://dblp.org/rec/{row['key']}.bib"
        e["bibsource"] = "dblp computer science bibliography, https://dblp.org"
        return {k: v for k, v in e.items() if v}
