# src/sumgrep/docs.py
"""Docs — Markdown files cut into pieces, each kept exactly as written.

No LLM here: a piece is embedded as it stands, with one line in front of
it saying where it sits (file path + the headings above it).

A file up to MAX_DOC_CHARS is one piece. A bigger one is cut at the first
of these that makes the pieces fit, never inside a sentence:
  1. headings, top level first, only as deep as needed
  2. blank lines: the end of a paragraph, table, list or code block
  3. line ends where the next line starts a table row or list item, the
     line ends a sentence, or both lines are code
Text with none of these within MAX_DOC_CHARS stays whole and oversized
(the embedder then reads only its start). A heading is never cut from
the text under it. A piece under MIN_DOC_CHARS joins the piece after or
before it when the two fit and belong together: one continues the other's
section, or they are sections side by side under the same heading.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from .chunker import DEFAULT_EXCLUDES, MAX_FILE_BYTES, Chunk, tracked_files

DOC_EXTS = (".md", ".mdx")
# bge-m3 through Ollama reads the first 2,048 tokens of an input and ignores
# the rest. Measured 2026-11-26: Korean-heavy Markdown is ~2 chars per token
# (4,000 chars = 2,016 tokens), English ~3.3.
MAX_DOC_CHARS = 4_000
MIN_DOC_CHARS = 400         # a smaller piece joins a neighbour if they fit

_HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")
_ITEM = re.compile(r"\s*(?:(?:[-*+]|\d+[.)])[ \t]|\|)")     # list item or table row
_SENTENCE_END = re.compile(r"[.!?。…][\"'”’)\]*_`]*$")


@dataclass
class _Piece:
    lo: int                     # lines[lo:hi]
    hi: int
    trail: tuple[str, ...]      # the headings above it, outermost first
    part: int = 0               # n-th part of a section with no heading to cut at


def embed_text(c: Chunk) -> str:
    """What is embedded for a doc piece: where it sits, then its text."""
    return f"{c.path} > {c.symbol}\n\n{c.code}"


def chunk_doc(repo: str, rel_path: str, text: str) -> list[Chunk]:
    lines = text.removeprefix("\ufeff").splitlines(keepends=True)
    n = len(lines)
    off = [0]                                   # chars before each line
    for ln in lines:
        off.append(off[-1] + len(ln))
    blank = [not ln.strip() for ln in lines]

    # headings, and which lines are fenced code (no headings in there)
    code = [False] * n
    heads: list[tuple[int, int, str]] = []      # (line, level, title)
    fence: tuple[str, int] | None = None        # (` or ~, how many) while inside one
    for i, ln in enumerate(lines):
        s = ln.rstrip("\r\n")
        m = _FENCE.match(s)
        if fence:
            code[i] = True
            if m and m[1][0] == fence[0] and len(m[1]) >= fence[1] and not m[2].strip():
                fence = None
        elif m and not (m[1][0] == "`" and "`" in m[2]):
            code[i] = True
            fence = (m[1][0], len(m[1]))
        elif (h := _HEADING.match(s)) and h[2]:
            heads.append((i, len(h[1]), h[2]))

    head_lines = {h[0] for h in heads}

    def size(lo: int, hi: int) -> int:
        return off[hi] - off[lo]

    pieces: list[_Piece] = []

    def leaf(lo: int, hi: int, trail: tuple[str, ...]) -> None:
        """lines[lo:hi] has no heading to cut at: one piece if it fits, else
        parts filled front to back, ending at blank lines where possible."""
        while hi > lo and blank[hi - 1]:
            hi -= 1
        if size(lo, hi) <= MAX_DOC_CHARS:
            if lo < hi:
                pieces.append(_Piece(lo, hi, trail))
            return
        # a cut at c falls between lines c-1 and c; none before the first
        # line of text, so a heading stays with what is under it
        first = next((i for i in range(lo, hi)
                      if not blank[i] and i not in head_lines), hi)
        soft = [c for c in range(first + 1, hi)              # a new block starts
                if blank[c - 1] and not blank[c] and not code[c - 1]]
        hard = [c for c in range(first + 1, hi)              # no sentence is split
                if (code[c - 1] and code[c]) or blank[c - 1] or _ITEM.match(lines[c])
                or _SENTENCE_END.search(lines[c - 1].rstrip())]
        parts: list[tuple[int, int]] = []
        start = lo
        while start < hi:
            end: int | None = hi
            if size(start, hi) > MAX_DOC_CHARS:
                end = max((c for c in soft
                           if c > start and size(start, c) <= MAX_DOC_CHARS), default=None)
                block = min((c for c in soft if c > (end or start)), default=hi)
                if end is None or size(end, block) > MAX_DOC_CHARS:
                    # the block that comes next fits in no piece: cut inside it
                    end = max((c for c in hard if c > (end or start)
                               and size(start, c) <= MAX_DOC_CHARS), default=end)
                if end is None:                              # nowhere to cut within the size
                    end = min((c for c in (*soft, *hard) if c > start), default=hi)
            parts.append((start, end))
            start = end
        for i, (a, b) in enumerate(parts, 1):
            pieces.append(_Piece(a, b, trail, i if len(parts) > 1 else 0))

    def cut(lo: int, hi: int, level: int, trail: tuple[str, ...]) -> None:
        """lines[lo:hi] is the section under `trail` (its own heading, if any,
        is line lo and has `level`)."""
        if size(lo, hi) <= MAX_DOC_CHARS:
            if lo < hi:
                pieces.append(_Piece(lo, hi, trail))
            return
        inner = [h for h in heads if lo <= h[0] < hi and h[1] > level]
        if not inner:
            leaf(lo, hi, trail)
            return
        top = min(h[1] for h in inner)
        cuts = [h for h in inner if h[1] == top]
        leaf(lo, cuts[0][0], trail)                          # the text above the first of them
        for h, nxt in zip(cuts, [*cuts[1:], (hi, 0, "")]):
            cut(h[0], nxt[0], h[1], (*trail, h[2]))

    cut(0, n, 0, ())

    def inside(a: _Piece, b: _Piece) -> bool:
        """b (which follows a) is still in the section a opens."""
        return b.trail[:len(a.trail)] == a.trail

    def beside(a: _Piece, b: _Piece) -> bool:
        """Two sections under the same heading."""
        return len(a.trail) == len(b.trail) and a.trail[:-1] == b.trail[:-1]

    # a small piece joins the next one if that continues its section, else
    # the one before it, else the next if they sit side by side
    joined: list[_Piece] = []
    i = 0
    while i < len(pieces):
        p = pieces[i]
        if size(p.lo, p.hi) < MIN_DOC_CHARS:
            prev = joined[-1] if joined else None
            nxt = pieces[i + 1] if i + 1 < len(pieces) else None
            fits_next = nxt is not None and size(p.lo, nxt.hi) <= MAX_DOC_CHARS
            fits_prev = prev is not None and size(prev.lo, p.hi) <= MAX_DOC_CHARS
            if fits_next and inside(p, nxt):
                pieces[i + 1] = _Piece(p.lo, nxt.hi, nxt.trail,
                                       p.part if nxt.trail == p.trail else nxt.part)
            elif fits_prev and (inside(prev, p) or beside(prev, p)):
                joined[-1] = _Piece(prev.lo, p.hi, prev.trail, prev.part)
            elif fits_next and beside(p, nxt):
                pieces[i + 1] = _Piece(p.lo, nxt.hi, nxt.trail, nxt.part)
            elif not all(blank[j] or j in head_lines for j in range(p.lo, p.hi)):
                joined.append(p)        # (a bare heading is dropped: what follows has it in its trail)
        else:
            joined.append(p)
        i += 1

    stem = Path(rel_path).stem
    whole = len(joined) == 1 and not joined[0].trail         # the file is one piece
    title = heads[0][2] if whole and heads and heads[0][1] == 1 else stem
    out: list[Chunk] = []
    seen: Counter[str] = Counter()
    for p in joined:
        lo, hi = p.lo, p.hi
        while lo < hi and blank[lo]:
            lo += 1
        while hi > lo and blank[hi - 1]:
            hi -= 1
        if lo == hi:
            continue
        symbol = " > ".join(p.trail) or title
        if p.part:
            symbol += f"#part{p.part}"
        seen[symbol] += 1
        if seen[symbol] > 1:                                 # the same headings twice in one file
            symbol += f" ({seen[symbol]})"
        body = "".join(lines[lo:hi]).rstrip("\r\n")
        c = Chunk(repo=repo, path=rel_path, language="markdown", symbol=symbol,
                  kind="doc", start_line=lo + 1, end_line=hi, code=body,
                  code_hash="", raw_chars=len(body))
        out.append(replace(c, code_hash=hashlib.sha256(
            embed_text(c).encode("utf-8", "replace")).hexdigest()))
    return out


def chunk_docs(repo_path: Path,
               exclude: tuple[str, ...] = ()) -> tuple[list[Chunk], list[str]]:
    """Returns (pieces, skipped) for the repo's tracked Markdown files —
    skipped is 'path (reason)' strings."""
    patterns = (*DEFAULT_EXCLUDES, *exclude)
    repo_root = repo_path.resolve()
    repo = repo_root.name

    chunks: list[Chunk] = []
    skipped: list[str] = []
    excluded = 0
    for rel in tracked_files(repo_root):
        if Path(rel).suffix.lower() not in DOC_EXTS:
            continue
        if any(fnmatch.fnmatch(rel, pat) for pat in patterns):
            excluded += 1
            continue
        f = repo_root / rel
        if not f.is_file():                                  # e.g. a dangling symlink
            continue
        if f.stat().st_size > MAX_FILE_BYTES:
            skipped.append(f"{rel} (>{MAX_FILE_BYTES // 1_000_000} MB)")
            continue
        chunks.extend(chunk_doc(repo, rel, f.read_text("utf-8", "replace")))
    if excluded:
        skipped.append(f"{excluded} files matched exclude patterns")
    return chunks, skipped
