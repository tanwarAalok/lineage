"""Repo walking and tree-sitter parsing into symbols, call references and search chunks.

Language-agnostic: structure/docstrings come from tree-sitter-language-pack's `process`; calls
come from a walk over call-shaped nodes, which most grammars name the same way.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Iterator

import tree_sitter_language_pack as tslp

from lineage.config import Settings
from lineage.models import Chunk, ParsedFile, Symbol
from lineage.security import is_secret_file, redact

log = logging.getLogger(__name__)

MAX_CHUNK_CHARS = 3000
FILE_HEAD_LINES = 40
WINDOW_LINES = 60
MIN_GAP_LINES = 5  # uncovered code shorter than this between symbols is not worth its own chunk
# ponytail: per-file chunk cap keeps generated/giant files from flooding the index; past it the
# tail of the file is not searchable. Raise it if legacy monsters need full coverage.
MAX_CHUNKS_PER_FILE = 800
PARSE_TIMEOUT_MS = 20_000

UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
# Legacy extensions the grammar pack does not map on its own.
EXTENSION_LANGUAGES = {
    ".cbl": "cobol", ".cob": "cobol", ".cpy": "cobol", ".bas": "vb", ".cls": "vb", ".frm": "vb",
    ".vbs": "vb", ".pas": "pascal", ".dpr": "pascal", ".f": "fortran", ".for": "fortran", ".f77": "fortran",
    ".pls": "sql", ".pks": "sql", ".pkb": "sql", ".prc": "sql", ".fnc": "sql", ".trg": "sql",
}


def walk(root: Path, settings: Settings) -> Iterator[str]:
    """Yield indexable repo-relative POSIX paths. Honors .gitignore when root is a git repo."""
    for rel in _list_files(root, settings):
        parts = PurePosixPath(rel).parts
        if any(p in settings.ignore_dirs for p in parts[:-1]):
            continue
        name = parts[-1].lower()
        if any(fnmatch.fnmatchcase(name, g) for g in settings.ignore_globs):
            continue
        if is_secret_file(rel):
            log.info("skipping secret-like file %s", rel)
            continue
        yield rel


def _list_files(root: Path, settings: Settings) -> list[str]:
    if (root / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                capture_output=True, check=True, timeout=120,
            ).stdout
            return sorted({p for p in out.decode("utf-8", "replace").split("\0") if p})
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("git ls-files failed (%s); falling back to directory walk", e)
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in settings.ignore_dirs]
        for f in filenames:
            files.append(Path(dirpath, f).relative_to(root).as_posix())
    return sorted(files)


def read_indexable(path: Path, max_bytes: int) -> bytes | None:
    """File bytes, or None when the file is too large, binary, or a symlink out of the repo."""
    if path.is_symlink() or not path.is_file():
        return None
    if path.stat().st_size > max_bytes:
        return None
    data = path.read_bytes()
    if b"\0" in data[:8192] and not data.startswith(UTF16_BOMS):
        return None
    return data


def decode(data: bytes) -> str:
    """Legacy code is often not UTF-8: honour BOMs, then try UTF-8, then Windows-1252."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith(UTF16_BOMS):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse(rel: str, data: bytes) -> ParsedFile:
    text, redactions = redact(decode(data))
    lines = text.splitlines()
    lang = _language(rel)
    symbols, file_doc = _structure(text, lines, lang) if lang else ([], "")
    if symbols:
        _attach_calls(text, len(lines), lang, symbols)
    summary = file_doc or _comment_above(lines, 0, top_of_file=True) or _default_summary(symbols, lang)
    return ParsedFile(
        path=rel, language=lang or "text", hash=file_hash(data), summary=summary,
        symbols=symbols, chunks=_chunks(rel, text, lines, symbols, summary), redactions=redactions,
    )


PLAIN_TEXT_EXTENSIONS = (".txt", ".log", ".csv")  # the pack maps .txt to vimdoc


def _language(rel: str) -> str | None:
    lower = rel.lower()
    if lower.endswith(PLAIN_TEXT_EXTENSIONS):
        return None
    ext = PurePosixPath(lower).suffix
    if ext in EXTENSION_LANGUAGES and tslp.has_language(EXTENSION_LANGUAGES[ext]):
        return EXTENSION_LANGUAGES[ext]
    try:
        lang = tslp.detect_language_from_path(rel)
    except Exception:  # unknown extension
        return None
    return lang if lang and tslp.has_language(lang) else None


