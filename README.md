# bibfix

Check a BibTeX file against rebiber's bundled proceedings database, a local copy of
DBLP, and Crossref; review the proposed corrections; optionally sync the bibliography
with Zotero. Requires Python 3.12 or later and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/narunraman/bibfix
bibfix dblp-update                          # once, then monthly (see below)
bibfix check references.bib --tex main.tex  # report only; also undefined / never-cited keys
bibfix fix references.bib -o reviewed.bib   # apply safe and arXiv fixes
```

## The local DBLP index

DBLP's search API and `.bib` pages answer scripts with an anti-bot page, and DBLP asks
tools that make many lookups to use its data dump instead. `bibfix dblp-update`
downloads the dump (about 1.1 GB) and builds a SQLite index from it (about 4.5 GB, ten
minutes or so). Re-run it monthly; it only downloads when DBLP has published a newer
dump. Without the index, bibfix still checks entries against rebiber and Crossref, and
its report says which entries could not be looked up on DBLP.

The index lives in `$BIBFIX_DBLP_DIR`, else `$XDG_CACHE_HOME/bibfix`, else `~/.cache/bibfix`.

## Results

| Result | Meaning | Applied by `fix` |
|---|---|---|
| safe | same paper; fields added or tidied | yes |
| arxiv | preprint → its published version | yes |
| review | title, authors, type or existing values differ, so possibly a different paper | no |
| not found | neither DBLP nor Crossref knows it | n/a |
| duplicate | one paper under several keys | n/a |

A match is evidence of matching metadata, not proof that a citation is right. `fix`
writes to stdout unless given `-o FILE` or `-i` (in place), and takes
`--accept safe,arxiv,review | all | none`. `--keep-arxiv` never swaps preprints for
published versions; `--no-dblp` uses only rebiber's database, with no network lookups.

```sh
bibfix check references.bib --json --fail-on not-found,error   # for CI
bibfix keys main.tex                                           # citation keys a paper uses
```

`check` exits 1 when a `--fail-on` condition occurs. Every command exits 2 when it
cannot run (a missing or malformed file, bad Zotero credentials, no network for Zotero);
lookups that fail during a check are reported per entry instead. The [sample workflow](bibfix-check.yml)
runs the check in a paper's GitHub repository.

Crossref: set `BIBFIX_MAILTO` to your email address to use Crossref's "polite pool"
(more reliable rate limits). It is sent to Crossref with each query and is never stored.

## Zotero

Zotero commands use the Zotero web API, so the Zotero app need not be running. Create a
key at <https://www.zotero.org/settings/keys> (with write access for `push --yes` and
`add`) and set:

```sh
export ZOTERO_API_KEY=...          # keep it out of files you commit
export ZOTERO_LIBRARY_ID=...       # "Your userID for use in API calls", on the same page
export ZOTERO_LIBRARY_TYPE=group   # only for a group library (the default is user)
```

Citation keys come from Better BibTeX (pinned keys, as the `citationKey` field or a
`Citation Key:` line in Extra).

```sh
bibfix sync --from-tex main.tex -o references.bib   # Zotero → .bib for the keys the paper cites
bibfix push references.bib                          # show the fixes it would write to Zotero
bibfix push references.bib --yes                    # write them
bibfix add 2101.00001 10.5555/example --dry-run     # add papers by arXiv ID, DOI or DBLP key
```

`sync` follows `\input` files, writes entries in citation order, keeps cited entries
that are not in Zotero yet, and reports undefined citations. `push` leaves Zotero's
author lists alone unless given `--authors`. Library metadata is cached in
`$XDG_CACHE_HOME/bibfix` (default `~/.cache/bibfix`), readable only by you.

## Development

```sh
uv run pytest packages/bibfix/tests   # offline: a tiny DBLP dump, faked Crossref and Zotero
```
