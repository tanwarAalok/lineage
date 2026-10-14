"""Incremental, parallel indexing built for large legacy repos (20k+ files).

Parser processes read, hash and parse files; the main process embeds and writes. Only files whose
content hash changed are re-processed; every file is its own transaction, so an interrupted run
simply resumes on the next one. A file that fails to parse is logged and skipped, never fatal.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from lineage.config import Settings
from lineage.ingest.embed import Embedder
from lineage.ingest.parse import file_hash, parse, read_indexable, walk
from lineage.models import ParsedFile
from lineage.store.base import Store

log = logging.getLogger(__name__)

EMBED_BATCH = 256  # chunks per embedding call, across files
WINDOW = 512  # files in flight at once: bounds memory however big the repo is
PARALLEL_FROM = 300  # below this many files, process start-up costs more than it saves
PROGRESS_EVERY_S = 2.0
BULK_LOAD_FILES = 2000  # this many new files lets the store defer index upkeep to one build

Progress = Callable[[dict], None]


@dataclass
class IndexReport:
    system_id: str
    total: int = 0
    indexed: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    redactions: int = 0
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _work(task: tuple[str, str, str | None, int]) -> tuple[str, str, object]:
    """Runs in a parser process. -> (rel, outcome, payload)."""
    root, rel, known_hash, max_bytes = task
    try:
        data = read_indexable(Path(root, rel), max_bytes)
        if data is None:
            return rel, "skipped", None
        if file_hash(data) == known_hash:
            return rel, "unchanged", None
        return rel, "parsed", parse(rel, data)
    except Exception as e:  # one strange legacy file must not stop a 20k-file run
        return rel, "failed", f"{type(e).__name__}: {e}"


def index_repo(root: Path, system_id: str, settings: Settings, embedder: Embedder, store: Store,
               progress: Progress | None = None) -> IndexReport:
    started = time.monotonic()
    report = IndexReport(system_id)
    store.bind_embedder(embedder.model_id, embedder.dim)
    store.ensure_system(system_id, root.name, str(root))
    known = store.file_hashes(system_id)
    paths = list(walk(root, settings))
    store.begin_bulk(large=len(set(paths) - set(known)) >= BULK_LOAD_FILES)
    report.total = len(paths)
    keep: set[str] = set()  # paths whose index entry stays (indexed, unchanged or failed)
    pending: list[ParsedFile] = []
    last_report = 0.0

    def flush() -> None:
        texts = [c.text for p in pending for c in p.chunks]
        vectors = embedder.embed(texts) if texts else []
        batch, i = [], 0
        for p in pending:
            batch.append((p, vectors[i:i + len(p.chunks)]))
            i += len(p.chunks)
        store.replace_files(system_id, batch)  # one transaction, a few round trips, for the whole batch
        report.indexed += len(pending)
        report.redactions += sum(p.redactions for p in pending)
        pending.clear()

    def tell(phase: str, force: bool = False) -> None:
        nonlocal last_report
        if progress and (force or time.monotonic() - last_report >= PROGRESS_EVERY_S):
            last_report = time.monotonic()
            progress({"phase": phase, **report.to_dict(), "seconds": round(time.monotonic() - started, 1)})

    workers = settings.index_workers or min(8, os.cpu_count() or 1)
    pool = ProcessPoolExecutor(workers) if workers > 1 and len(paths) >= PARALLEL_FROM else None
    try:
        for w in range(0, len(paths), WINDOW):
            tasks = [(str(root), rel, known.get(rel), settings.max_file_bytes) for rel in paths[w:w + WINDOW]]
            results = pool.map(_work, tasks, chunksize=8) if pool else map(_work, tasks)
            for rel, outcome, payload in results:
                if outcome == "skipped":
                    report.skipped += 1
                    continue
                keep.add(rel)
                if outcome == "unchanged":
                    report.unchanged += 1
                elif outcome == "failed":
                    report.failed += 1
                    log.warning("could not parse %s: %s", rel, payload)
                else:
                    pending.append(payload)
                    if sum(len(p.chunks) for p in pending) >= EMBED_BATCH:
                        flush()
                tell("indexing")
        flush()
    finally:
        if pool:
            pool.shutdown(cancel_futures=True)
        store.end_bulk()

    gone = sorted(set(known) - keep)
    store.delete_files(system_id, gone)
    report.deleted = len(gone)
    report.seconds = round(time.monotonic() - started, 2)
    tell("done", force=True)
    log.info("indexed %s: %s", system_id, report)
    return report