def _structure(text: str, lines: list[str], lang: str) -> tuple[list[Symbol], str]:
    try:
        result = tslp.process(text, tslp.ProcessConfig(language=lang, structure=True, docstrings=True,
                                                       imports=False, exports=False,
                                                       parse_timeout_ms=PARSE_TIMEOUT_MS))
    except Exception as e:  # grammar download/parse failure: still index the file as text
        log.warning("tree-sitter failed for %s: %s", lang, e)
        return [], ""
    docs: dict[str, str] = {}
    file_doc = ""
    for d in result.docstrings or []:
        first = _first_line(d.text)
        if not d.associated_item:
            file_doc = file_doc or first
        else:
            docs.setdefault(d.associated_item, first)

    symbols: list[Symbol] = []

    def visit(item, parent: int | None) -> None:
        kind = getattr(item.kind, "name", str(item.kind)).lower()
        if not item.name:
            return
        if kind == "function" and parent is not None and symbols[parent].kind == "class":
            kind = "method"
        sig = (item.signature or "").strip()
        symbols.append(Symbol(
            name=item.name, kind=kind,
            start_line=item.span.start_line + 1, end_line=item.span.end_line + 1,
            signature=sig[:300],
            summary=(docs.get(item.name) or _first_line(item.doc_comment or "")
                     or _comment_above(lines, item.span.start_line) or sig[:160]),
            parent=parent,
        ))
        me = len(symbols) - 1
        for child in item.children or []:
            visit(child, me)

    for item in result.structure or []:
        visit(item, None)
    return symbols, file_doc


# Node types that mean "a call" across grammars (python, js/ts, go, rust, c/c++, java, c#, kotlin, php, ...).
CALL_NODES = frozenset({
    "call", "call_expression", "method_invocation", "invocation_expression", "function_call",
    "method_call", "function_call_expression", "member_call_expression", "scoped_call_expression",
    "object_creation_expression", "new_expression",
})
CALLEE_FIELDS = ("function", "name", "method", "constructor", "type")
MEMBER_FIELDS = ("name", "property", "attribute", "field", "member")


def _attach_calls(text: str, n_lines: int, lang: str, symbols: list[Symbol]) -> None:
    try:
        root = tslp.get_parser(lang).parse(text.encode("utf-8")).root_node
    except Exception as e:
        log.warning("call extraction failed for %s: %s", lang, e)
        return
    owner = _owner_by_line(symbols, n_lines)
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in CALL_NODES:
            name = _callee(node)
            line = node.start_point[0] + 1
            i = owner[line] if line < len(owner) else None
            if name and i is not None and name not in symbols[i].calls:
                symbols[i].calls.append(name)
        stack.extend(node.named_children)


def _callee(call) -> str | None:
    """`a.b.c(x)` -> 'c', `new Foo()` -> 'Foo'. Descends member accesses to the rightmost name."""
    node = next((c for f in CALLEE_FIELDS if (c := call.child_by_field_name(f))), None)
    if node is None and call.named_children:
        node = call.named_children[0]
    for _ in range(8):  # bounded: member chains are shallow
        if node is None or node.type.endswith("identifier"):
            break
        node = next((c for f in MEMBER_FIELDS if (c := node.child_by_field_name(f))), None) or (
            node.named_children[-1] if node.named_children else None)
    if node is None or not node.type.endswith("identifier"):
        return None
    return node.text.decode("utf-8", "replace")


def _owner_by_line(symbols: list[Symbol], n_lines: int) -> list[int | None]:
    """Innermost enclosing symbol per line: paint ranges largest first so nested ones win."""
    owner: list[int | None] = [None] * (n_lines + 2)
    for i in sorted(range(len(symbols)), key=lambda i: symbols[i].start_line - symbols[i].end_line):
        s = symbols[i]
        for line in range(s.start_line, min(s.end_line, n_lines) + 1):
            owner[line] = i
    return owner


