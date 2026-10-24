import dataclasses
import time

import pytest

from conftest import write
from lineage.service import Lineage


def test_search_finds_relevant_file_with_meaning(env):
    hits = env.app.search_code(env.root, "generate monthly invoice")
    assert hits[0]["path"] == "billing/invoice.py"
    assert "Invoice generation" in hits[0]["meaning"]
    assert "generate_invoice" in hits[0]["symbols"]
    assert {h["system_id"] for h in env.app.search_code(env.root, "invoice", project="crm")} <= {"crm"}


def test_cross_system_search_with_star(env):
    bob = env.who("bob")
    everywhere = {h["system_id"] for h in env.app.search_code(bob, "load customer invoice", project="*")}
    assert everywhere == {"billing", "crm"}
    assert env.app.resolve_term(bob, "invoice service", project="*")["mappings"]


def test_agents_learn_about_the_other_systems(env):
    app, root, bob = env.app, env.root, env.who("bob")
    app.set_description(root, "crm", "backend: customer profiles")
    with pytest.raises(PermissionError):
        app.set_description(bob, "crm", "mine")  # developers cannot
    ctx = app.project_context(bob, "billing")
    assert ctx["other_systems"] == [{"project": "crm", "description": "backend: customer profiles"}]
    assert "read_code" in ctx["other_systems_access"]
    md = app.assets(bob, "billing")["copilot-instructions.md"]
    assert "`crm` - backend: customer profiles" in md and "not in this workspace" in md
    assert md.index("## Systems") < md.index("## Project rules")  # up front, where agents look first


def test_read_code_from_another_project(env):
    bob, app = env.who("bob"), env.app
    code = app.read_code(bob, "billing/invoice.py", "billing", 8, 10)
    assert code["start_line"] == 8 and "fetch_usage(account_id)" in code["code"]
    assert "hunter2pass" not in app.read_code(bob, "billing/config.py", "billing")["code"]  # redacted
    for path in (".env", "../crm/crm/customer.py", "billing/missing.py"):  # secret, traversal, unknown
        with pytest.raises((LookupError, PermissionError)):
            app.read_code(bob, path, "billing")


def test_secrets_never_indexed(env):
    app, root = env.app, env.root
    with app.open_store() as store:
        assert ".env" not in store.file_hashes("billing")
    for secret in ("hunter2pass", "AKIAABCDEFGHIJKLMNOP", "supersecretvalue123"):
        assert all("config.py" not in h["path"] or secret.lower() not in h["meaning"].lower()
                   for h in app.search_code(root, secret))
    assert not any(h["path"] == ".env" for h in app.search_code(root, "API_KEY supersecretvalue123"))


def test_incremental_reindex(env):
    app, root, billing = env.app, env.root, env.billing
    report = app.index_project(root, str(billing), "billing")
    assert (report["indexed"], report["unchanged"], report["deleted"]) == (0, 3, 0)

    write(billing, "web/cart.js", "// Checkout discount rules\nfunction applyDiscount(total) { return total; }\n")
    (billing / "billing/config.py").unlink()
    report = app.index_project(root, str(billing), "billing")
    assert (report["indexed"], report["unchanged"], report["deleted"]) == (1, 1, 1)
    assert app.search_code(root, "checkout discount")[0]["path"] == "web/cart.js"


def test_background_job_reports_progress(env):
    app, alice = env.app, env.who("alice")
    write(env.billing, "billing/refund.py", '"""Refunds."""\ndef refund(x):\n    return x\n')
    job = app.reindex(alice, "billing")
    assert job["status"] == "queued"
    for _ in range(100):
        jobs = app.jobs(alice, "billing")
        if jobs[0]["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert jobs[0]["status"] == "done" and jobs[0]["progress"]["indexed"] == 1
    assert app.list_projects(alice)[0]["last_job"]["status"] == "done"


def test_call_graph_and_assets(env):
    with env.app.open_store() as store:
        hubs = {h["name"] for h in store.project_stats("billing")["hubs"]}
    assert {"fetch_usage", "apply_tax"} <= hubs
    assert not (env.billing / ".lineage").exists()  # never writes into repositories (copies go stale)
    agents = env.app.assets(env.root, "billing")["AGENTS.md"]
    assert "get_project_context" in agents and "fetch_usage" in agents


def test_dictionary_rules_context_and_provenance(env):
    app, root = env.app, env.root
    app.save_term(root, "billing", "Bill run", "Monthly batch that generates invoices.", approve=True,
                  source="HLD v3 §2.1")
    app.save_rule(root, "billing", "Money", "Use Decimal for amounts, never float.", approve=True)
    app.save_rule(env.who("bob"), "billing", "Logging", "Use structlog.", source="team chat")  # proposal only

    ctx = app.project_context(env.who("bob"), "billing")
    assert ctx["dictionary"] == [{"term": "Bill run", "definition": "Monthly batch that generates invoices."}]
    assert [r["title"] for r in ctx["rules"]] == ["Money"]  # proposals never reach agents
    assert app.terms(env.who("bob"), "billing")[0]["source"] == "HLD v3 §2.1"

    agents = app.assets(env.who("bob"), "billing")["copilot-instructions.md"]
    assert "Use Decimal for amounts" in agents and "Bill run" in agents and "structlog" not in agents
    assert app.resolve_term(env.who("bob"), "BILL RUN", "billing")["definitions"][0]["definition"].startswith("Monthly")


def test_code_link_lifecycle(env):
    app, alice, bob = env.app, env.who("alice"), env.who("bob")
    proposed = app.resolve_term(bob, "invoice service", "billing")
    assert proposed["status"] == "proposed"
    top = proposed["mappings"][0]
    assert top["symbol"] == "InvoiceService" and top["origin"] == "inferred"

    link = next(m for m in app.mappings(alice, "billing") if m["symbol_name"] == "InvoiceService")
    app.review(alice, "billing", "mapping", link["id"], "approved")
    approved = app.resolve_term(bob, "invoice service", "billing")
    assert approved["status"] == "approved"
    assert [m["symbol"] for m in approved["mappings"]] == ["InvoiceService"]

    # A later suggestion never overrides the maintainer's review.
    app.link_term(bob, "billing", "invoice service", "InvoiceService", "billing/invoice.py")
    assert app.resolve_term(bob, "invoice service", "billing")["status"] == "approved"


def test_guards(env, tmp_path):
    app, root = env.app, env.root
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(PermissionError):
        app.index_project(root, str(outside))
    with pytest.raises(ValueError):
        app.index_project(root, str(env.billing), "Bad ID!")
    with pytest.raises(ValueError):
        app.search_code(root, "x" * 5000)
    with pytest.raises(RuntimeError, match="embedder"):
        Lineage(dataclasses.replace(env.settings, embedder="hash:256")).index_project(root, str(env.billing), "billing")
