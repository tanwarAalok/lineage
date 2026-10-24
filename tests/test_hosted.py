"""What a shared, hosted database needs beyond a local one."""

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from lineage.service import INSTANCE, Lineage
from lineage.store.sql import SCHEMA_VERSION


def test_remote_connections_require_tls_by_default():
    pytest.importorskip("psycopg")
    from lineage.store.postgres import describe, secure_conninfo

    assert "sslmode=require" in secure_conninfo("postgresql://app:pw@db.example.com/eng")
    assert "sslmode=verify-full" in secure_conninfo("postgresql://app:pw@db.example.com/eng?sslmode=verify-full")
    assert "sslmode" not in secure_conninfo("postgresql://app:pw@localhost/eng")
    shown = describe("postgresql://app:s3cret@db.example.com:6432/eng?sslmode=require")
    assert "s3cret" not in shown and "db.example.com:6432/eng" in shown


def _reopen(app: Lineage):
    """Run startup (schema create/migrate) again, as a new server process would."""
    with app.open_store() as store:
        if hasattr(store, "_p"):
            store._p._initialize()
    return app.open_store()


def test_upgrade_migrates_and_keeps_team_data(env):
    app, root = env.app, env.root
    app.save_rule(root, "billing", "Money", "Use Decimal.", approve=True)
    with app.open_store() as store:  # turn the database back into the v3 shape
        store._execute("drop index jobs_one_active")
        store._execute("alter table jobs drop column heartbeat_at")
        store._execute("alter table jobs drop column owner")
        store._execute("alter table systems drop column description")
        store._set_meta("schema_version", "3")
    with _reopen(app) as store:
        assert store._meta("schema_version") == str(SCHEMA_VERSION)
        assert store.create_job("billing", "index", "root", INSTANCE) is not None  # new columns work
    assert [r["title"] for r in app.rules(root, "billing")] == ["Money"]
    assert app.authenticate(env.tokens["bob"]).name == "bob"


def test_refuses_a_database_newer_than_this_code(env):
    with env.app.open_store() as store:
        store._set_meta("schema_version", "99")
    with pytest.raises(RuntimeError, match="newer than this Lineage"):
        with _reopen(env.app):
            pass


def test_one_active_job_per_project_across_servers(env):
    server_a, server_b = env.app, Lineage(env.settings)  # two servers, one database
    with server_a.open_store() as a, server_b.open_store() as b:
        assert a.create_job("billing", "index", "root", "host-a:1") is not None
        assert b.create_job("billing", "index", "root", "host-b:2") is None
        assert b.create_job("crm", "index", "root", "host-b:2") is not None


def test_restart_only_interrupts_abandoned_jobs(env):
    app = env.app
    with app.open_store() as store:
        live = store.create_job("billing", "index", "root", "host-a:1")
        dead = store.create_job("crm", "index", "root", "host-b:2")
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        store._execute("update jobs set heartbeat_at = ? where id = ?", (old, dead))
    Lineage(env.settings).recover_jobs()  # another server starting up
    with app.open_store() as store:
        status = {j["id"]: j["status"] for j in store.jobs(None)}
    assert status[live] == "queued" and status[dead] == "interrupted"


def test_db_check_reports_the_database(env):
    info = env.app.db_check()
    assert info["schema_version"] == str(SCHEMA_VERSION) and info["rows"]["systems"] == 2
    if info["database"] == "postgres":
        assert info["pgvector"] and info["round_trip_ms"] >= 0 and "password" not in info["connection"]
    else:
        assert any("hosted Postgres" in w for w in info["warnings"])


def test_least_privilege_role_with_dba_prepared_schema(postgres_url, tmp_path):
    """Many DBAs grant an app role only its own schema: no CREATE on the database, no extensions."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    from lineage.config import Settings

    with psycopg.connect(postgres_url, autocommit=True) as admin:
        admin.execute("create extension if not exists vector schema public")
        admin.execute("drop schema if exists app cascade")
        admin.execute("drop role if exists lineage_app")
        admin.execute("create role lineage_app login password 'app-pw' nosuperuser nocreatedb nocreaterole")
        admin.execute("revoke create on database postgres from public")
        admin.execute("create schema app authorization lineage_app")
    p = conninfo_to_dict(postgres_url)
    app_url = f"postgresql://lineage_app:app-pw@{p['host']}:{p['port']}/{p['dbname']}"
    repos = tmp_path / "repos"
    (repos / "svc").mkdir(parents=True)
    (repos / "svc" / "orders.py").write_text('"""Orders."""\ndef place_order(c):\n    return c\n', encoding="utf-8")
    app = Lineage(Settings(db_url=app_url, db_schema="app", embedder="hash",
                           allowed_roots=(str(repos),), user="root", index_workers=1))
    app.index_project(app.local_principal(), str(repos / "svc"), "svc")
    assert app.search_code(app.local_principal(), "place order")[0]["path"] == "orders.py"
