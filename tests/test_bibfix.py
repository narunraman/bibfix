"""Offline tests for bibfix: DBLP is a tiny local index, Crossref and Zotero are faked."""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bibfix import cli
from bibfix.crossref import CrossrefHit
from bibfix.match import DblpMatch
from bibfix.pipeline import run_pipeline
from bibfix.review import classify
from bibfix.texcites import cited_keys
from bibfix.zmap import make_citekey, parse_entry, plan_update, split_names, zotero_type
from bibfix.zotero import ZoteroClient, ZoteroConfig, ZoteroError, citation_key

FIXTURES = Path(__file__).parent / "fixtures"

PREPRINT = """@article{doe2021learning,
  author = {Jane Doe and Alex Smith},
  journal = {CoRR}, eprinttype = {arXiv}, eprint = {2101.00001}, volume = {abs/2101.00001},
  title = {Learning Synthetic Benchmarks from Examples}, year = {2021}
}"""
PUBLISHED = """@inproceedings{doe2021learning,
  author = {Jane Doe and Alex Smith},
  booktitle = {Proceedings of the Example Conference, ExConf 2022},
  publisher = {Example Press},
  title = {Learning Synthetic Benchmarks from Examples}, year = {2022}
}"""


def _no_rebiber(monkeypatch, entries=None):
    entries = entries or {}
    monkeypatch.setattr("bibfix.pipeline._run_rebiber", lambda _: (entries, set(entries)))


def _fake_dblp(monkeypatch, match):
    monkeypatch.setattr("bibfix.pipeline.match_entry", lambda key, text, replace_arxiv: match(key))


# -- the synthetic bibliography, end to end ----------------------------------------
@pytest.fixture
def dblp(tmp_path, monkeypatch):
    """A local DBLP index built from fixtures/dblp.xml, as `bibfix dblp-update` would."""
    from bibfix import dblpindex, match
    shutil.copy(FIXTURES / "dblp.dtd", tmp_path)
    with gzip.open(tmp_path / "dblp.xml.gz", "wb") as f:
        f.write((FIXTURES / "dblp.xml").read_bytes())
    monkeypatch.setenv("BIBFIX_DBLP_DIR", str(tmp_path))
    assert dblpindex.build(log=lambda *_: None) == 6       # the <www> homepage is skipped
    monkeypatch.setattr(match, "_index_cache", [])
    return dblpindex.DblpIndex()


@pytest.fixture
def offline(monkeypatch):
    _no_rebiber(monkeypatch)
    monkeypatch.setattr("bibfix.pipeline.crossref_lookup", lambda text: (
        CrossrefHit(True, "on Crossref: Example Journal, 2019 (doi:10.5555/x)") if "Imaginary Markets" in text
        else CrossrefHit(False, "not on Crossref either")))


