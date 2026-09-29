"""Collect the citation keys a LaTeX document uses, following \\input/\\include.

Handles natbib and biblatex variants (\\citep, \\citet*, \\citeauthor,
\\parencite, \\textcite, \\autocite, \\nocite, ...), optional arguments
(\\citep[see][p.~3]{a,b}), multi-key lists, and % comments. Keys come back
in order of first use, which is also the order `sync` writes them in.
"""

from __future__ import annotations

import re
from pathlib import Path

_CITE_RE = re.compile(
    r"\\(?:[a-zA-Z]*cite[a-zA-Z]*|nocite)\*?"  # \cite, \citep, \parencite, \citeauthor*, \nocite ...
    r"(?:\s*\[[^\]]*\]){0,2}"                   # up to two optional args
    r"\s*\{([^}]*)\}"
)
_INPUT_RE = re.compile(r"\\(?:input|include|subfile)\s*\{([^}]+)\}")
_COMMENT_RE = re.compile(r"(?<!\\)%.*")


def _strip_comments(text: str) -> str:
    return "\n".join(_COMMENT_RE.sub("", line) for line in text.splitlines())


def _resolve(base: Path, name: str) -> Path | None:
    p = (base / name.strip())
    for candidate in (p, p.with_suffix(p.suffix + ".tex") if p.suffix != ".tex" else p):
        if candidate.is_file():
            return candidate
    return None


def cited_keys(tex_path: str | Path) -> list[str]:
    """Return citation keys used by `tex_path` and the files it inputs, in
    first-use order. `\\nocite{*}` is ignored (it means "everything")."""
    root = Path(tex_path).resolve()
    seen_files: set[Path] = set()
    keys: dict[str, None] = {}

    def walk(path: Path) -> None:
        if path in seen_files:
            return
        seen_files.add(path)
        text = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        # Process cites and inputs in document order so key order matches the PDF.
        events = [(m.start(), "cite", m.group(1)) for m in _CITE_RE.finditer(text)]
        events += [(m.start(), "input", m.group(1)) for m in _INPUT_RE.finditer(text)]
        for _, kind, arg in sorted(events):
            if kind == "cite":
                for key in arg.split(","):
                    key = key.strip()
                    if key and key != "*":
                        keys.setdefault(key, None)
            else:
                child = _resolve(root.parent, arg)
                if child is not None:
                    walk(child)

    walk(root)
    return list(keys)
