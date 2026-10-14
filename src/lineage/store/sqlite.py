"""SQLite dialect: one local file. FTS5 for keywords/exact names, sqlite-vec for vectors."""

from __future__ import annotations

import itertools
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Sequence

import sqlite_vec

from lineage.store.base import Scope
from lineage.store.sql import COMMON_DDL, SqlStore, scope_clause
from lineage.text import tokens

DDL = COMMON_DDL.format(id="integer primary key", ref="integer") + """
create table if not exists chunks(
    id integer primary key,
    file_id integer not null references files(id) on delete cascade,
    symbol_id integer references symbols(id) on delete cascade,
    system_id text not null, start_line integer not null, end_line integer not null);
create index if not exists chunks_file on chunks(file_id);
create virtual table if not exists chunks_fts using fts5(tokens, tokenize='unicode61');
"""

_savepoints = itertools.count()


def _dict_row(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {col[0]: value for col, value in zip(cursor.description, row)}


class SQLiteStore(SqlStore):
    def __init__(self, path: str):
        file = Path(path).expanduser()
        file.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit; _tx() opens explicit transactions, like the Postgres store.
        self.db = sqlite3.connect(file, timeout=30, check_same_thread=False, isolation_level=None)
        self.db.row_factory = _dict_row
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.execute("pragma foreign_keys=on")
        self.db.execute("pragma synchronous=normal")
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript(DDL)  # idempotent and sub-millisecond, so it simply runs on every open
        with self._tx():
            self._migrate()

    def _server_info(self) -> dict:
        return {"database": "sqlite (single machine: POC and tests only)",
                "path": self.db.execute("pragma database_list").fetchone()["file"]}

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.db.close()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self.db.execute(sql, params)

    def _executemany(self, sql: str, rows: list[Sequence[Any]]) -> None:
        self.db.executemany(sql, rows)

    @contextmanager
    def _tx(self):
        if self.db.in_transaction:  # nested: a savepoint, so an inner failure undoes only its part
            name = f"sp{next(_savepoints)}"
            self.db.execute(f"savepoint {name}")
            try:
                yield
            except BaseException:
                self.db.execute(f"rollback to {name}")
                self.db.execute(f"release {name}")
                raise
            self.db.execute(f"release {name}")
            return
        self.db.execute("begin immediate")  # take the write lock up front: no lock-upgrade deadlocks
        try:
            yield
        except BaseException:
            self.db.execute("rollback")
            raise
        self.db.execute("commit")

    def _create_vector_index(self, dim: int) -> None:
        self.db.execute(f"create virtual table if not exists chunks_vec using vec0("
                        f"embedding float[{dim}] distance_metric=cosine, system_id text)")

    def _insert_chunks(self, system_id: str, rows: list[tuple]) -> None:
        for file_id, symbol_id, start, end, text, vec in rows:
            cid = self._one(
                "insert into chunks(file_id, symbol_id, system_id, start_line, end_line) values (?,?,?,?,?) returning id",
                (file_id, symbol_id, system_id, start, end))["id"]
            self.db.execute("insert into chunks_fts(rowid, tokens) values (?, ?)", (cid, " ".join(tokens(text))))
            self.db.execute("insert into chunks_vec(rowid, embedding, system_id) values (?, ?, ?)",
                            (cid, sqlite_vec.serialize_float32(vec), system_id))

    def _delete(self, system_id: str, paths: Sequence[str]) -> None:
        for path in paths:
            ids = [(r["id"],) for r in self._all(
                "select c.id from chunks c join files f on f.id = c.file_id where f.system_id = ? and f.path = ?",
                (system_id, path))]
            if ids:  # virtual tables are outside foreign-key cascades
                self.db.executemany("delete from chunks_fts where rowid = ?", ids)
                self.db.executemany("delete from chunks_vec where rowid = ?", ids)
            self._execute("delete from files where system_id = ? and path = ?", (system_id, path))

    def keyword_search(self, terms: Sequence[str], systems: Scope, limit: int) -> list[tuple[int, float]]:
        if not terms:
            return []
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        clause, params = scope_clause("c.system_id", systems)
        rows = self._all(
            "select f.rowid as id, -bm25(chunks_fts) as score from chunks_fts f join chunks c on c.id = f.rowid"
            f" where chunks_fts match ?{clause} order by bm25(chunks_fts) limit ?", [match, *params, limit])
        return [(r["id"], r["score"]) for r in rows]

    def vector_search(self, vector: list[float], systems: Scope, limit: int) -> list[tuple[int, float]]:
        if self._meta("embedder") is None:
            return []
        clause, params = scope_clause("system_id", systems)
        rows = self._all(
            f"select rowid as id, distance from chunks_vec where embedding match ? and k = ?{clause} order by distance",
            [sqlite_vec.serialize_float32(vector), limit, *params])
        return [(r["id"], r["distance"]) for r in rows]
