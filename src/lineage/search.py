"""Hybrid retrieval (FTS5 keywords + vectors, fused with reciprocal rank) and term resolution."""

from __future__ import annotations

from collections import defaultdict

from lineage.ingest.embed import Embedder
from lineage.models import SearchHit
from lineage.store.base import ChunkRow, Scope, Store, TermMapping
from lineage.text import tokens

RRF_K = 60
CANDIDATES = 60
MAX_TOP_N = 25
TERM_CANDIDATES = 5
TIE_BREAK = 0.01


def _rank_chunks(store: Store, embedder: Embedder, query: str, systems: Scope) -> tuple[list[tuple[int, float]], dict[int, ChunkRow]]:
    keyword = store.keyword_search(tokens(query), systems, CANDIDATES)
    dense = store.vector_search(embedder.embed([query])[0], systems, CANDIDATES)
    fused: dict[int, float] = defaultdict(float)
    for ranked in (keyword, dense):
        for rank, (chunk_id, _) in enumerate(ranked):
            fused[chunk_id] += 1.0 / (RRF_K + rank + 1)
    ranked = sorted(fused.items(), key=lambda kv: -kv[1])
    return ranked, store.chunks([cid for cid, _ in ranked])


def search_code(store: Store, embedder: Embedder, query: str, systems: Scope, top_n: int = 8) -> list[SearchHit]:
    """Top files for a query, ranked by their best chunk (max-passage). Summing chunks would let
    any big file outvote the one function that matters: ten getters mentioning "total" beat
    calculateOrderTotal. The second-best chunk only breaks ties."""
    top_n = max(1, min(top_n, MAX_TOP_N))
    ranked, rows = _rank_chunks(store, embedder, query, systems)
    files: dict[tuple[str, str], dict] = {}
    for chunk_id, score in ranked:
        row = rows.get(chunk_id)
        if row is None:
            continue
        f = files.get((row.system_id, row.path))
        if f is None:  # ranked is best-first, so the first chunk seen is the file's best
            files[(row.system_id, row.path)] = {"score": score, "best": row, "symbols": _sym(row), "second": 0.0}
        else:
            if not f["second"]:
                f["second"] = score
                f["score"] += TIE_BREAK * score
            f["symbols"] += _sym(row)
    hits = []
    for (system_id, path), f in sorted(files.items(), key=lambda kv: -kv[1]["score"])[:top_n]:
        best: ChunkRow = f["best"]
        meaning = best.file_summary
        if best.symbol_name:
            meaning += f" | best match: {best.symbol_kind} {best.symbol_name} — {best.symbol_summary}"
        hits.append(SearchHit(system_id, path, meaning, round(f["score"], 4),
                              list(dict.fromkeys(f["symbols"]))[:6], (best.start_line, best.end_line)))
    return hits


def _sym(row: ChunkRow) -> list[str]:
    return [row.symbol_name] if row.symbol_name else []


def resolve_term(store: Store, embedder: Embedder, term: str, systems: Scope) -> dict:
    """Design term -> its approved dictionary definition + the code that implements it.
    Approved code links win; otherwise candidates are inferred, saved as `proposed` for a
    maintainer to review, and returned flagged as unconfirmed."""
    definitions = [{"project": t["system_id"], "definition": t["definition"]}
                   for t in store.terms(systems, term=term, status="approved")]
    known = store.mappings(term, systems)
    approved = [m for m in known if m.status == "approved"]
    if approved:
        return {"term": term, "status": "approved", "definitions": definitions,
                "mappings": [_m(m) for m in approved]}
    rejected = {(m.system_id, m.symbol_name, m.path) for m in known if m.status == "rejected"}

    term_tokens = set(tokens(term))
    ranked, rows = _rank_chunks(store, embedder, term, systems)
    scored: dict[tuple[str, str, str], float] = {}
    for chunk_id, score in ranked:
        row = rows.get(chunk_id)
        if row is None or not row.symbol_name:
            continue
        key = (row.system_id, row.symbol_name, row.path)
        if key in rejected or key in scored:
            continue
        # Parser-confirmed name overlap is stronger evidence than text similarity alone.
        if term_tokens and term_tokens <= set(tokens(row.symbol_name)):
            score *= 2
        scored[key] = score
    candidates = sorted(scored.items(), key=lambda kv: -kv[1])[:TERM_CANDIDATES]
    out = []
    for (system_id, symbol_name, path), score in candidates:
        m = TermMapping(system_id, term, symbol_name, path, "proposed", "inferred", round(score, 4), None, "")
        store.save_mapping(m)
        out.append(_m(m))
    return {
        "term": term,
        "status": "proposed" if out else "not_found",
        "note": "Code links are inferred, not reviewed. A project maintainer approves them in the Lineage web UI.",
        "definitions": definitions,
        "mappings": out,
    }


def _m(m: TermMapping) -> dict:
    return {"system_id": m.system_id, "symbol": m.symbol_name, "path": m.path,
            "status": m.status, "origin": m.origin, "score": m.score}
