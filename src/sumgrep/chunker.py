# src/sumgrep/chunker.py
"""Phase 1 — AST chunking.

Turns a git repo into a list of Chunk records: one per function, method,
or class, cut on tree-sitter AST boundaries. Everything downstream
(summarizer, indexer) consumes these records.
"""
from __future__ import annotations

import fnmatch
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

# ---------------------------------------------------------------- constants

MAX_CHUNK_CHARS = 8_000     # bigger chunks get head+tail truncated (code only)
TRUNC_KEEP = 3_000          # chars kept at each end when truncating
MAX_FILE_BYTES = 2_000_000  # skip generated/huge files outright

# Always skipped (shell-style patterns; `*` matches across `/`).
# Add repo-specific ones with `--exclude` (Phase 3 moves these to sumgrep.toml).
DEFAULT_EXCLUDES = ("*.pb.go", "*/vendor/*", "*/node_modules/*", "*.min.js")

EXT_LANG = {
    ".py": "python",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".h": "cpp",
}

# tsx / javascript reuse the typescript node-type sets.
LANG_ALIAS = {"tsx": "typescript", "javascript": "typescript"}

FUNC_NODES = {
    "python":     {"function_definition"},
    "go":         {"function_declaration", "method_declaration"},
    "typescript": {"function_declaration", "method_definition"},
    "cpp":        {"function_definition"},
}

CLASS_NODES = {
    "python":     {"class_definition"},
    "go":         set(),
    "typescript": {"class_declaration"},
    "cpp":        {"class_specifier", "struct_specifier"},
}

# ---------------------------------------------------------------- data model


@dataclass(frozen=True)
class Chunk:
    repo: str
    path: str          # relative to repo root
    language: str      # as detected from the extension (tsx stays tsx)
    symbol: str        # qualified: "Router.handle", "Foo::bar", "backoffLoop"
    kind: str          # "function" | "method" | "class"
    start_line: int    # 1-based, inclusive
    end_line: int
    code: str          # possibly truncated (see raw_chars)
    code_hash: str     # sha256 of the comment-stripped body — freshness key
    raw_chars: int     # size before truncation

    @property
    def chunk_id(self) -> str:
        return f"{self.repo}:{self.path}:{self.symbol}"


# ---------------------------------------------------------------- helpers


