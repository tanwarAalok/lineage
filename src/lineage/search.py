"""Hybrid retrieval (FTS5 keywords + vectors, fused with reciprocal rank) and term resolution."""

from __future__ import annotations

from collections import defaultdict

from lineage.ingest.embed import Embedder
from lineage.models import SearchHit
from lineage.store.base import ChunkRow, Scope, Store, TermMapping
from lineage.text import query_terms, tokens

RRF_K = 60
# Wide candidate pools matter: large generated files (API schemas, fixtures) otherwise fill the
# slots and crowd the right file out. Measured on a 4.6k-file repo: 60 -> 300 took top-10 from 92% to 100%.
CANDIDATES = 300
MAX_TOP_N = 25
TERM_CANDIDATES = 5
TIE_BREAK = 0.01
KEYWORD_WEIGHT = 1.0
DENSE_WEIGHT = 1.0


def _rank_chunks(store: Store, embedder: Embedder, query: str, systems: Scope,
                 vector: list[float] | None = None) -> tuple[list[tuple[int, float]], dict[int, ChunkRow]]:
    keyword = store.keyword_search(query_terms(query) or tokens(query), systems, CANDIDATES)
    dense = store.vector_search(vector or embedder.embed([query])[0], systems, CANDIDATES)
    fused: dict[int, float] = defaultdict(float)
    for ranked, weight in ((keyword, KEYWORD_WEIGHT), (dense, DENSE_WEIGHT)):
        for rank, (chunk_id, _) in enumerate(ranked):
            fused[chunk_id] += weight / (RRF_K + rank + 1)
    rows = store.chunks(list(fused))
    for cid, row in rows.items():  # implementation code first: tests, migrations and docs rank lower
        fused[cid] *= path_prior(row.path)
    return sorted(fused.items(), key=lambda kv: -kv[1]), rows


TEST_MARKERS = ("/tests/", "/test/", "/__tests__/", "/spec/")
DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")


def path_prior(path: str) -> float:
    """How likely a file is the code to change for a requirement. Tests, migrations and docs stay
    findable but rank below implementation files."""
    p = "/" + path.lower()
    name = p.rsplit("/", 1)[-1]
    if "/migrations/" in p:
        return 0.3
    if any(m in p for m in TEST_MARKERS) or name.startswith("test_") or name.endswith(
            ("_test.py", "_test.go", ".test.ts", ".test.tsx", ".spec.ts", ".spec.js", "test.java", "tests.java")):
        return 0.5
    if name.endswith(DOC_SUFFIXES) or "/docs/" in p:
        return 0.6
    return 1.0


def search_code(store: Store, embedder: Embedder, query: str, systems: Scope, top_n: int = 8) -> list[SearchHit]:
    """Top files for a query, from two rankings fused at file level:
    - by best chunk (max-passage: summing chunks would let ten getters mentioning "total" outvote
      calculateOrderTotal; the second-best chunk only breaks ties), and
    - by the file's plain-language card (path, summary, symbols as words), which matches vague,
      business-language requirements that the code itself does not.
    Measured on a 4.6k-file repo with 24 vague requirements: top-10 75% -> 92%, MRR 0.30 -> 0.48."""
    top_n = max(1, min(top_n, MAX_TOP_N))
    vector = embedder.embed([query])[0]
    ranked, rows = _rank_chunks(store, embedder, query, systems, vector)
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
    by_chunks = sorted(files, key=lambda k: -files[k]["score"])
    cards = store.file_vector_search(vector, systems, CANDIDATES)
    for c in cards:
        c["distance"] = 1 - (1 - c["distance"]) * path_prior(c["path"])  # same implementation-first prior
    by_cards = [(c["system_id"], c["path"]) for c in sorted(cards, key=lambda c: c["distance"])]
    fused: dict[tuple[str, str], float] = defaultdict(float)
    for order in (by_chunks, by_cards):
        for rank, key in enumerate(order):
            fused[key] += 1.0 / (RRF_K + rank + 1)
    summaries = {(c["system_id"], c["path"]): c["summary"] for c in cards}
    hits = []
    for key in sorted(fused, key=lambda k: -fused[k])[:top_n]:
        system_id, path = key
        f = files.get(key)
        if f is None:  # found by its card only
            hits.append(SearchHit(system_id, path, summaries[key], round(fused[key], 4), [], (1, 1)))
            continue
        best: ChunkRow = f["best"]
        meaning = best.file_summary
        if best.symbol_name:
            meaning += f" | best match: {best.symbol_kind} {best.symbol_name} — {best.symbol_summary}"
        hits.append(SearchHit(system_id, path, meaning, round(fused[key], 4),
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