def test_check_and_fix_synthetic_bibliography(dblp, offline, tmp_path, capsys):
    bib, out = tmp_path / "ref.bib", tmp_path / "fixed.bib"
    shutil.copy(FIXTURES / "ref.bib", bib)

    assert cli.main(["check", str(bib), "--json", "--fail-on", "not-found"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert {e["key"]: (e["status"], e["tier"]) for e in report["entries"]} == {
        "doe2021learning": ("updated", "arxiv"),
        "doe2022learning": ("updated", "safe"),
        "roe2020sparse": ("updated", "safe"),
        "lee2018imaginary": ("updated", "review"),
        "nobody2024ghost": ("not_found", None),
        "market2019prices": ("elsewhere", None),
        "broken2020comma": ("skipped", None),
    }
    assert report["duplicates"] == [["doe2021learning", "doe2022learning"]]

    assert cli.main(["fix", str(bib), "-o", str(out)]) == 0
    fixed = out.read_text()
    assert fixed.count("@inproceedings{doe2021learning,") == 1              # arXiv -> published
    assert "10.5555/exj.2020.3" in fixed                                   # safe: DOI added
    assert "year    = {2018}" in fixed                                     # review: not applied
    assert "Dan" not in fixed and "Jane Doe 0001" not in fixed             # DBLP homonym suffix dropped
    for untouched in ("@string{exj = {Example Journal}}", "title  = {Missing Comma}\n", "journal = exj,"):
        assert untouched in fixed
    assert bib.read_text() == (FIXTURES / "ref.bib").read_text()           # fix -o leaves the input alone


def test_accept_none_reproduces_input(tmp_path, monkeypatch):
    bib = tmp_path / "refs.bib"
    bib.write_text(PREPRINT + "\n")
    _no_rebiber(monkeypatch, {"doe2021learning": PUBLISHED})
    assert cli.main(["fix", str(bib), "-i", "--accept", "none"]) == 0
    assert bib.read_text() == PREPRINT + "\n"


def test_keep_arxiv(dblp, monkeypatch):
    from bibfix.match import match_entry
    kept = match_entry("doe2021learning", PREPRINT, replace_arxiv=False)
    assert kept.status in ("same", "updated") and "inproceedings" not in (kept.proposed or "")
    _no_rebiber(monkeypatch, {"doe2021learning": PUBLISHED})           # rebiber's answer is dropped too
    _fake_dblp(monkeypatch, lambda key: DblpMatch(key, "same"))
    assert run_pipeline(PREPRINT, replace_arxiv=False).replacements == {}


def test_crossref_second_opinion(monkeypatch):
    """DBLP not_found/error + Crossref found -> elsewhere; neither -> not_found; DBLP error or a
    Crossref outage without a match -> error (unverified), never reported as not found."""
    src = "\n".join(f"@article{{{k}, author={{A. B}}, title={{T {k}}}, year={{2020}}}}"
                    for k in ("econ", "fake", "down_real", "down_unknown", "crossref_down"))
    _no_rebiber(monkeypatch)
    dblp = {"econ": "not_found", "fake": "not_found", "down_real": "error", "down_unknown": "error",
            "crossref_down": "not_found"}
    _fake_dblp(monkeypatch, lambda key: DblpMatch(key, dblp[key], detail="DBLP down" if dblp[key] == "error" else ""))

    def crossref(text):
        if "T crossref_down" in text:
            return CrossrefHit(False, "Crossref lookup failed (Timeout)")
        if "T econ" in text or "T down_real" in text:
            return CrossrefHit(True, "on Crossref: J, 2020 (doi:x)")
        return CrossrefHit(False, "not on Crossref either")

    monkeypatch.setattr("bibfix.pipeline.crossref_lookup", crossref)
    r = run_pipeline(src)
    assert r.status == {"econ": "elsewhere", "fake": "not_found", "down_real": "elsewhere",
                        "down_unknown": "error", "crossref_down": "error"}
    assert "not on DBLP" in r.detail["fake"] and "DBLP down" in r.detail["down_unknown"]


def test_tex_crosscheck(tmp_path, monkeypatch, capsys):
    bib = tmp_path / "refs.bib"
    bib.write_text(PREPRINT + "\n@misc{unused, author={A. B}, title={Something Nobody Cites Here}, year={2020}}\n")
    (tmp_path / "main.tex").write_text("\\cite{doe2021learning} \\citep{ghost}")
    _no_rebiber(monkeypatch)
    _fake_dblp(monkeypatch, lambda key: DblpMatch(key, "same"))
    assert cli.main(["check", str(bib), "--tex", str(tmp_path / "main.tex"), "--fail-on", "undefined"]) == 1
    out = capsys.readouterr().out
    assert "ghost" in out.split("but not in the .bib")[1].splitlines()[1]
    assert "unused" in out.split("but not cited")[1]


# -- CLI behaviour --------------------------------------------------------------
def test_help_does_not_import_lookup_modules():
    """Keeps `bibfix --help` fast: reffix, requests and bibtexparser load only when needed."""
    script = ("import sys\nfrom bibfix import cli\ntry:\n    cli.main(['--help'])\nexcept SystemExit:\n    pass\n"
              "assert not {'reffix', 'requests', 'bibtexparser'} & set(sys.modules), sys.modules.keys()\n")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0 and "Check and fix BibTeX" in result.stdout, result.stderr


@pytest.mark.parametrize("argv", [["check", "missing.bib", "--fail-on", "typo"],
                                  ["fix", "missing.bib", "--accept", "typo"],
                                  ["add", "10.1234/a", "10.1234/b", "--key", "one"]])
def test_invalid_options_fail_before_any_lookup(argv, monkeypatch):
    monkeypatch.setattr(cli, "_run", lambda *_: pytest.fail("invalid options must not perform lookups"))
    monkeypatch.setattr(cli, "_zotero", lambda: pytest.fail("invalid options must not contact Zotero"))
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("content,message", [
    (None, "missing.bib: No such file or directory"),
    ("@article{k,title={One}}\n@article{k,title={Two}}", "citation key used by more than one entry: k"),
    ("@article{ok,title={One}}\n\n@article{k,title={Unfinished}", "line 3: this @article entry is never closed"),
])
def test_bad_input_is_a_one_line_error(tmp_path, monkeypatch, capsys, content, message):
    monkeypatch.setattr("bibfix.pipeline._run_rebiber", lambda _: pytest.fail("must validate before lookup"))
    bib = tmp_path / "missing.bib"
    if content is not None:
        bib.write_text(content)
    assert cli.main(["fix", str(bib), "-i"]) == 2
    err = capsys.readouterr().err
    assert message in err and "Traceback" not in err
    assert content is None or bib.read_text() == content


# -- review tiers -------------------------------------------------------------
def test_preprint_to_published_is_arxiv_tier():
    c = classify("doe2021learning", PREPRINT, PUBLISHED)
    assert c.tier == "arxiv" and "ARXIV→PUB" in c.badges


def test_added_fields_are_safe():
    orig = "@inproceedings{k, author={A. Smith}, title={X}, booktitle={ICML}, year={2020}}"
    prop = "@inproceedings{k, author={A. Smith}, title={X}, booktitle={ICML}, year={2020}, pages={1--9}}"
    c = classify("k", orig, prop)
    assert c.tier == "safe" and c.badges == ["+1 field"]


def test_preprint_reencoding_is_safe_and_journal_counts_as_published():
    misc = "@misc{k, author={Ann Lee and Bo Chen}, title={Verifiers}, year={2021}, eprint={2110.00001}, archivePrefix={arXiv}}"
    corr = "@article{k, author={Ann Lee and Bo Chen}, title={Verifiers}, year={2021}, journal={CoRR}, volume={abs/2110.00001}, eprinttype={arXiv}}"
    assert classify("k", misc, corr).tier == "safe"
    journal = "@article{k, author={Ann Lee and Bo Chen}, title={Verifiers}, year={2023}, journal={Trans. Mach. Learn. Res.}}"
    assert classify("k", corr, journal).tier == "arxiv"


@pytest.mark.parametrize("proposed", [
    "@inproceedings{k, author={A. Smith}, title={Example}, booktitle={J}, year={2020}, doi={10.1234/example}}",  # type
    "@article{k, author={A. Smith}, title={Example Fast}, journal={J}, year={2020}, doi={10.1234/example}}",     # title
    "@article{k, author={B. Jones}, title={Example}, journal={J}, year={2020}, doi={10.1234/example}}",          # authors
    "@article{k, author={A. Smith}, title={Example}, journal={Wrong}, year={2020}, doi={10.1234/example}}",      # venue
    "@article{k, author={A. Smith}, title={Example}, journal={J}, year={2021}, doi={10.1234/example}}",          # year
    "@article{k, author={A. Smith}, title={Example}, journal={J}, year={2020}}",                                 # removal
])
def test_changes_to_existing_metadata_need_review(proposed):
    original = "@article{k, author={A. Smith}, title={Example}, journal={J}, year={2020}, doi={10.1234/example}}"
    assert classify("k", original, proposed).tier == "review"


# -- parsing ------------------------------------------------------------------
def test_splitter_skips_comments_and_embedded_entries():
    from bibfix.splitter import find_entry_spans
    text = ('% @article{ignored, title={No}}\n'
            '@COMMENT{\n@article{embedded,title={No}}\n}\n'
            '  @ARTICLE(a, title={A "quoted" title with \\{escaped\\}})\n'
            '@article{b, title="Quoted } braces", note={@article{notAnEntry,title={No}}}}')
    spans = find_entry_spans(text, strict=True)
    assert [span.key for span in spans] == ["a", "b"]
    assert spans[0].text(text).endswith("})")


def test_parse_entry_keeps_undefined_string_macros():
    assert parse_entry("@article{k, title={T}, journal=exj # { Letters}, month=jan}")["journal"] == "exj Letters"


def test_cited_keys_follows_inputs_and_variants(tmp_path: Path):
    (tmp_path / "sections").mkdir()
    (tmp_path / "main.tex").write_text(
        "Intro \\citep[see][p.~3]{b, a}. % \\cite{commented}\n"
        "\\input{sections/rel}\n\\textcite{c}\\nocite{*}\\citeauthor*{a}\n"
    )
    (tmp_path / "sections" / "rel.tex").write_text("\\parencite{d,b} and 100\\% \\autocite{e}")
    assert cited_keys(tmp_path / "main.tex") == ["b", "a", "d", "e", "c"]


# -- local DBLP index -------------------------------------------------------------
def test_index_resolves_entities_and_proceedings(dblp):
    assert dblp.by_title("umlauts in author names")[0]["authors"] == "Mia Jäger"
    e = dblp.as_entry(dblp.get("conf/exconf/DoeS22"))
    assert e["booktitle"] == "Proceedings of the Example Conference, ExConf 2022"
    assert e["publisher"] == "Example Press" and e["author"] == "Jane Doe and\nAlex Smith"


def test_index_is_thread_safe(dblp):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as pool:
        counts = set(pool.map(lambda _: len(dblp.by_title("Learning Synthetic Benchmarks from Examples")), range(64)))
    assert counts == {2}


def test_failed_index_build_keeps_existing_index(tmp_path):
    from bibfix.dblpindex import build
    from lxml.etree import XMLSyntaxError
    gz, db = tmp_path / "dblp.xml.gz", tmp_path / "dblp.sqlite"
    db.write_bytes(b"existing index")
    with gzip.open(gz, "wt") as handle:
        handle.write("<dblp><article>")
    with pytest.raises(XMLSyntaxError):
        build(gz, db, log=lambda *_: None)
    assert db.read_bytes() == b"existing index" and not list(tmp_path.glob("*.building"))


def test_norm_title_removes_brace_protection():
    from bibfix.dblpindex import norm_title
    assert norm_title("{H}olistic {E}valuation of {L}anguage {M}odels") == "holistic evaluation of language models"
    assert norm_title(r"\emph{Attention} Is All You Need.") == "attention is all you need"


# -- BibTeX <-> Zotero mapping ---------------------------------------------------
def test_split_names_respects_braces():
    assert split_names("Smith, Jane and {Barnes and Noble} and John von Neumann") == [
        ("Jane", "Smith"), ("", "Barnes and Noble"), ("John von", "Neumann")]


def test_zotero_type():
    assert zotero_type(parse_entry(PREPRINT)) == "preprint"
    assert zotero_type(parse_entry(PUBLISHED)) == "conferencePaper"


def test_plan_update_preprint_to_conference_paper():
    data = {"itemType": "preprint", "title": "Learning synthetic benchmarks from examples", "citationKey": "stable",
            "repository": "arXiv", "date": "2021", "tags": [{"tag": "benchmarks"}],
            "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}]}
    valid = {"title", "date", "proceedingsTitle", "publisher", "DOI", "url", "pages"}
    new, changes = plan_update(data, parse_entry(PUBLISHED), valid)
    fields = {f for f, _, _ in changes}
    assert new["itemType"] == "conferencePaper" and "repository" not in new
    assert new["proceedingsTitle"] == "Proceedings of the Example Conference, ExConf 2022"
    assert new["tags"] == data["tags"] and new["citationKey"] == "stable"
    assert "title" not in fields                        # same up to case/braces
    assert {"itemType", "proceedingsTitle", "date"} <= fields
    assert new["creators"] == data["creators"]          # authors only with update_creators


def test_make_citekey_matches_better_bibtex_style():
    e = parse_entry(PUBLISHED)
    base = "doeLearningSyntheticBenchmarks2022"
    assert make_citekey(e, set()) == base
    assert make_citekey(e, {base}) == base + "a"
    assert make_citekey(e, {base} | {base + chr(c) for c in range(ord("a"), ord("z") + 1)}) == base + "aa"


def test_citation_key_from_field_or_extra():
    assert citation_key({"citationKey": "a2020"}) == "a2020"
    assert citation_key({"extra": "tldr\nCitation Key: b2021\n"}) == "b2021"
    assert citation_key({"extra": "nothing"}) is None


# -- Zotero client against a fake API --------------------------------------------
class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeZotero:
    def __init__(self):
        self.items = [
            {"key": "AAA", "version": 3, "data": {"itemType": "preprint", "title": "Learning", "extra": "Citation Key: doe2021"}},
            {"key": "BBB", "version": 5, "data": {"itemType": "journalArticle", "title": "Other", "citationKey": "roe2019"}},
            {"key": "ATT", "version": 1, "data": {"itemType": "attachment", "title": "pdf"}},
        ]
        self.headers, self.calls = {}, []

    def request(self, method, url, params=None, headers=None, data=None, timeout=None, allow_redirects=None):
        self.calls.append((method, url, params, headers, data))
        if method == "GET" and url.endswith("/items/top"):
            if headers and headers.get("If-Modified-Since-Version") == "7":
                return FakeResponse(304)
            return FakeResponse(200, self.items, {"Last-Modified-Version": "7", "Total-Results": "3"})
        if method == "GET" and url.endswith("/items"):
            keys = params["itemKey"].split(",")
            return FakeResponse(200, [{"key": k, "bibtex": f"@article{{zoteroGenerated{k},\n title={{T}}\n}}"} for k in keys])
        if method == "PUT":
            return FakeResponse(204)
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def client(tmp_path):
    return ZoteroClient(ZoteroConfig("k", "123"), session=FakeZotero(), cache_dir=tmp_path)


def test_index_by_citekey_skips_attachments_and_caches_privately(client, tmp_path):
    index, unkeyed = client.index_by_citekey()
    assert set(index) == {"doe2021", "roe2019"} and unkeyed == []
    assert (tmp_path / "zotero-user-123.json").stat().st_mode & 0o077 == 0
    # a fresh client revalidates the disk cache with one conditional request
    fresh = ZoteroClient(ZoteroConfig("k", "123"), session=FakeZotero(), cache_dir=tmp_path)
    assert len(fresh.items()) == 2
    assert fresh.http.calls[0][3] == {"If-Modified-Since-Version": "7"}


def test_duplicate_zotero_citation_keys_are_rejected(client):
    client.http.items[1]["data"]["citationKey"] = "doe2021"
    with pytest.raises(ZoteroError, match="More than one"):
        client.index_by_citekey()


def test_update_sends_version_guard(client):
    client.update({"key": "AAA", "version": 3}, {"itemType": "preprint", "title": "New"})
    method, url, _, _, data = client.http.calls[-1]
    assert method == "PUT" and url.endswith("/users/123/items/AAA")
    assert json.loads(data)["version"] == 3


def test_failed_create_is_not_retried(client):
    """A 5xx after POST may still have created the items; retrying could duplicate them."""
    calls = []
    client.http.request = lambda *args, **kwargs: calls.append(args) or FakeResponse(500)
    with pytest.raises(ZoteroError, match="avoid duplicates"):
        client.create([{"itemType": "document"}])
    assert len(calls) == 1


def test_sync_writes_cited_keys_in_order_and_keeps_unknown(tmp_path, monkeypatch, client):
    (tmp_path / "main.tex").write_text("\\cite{roe2019,doe2021,local2024} \\cite{ghost}")
    out = tmp_path / "refs.bib"
    out.write_text("@misc{local2024, title={Only here}}\n")
    monkeypatch.setattr(cli, "_zotero", lambda: client)
    assert cli.main(["sync", "--from-tex", str(tmp_path / "main.tex"), "-o", str(out)]) == 1  # ghost is undefined
    text = out.read_text()
    assert text.index("{roe2019,") < text.index("{doe2021,") < text.index("{local2024,")
    assert "zoteroGenerated" not in text


def test_sync_keeps_existing_file_when_zotero_export_is_missing(tmp_path, monkeypatch, client):
    out, tex = tmp_path / "refs.bib", tmp_path / "main.tex"
    out.write_text("original content\n")
    tex.write_text("\\cite{doe2021}")
    monkeypatch.setattr(cli, "_zotero", lambda: client)
    monkeypatch.setattr(client, "bibtex", lambda _: {})
    assert cli.main(["sync", "--from-tex", str(tex), "-o", str(out)]) == 2
    assert out.read_text() == "original content\n"