def _text(src: bytes, node: Node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def normalized_hash(node: Node, src: bytes) -> str:
    """sha256 over the node's tokens with comments dropped and whitespace
    collapsed — so comment/formatting edits don't change the hash."""
    parts: list[bytes] = []

    def walk(n: Node) -> None:
        if "comment" in n.type:
            return
        if n.child_count == 0:
            parts.append(src[n.start_byte:n.end_byte])
        else:
            for c in n.children:
                walk(c)

    walk(node)
    return hashlib.sha256(b" ".join(parts)).hexdigest()


def _truncate(code: str) -> str:
    if len(code) <= MAX_CHUNK_CHARS:
        return code
    omitted = len(code) - 2 * TRUNC_KEEP
    return (code[:TRUNC_KEEP]
            + f"\n/* … sumgrep: {omitted} chars truncated … */\n"
            + code[-TRUNC_KEEP:])


def _span(node: Node) -> Node:
    """Expand the emitted span to include `export ...` wrappers (TS/JS)."""
    s = node
    while s.parent is not None and s.parent.type == "export_statement":
        s = s.parent
    return s


def _field_name(src: bytes, node: Node) -> str | None:
    n = node.child_by_field_name("name")
    return _text(src, n) if n is not None else None


def _cpp_name(src: bytes, node: Node) -> str | None:
    """Descend `declarator` fields until the innermost name-ish node.
    Handles pointers, qualified names (Foo::bar), operators, destructors."""
    d = node
    while True:
        nxt = d.child_by_field_name("declarator")
        if nxt is None:
            break
        d = nxt
    return None if d is node else _text(src, d)


def _go_receiver(src: bytes, node: Node) -> str | None:
    recv = node.child_by_field_name("receiver")
    if recv is None:
        return None
    for c in recv.named_children:
        if c.type == "parameter_declaration":
            t = c.child_by_field_name("type")
            if t is not None:
                # "*Server" -> "Server", "Queue[T]" -> "Queue"
                return _text(src, t).lstrip("*").split("[")[0]
    return None


def _signature(src: bytes, outer: Node) -> str:
    """Text from the definition's start to its body's start — i.e. the
    signature (decorators included for decorated defs)."""
    inner = outer
    if outer.type == "decorated_definition":
        inner = outer.child_by_field_name("definition") or outer
    body = inner.child_by_field_name("body")
    end = body.start_byte if body is not None else inner.end_byte
    return src[outer.start_byte:end].decode("utf-8", "replace").strip()


def _py_docstring(src: bytes, body: Node) -> str | None:
    kids = body.named_children
    if kids and kids[0].type == "expression_statement":
        inner = kids[0].named_children
        if inner and inner[0].type == "string":
            return _text(src, kids[0])
    return None


# ---------------------------------------------------------------- per-file


def chunk_file(repo: str, repo_root: Path, rel_path: str) -> list[Chunk]:
    language = EXT_LANG[Path(rel_path).suffix]
    lang = LANG_ALIAS.get(language, language)      # node-set key
    src = (repo_root / rel_path).read_bytes()
    tree = get_parser(language).parse(src)

    chunks: list[Chunk] = []
    seen: dict[str, int] = {}                      # symbol collision counter

    def emit(span: Node, hash_key: str, symbol: str, kind: str,
             code: str | None = None) -> None:
        n = seen.get(symbol, 0)
        seen[symbol] = n + 1
        if n:                                      # e.g. C++ overloads
            symbol = f"{symbol}#{n + 1}"
        text = code if code is not None else _text(src, span)
        chunks.append(Chunk(
            repo=repo, path=rel_path, language=language,
            symbol=symbol, kind=kind,
            start_line=span.start_point[0] + 1,
            end_line=span.end_point[0] + 1,
            code=_truncate(text), code_hash=hash_key,
            raw_chars=len(text),
        ))

    def handle_function(node: Node, span: Node, scope: list[str]) -> None:
        if lang == "cpp":
            body = node.child_by_field_name("body")
            if body is None or not body.named_children:
                return                             # empty/defaulted stubs
            name = _cpp_name(src, node)
        elif lang == "go" and node.type == "method_declaration":
            recv = _go_receiver(src, node)
            base = _field_name(src, node)
            name = f"{recv}.{base}" if recv and base else base
        else:
            name = _field_name(src, node)
        if not name:
            return
        symbol = ".".join(scope + [name]) if scope else name
        kind = ("method" if scope or "." in name and lang == "go"
                else "function")
        emit(_span(span), normalized_hash(node, src), symbol, kind)
        # no recursion: nested defs stay inside this chunk

    def handle_class(node: Node, span: Node, scope: list[str]) -> None:
        name = _field_name(src, node)
        if not name:
            for c in node.named_children:
                walk(c, scope)
            return
        symbol = ".".join(scope + [name]) if scope else name
        body = node.child_by_field_name("body")

        head_end = body.start_byte if body is not None else node.end_byte
        parts = [src[_span(span).start_byte:head_end]
                 .decode("utf-8", "replace").strip()]
        if body is not None:
            if lang == "python":
                ds = _py_docstring(src, body)
                if ds:
                    parts.append("    " + ds)
            for ch in body.named_children:
                inner = (ch.child_by_field_name("definition")
                         if ch.type == "decorated_definition" else ch)
                if inner is not None and inner.type in FUNC_NODES[lang]:
                    parts.append("    " + _signature(src, ch) + " ...")
        skeleton = "\n".join(parts)
        emit(span, hashlib.sha256(skeleton.encode()).hexdigest(),
             symbol, "class", code=skeleton)

        if body is not None:                       # methods, nested classes
            for ch in body.named_children:
                walk(ch, scope + [name])

    def handle_declaration(node: Node, scope: list[str]) -> None:
        """TS/JS: const f = (...) => {...} — chunk named function values."""
        hit = False
        for d in node.named_children:
            if d.type != "variable_declarator":
                continue
            val = d.child_by_field_name("value")
            if val is not None and val.type in (
                    "arrow_function", "function_expression", "function"):
                name = _field_name(src, d)
                if name:
                    emit(_span(node), normalized_hash(node, src),
                         ".".join(scope + [name]) if scope else name,
                         "method" if scope else "function")
                    hit = True
        if not hit:
            for c in node.named_children:
                walk(c, scope)

    def walk(node: Node, scope: list[str]) -> None:
        t = node.type
        if t == "decorated_definition":            # python decorators
            inner = node.child_by_field_name("definition")
            if inner is not None:
                if inner.type in CLASS_NODES[lang]:
                    handle_class(inner, node, scope)
                elif inner.type in FUNC_NODES[lang]:
                    handle_function(inner, node, scope)
                return
        if t in CLASS_NODES[lang]:
            handle_class(node, node, scope)
        elif t in FUNC_NODES[lang]:
            handle_function(node, node, scope)
        elif lang == "typescript" and t in ("lexical_declaration",
                                            "variable_declaration"):
            handle_declaration(node, scope)
        else:
            for c in node.named_children:
                walk(c, scope)

    walk(tree.root_node, [])
    return chunks


# ---------------------------------------------------------------- per-repo


def chunk_repo(repo_path: Path,
               exclude: tuple[str, ...] = ()) -> tuple[list[Chunk], list[str]]:
    """Returns (chunks, skipped) — skipped is 'path (reason)' strings."""
    patterns = (*DEFAULT_EXCLUDES, *exclude)
    repo_root = repo_path.resolve()
    repo = repo_root.name
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True, check=True,
        ).stdout
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"{repo_root} is not a git repository "
            f"(git ls-files failed: {e.stderr.decode().strip()})") from e

    chunks: list[Chunk] = []
    skipped: list[str] = []
    excluded = 0
    for rel in out.decode("utf-8", "replace").split("\0"):
        if not rel or Path(rel).suffix not in EXT_LANG:
            continue
        if any(fnmatch.fnmatch(rel, pat) for pat in patterns):
            excluded += 1
            continue
        f = repo_root / rel
        if not f.is_file():
            continue
        if f.stat().st_size > MAX_FILE_BYTES:
            skipped.append(f"{rel} (>{MAX_FILE_BYTES // 1_000_000} MB)")
            continue
        try:
            chunks.extend(chunk_file(repo, repo_root, rel))
        except Exception as e:                     # never die on one file
            skipped.append(f"{rel} (parse error: {e})")
    if excluded:
        skipped.append(f"{excluded} files matched exclude patterns")
    return chunks, skipped
