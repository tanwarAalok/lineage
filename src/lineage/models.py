"""Plain data passed between ingest, store and search."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Symbol:
    name: str
    kind: str  # function / method / class / interface / ...
    start_line: int  # 1-based, inclusive
    end_line: int
    signature: str = ""
    summary: str = ""
    parent: int | None = None  # index into ParsedFile.symbols
    calls: list[str] = field(default_factory=list)  # callee names; resolved by name at query time


@dataclass
class Chunk:
    text: str
    start_line: int
    end_line: int
    symbol: int | None = None  # index into ParsedFile.symbols; None = file-level chunk


@dataclass
class ParsedFile:
    path: str  # repo-relative, forward slashes
    language: str
    hash: str
    summary: str
    symbols: list[Symbol]
    chunks: list[Chunk]
    redactions: int = 0


@dataclass
class SearchHit:
    system_id: str
    path: str
    meaning: str
    score: float
    symbols: list[str]
    lines: tuple[int, int]