def _chunks(rel: str, text: str, lines: list[str], symbols: list[Symbol], summary: str) -> list[Chunk]:
    head = "\n".join(lines[:FILE_HEAD_LINES])
    chunks = [Chunk(f"{rel}\n{summary}\n{head}"[:MAX_CHUNK_CHARS], 1, min(len(lines), FILE_HEAD_LINES) or 1)]
    children: dict[int, list[Symbol]] = {}
    for s in symbols:
        if s.parent is not None:
            children.setdefault(s.parent, []).append(s)
    covered = [False] * (len(lines) + 2)
    for line in range(1, min(FILE_HEAD_LINES, len(lines)) + 1):  # the file-head chunk
        covered[line] = True
    for i, s in enumerate(symbols):
        header = f"{rel} :: {s.kind} {s.name}\n{s.summary}\n"
        for line in range(s.start_line, min(s.end_line, len(lines)) + 1):
            covered[line] = True
        if i in children:  # container: its members have their own chunks
            body = s.signature + "\n" + "\n".join(c.signature for c in children[i])
            chunks.append(Chunk((header + body)[:MAX_CHUNK_CHARS], s.start_line, s.end_line, symbol=i))
            continue
        # Long legacy routines are split into windows so their whole body stays searchable.
        for start, end in _windows(s.start_line, s.end_line, lines):
            part = header if start == s.start_line else f"{header}(continued)\n"
            chunks.append(Chunk((part + "\n".join(lines[start - 1:end]))[:MAX_CHUNK_CHARS], start, end, symbol=i))
    # Code outside any symbol (scripts, top-level statements, unparsed languages).
    line = 1
    while line <= len(lines):
        if covered[line]:
            line += 1
            continue
        gap_end = line
        while gap_end + 1 <= len(lines) and not covered[gap_end + 1]:
            gap_end += 1
        if gap_end - line + 1 >= MIN_GAP_LINES:
            for start, end in _windows(line, gap_end, lines):
                chunks.append(Chunk(f"{rel}\n" + "\n".join(lines[start - 1:end]), start, end))
        line = gap_end + 1
    return chunks[:MAX_CHUNKS_PER_FILE]


def _windows(first: int, last: int, lines: list[str]) -> list[tuple[int, int]]:
    """1-based inclusive line windows covering [first, last], each under MAX_CHUNK_CHARS."""
    out, start = [], first
    while start <= last:
        end, size = start, 0
        while end <= last and (end == start or (end - start < WINDOW_LINES and size + len(lines[end - 1]) < MAX_CHUNK_CHARS)):
            size += len(lines[end - 1]) + 1
            end += 1
        out.append((start, end - 1))
        start = end
    return out


COMMENT_PREFIXES = ("//", "/*", "*", "# ", "--", ";;")
SKIP_ABOVE = ("@", "[")  # annotations / attributes between a doc comment and its symbol


def _is_comment(line: str) -> bool:
    return line.startswith(COMMENT_PREFIXES) or line == "#"


def _comment_above(lines: list[str], start: int, top_of_file: bool = False) -> str:
    """First line of the comment block directly above 0-based line `start`, or of the
    file's opening comment when top_of_file."""
    block: list[str] = []
    if top_of_file:
        for raw in lines[:30]:
            line = raw.strip()
            if not block and (not line or line.startswith("#!")):
                continue
            if not _is_comment(line):
                break
            block.append(line)
    else:
        i = start - 1
        while i >= 0 and lines[i].strip().startswith(SKIP_ABOVE):
            i -= 1
        while i >= 0 and _is_comment(lines[i].strip()):
            block.insert(0, lines[i].strip())
            i -= 1
    return _first_line("\n".join(block))


def _first_line(doc: str) -> str:
    for raw in doc.splitlines():
        line = re.sub(r"<[^>]+>", "", raw).strip().strip("\"'`").lstrip("/*#!-;").rstrip("*/").strip()
        if line:
            return line[:160]
    return ""


def _default_summary(symbols: list[Symbol], lang: str | None) -> str:
    top = [s.name for s in symbols if s.parent is None][:6]
    if top:
        return f"{lang} module defining {', '.join(top)}"
    return f"{lang or 'text'} file"
