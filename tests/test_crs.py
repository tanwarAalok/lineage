import subprocess

import pytest

from conftest import write


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Dev", "-c", "user.email=dev@example.com", *args],
                   check=True, capture_output=True)


@pytest.fixture
def crs(env):
    """CR-1 (gift wrap) agreed and partly built; CR-2 builds on it."""
    app, root, alice = env.app, env.root, env.who("alice")
    app.save_cr(root, "CR-1", "Gift wrap", "Customers can add gift wrapping.", "in_progress")
    app.propose_contract(alice, "CR-1", "billing", "REST POST /invoices",
                         '{"giftWrap": {"enabled": bool, "message": "string <= 200"}}', "crm", "API review 2026-10-01",
                         approve=True)
    app.log_decision(alice, "CR-1", "Who prices gift wrap?", "billing, never the client", system="billing", approve=True)
    app.log_decision(env.who("bob"), "CR-1", "Max message length?", "200 characters")  # proposal only
    app.save_cr(root, "CR-2", "Scheduled gift delivery", "Pick a delivery date.", depends_on=["CR-1"])
    return env


def test_only_admins_and_maintainers_register_crs(crs):
    with pytest.raises(PermissionError):
        crs.app.save_cr(crs.who("bob"), "CR-9", "Mine")
    crs.app.save_cr(crs.who("alice"), "CR-9", "Maintainers may")
    with pytest.raises(ValueError):
        crs.app.save_cr(crs.root, "CR-9", "Self", depends_on=["cr-9"])
    with pytest.raises(ValueError):
        crs.app.save_cr(crs.root, "not a cr", "Bad id")


def test_contracts_are_versioned_and_agreed_by_the_provider(crs):
    app, alice, bob = crs.app, crs.who("alice"), crs.who("bob")
    v2 = app.propose_contract(bob, "CR-1", "billing", "REST POST /invoices", '{"giftWrap": {"message": "<= 150"}}')
    assert v2 == {"cr": "CR-1", "provider": "billing", "name": "REST POST /invoices", "version": 2, "status": "proposed"}
    with pytest.raises(PermissionError):
        app.propose_contract(bob, "CR-1", "billing", "REST POST /invoices", "x", approve=True)
    with pytest.raises(PermissionError):  # alice maintains billing, not crm
        app.propose_contract(alice, "CR-1", "crm", "event customer.updated", "x", approve=True)
    with pytest.raises(LookupError):
        app.propose_contract(bob, "CR-404", "billing", "x", "y")

    current = app.contracts(bob, cr_id="CR-1")[0]
    assert (current["version"], current["status"], current["pending_version"]["version"]) == (1, "agreed", 2)
    history = app.contracts(bob, cr_id="CR-1", history=True)
    assert [c["version"] for c in history] == [1, 2]  # nothing overwritten

    app.review_contract(alice, history[1]["id"], "agreed")
    assert app.contracts(bob, cr_id="CR-1")[0]["version"] == 2


def test_a_cr_inherits_everything_it_builds_on(crs):
    ctx = crs.app.cr_context(crs.who("carol"), "cr-2")
    assert ctx["cr"] == "CR-2" and ctx["status"] == "proposed"
    parent = ctx["builds_on"][0]
    assert (parent["cr"], parent["depth"], parent["title"]) == ("CR-1", 1, "Gift wrap")
    assert parent["contracts"][0]["name"] == "REST POST /invoices"
    assert [d["decision"] for d in parent["decisions"]] == ["billing, never the client"]  # approved only
    assert any("CR-1 (in_progress)" in w for w in ctx["warnings"])

    inherited = crs.app.contracts(crs.who("carol"), cr_id="CR-2")
    assert inherited[0]["cr"] == "CR-1" and inherited[0]["inherited"] is True

    crs.app.save_cr(crs.root, "CR-3", "Gift receipts", depends_on=["CR-2"])  # transitive: CR-3 -> CR-2 -> CR-1
    chain = [(a["cr"], a["depth"]) for a in crs.app.cr_context(crs.root, "CR-3")["builds_on"]]
    assert chain == [("CR-2", 1), ("CR-1", 2)]
    assert crs.app.cr_context(crs.root, "CR-1")["built_on_by"] == [{"cr": "CR-2", "kind": "depends_on"}]


def test_warns_when_the_foundation_changes_after_a_cr_started(crs):
    app, alice = crs.app, crs.who("alice")
    with app.open_store() as store:  # CR-2 started well before CR-1's contract changes
        store._execute("update crs set created_at = '2026-01-01T00:00:00+00:00' where id = 'CR-2'")
    app.propose_contract(alice, "CR-1", "billing", "REST POST /invoices", '{"message": "<= 150"}', approve=True)
    warnings = app.cr_context(crs.root, "CR-2")["warnings"]
    assert any("changed to v2" in w and "after CR-2 was created" in w for w in warnings)


def test_code_and_commits_link_themselves_to_crs(crs):
    app, root = crs.app, crs.root
    write(crs.billing, "billing/gift.py", "def gift_wrap_price(order):\n    # CR-1: wrap price is set by billing\n    return 50\n")
    app.index_project(root, str(crs.billing), "billing")

    git(crs.crm, "init", "-q")
    git(crs.crm, "add", ".")
    git(crs.crm, "commit", "-q", "-m", "baseline")
    write(crs.crm, "crm/gift_prefs.py", "def remember_gift_choice(customer):\n    return True\n")
    git(crs.crm, "add", ".")
    git(crs.crm, "commit", "-q", "-m", "CR-1: remember gift-wrap preference")
    app.index_project(root, str(crs.crm), "crm")

    built = app.cr_context(crs.who("bob"), "CR-1")["implemented_in"]
    assert built["billing"]["files"] == [{"path": "billing/gift.py", "lines": [2], "symbols": ["gift_wrap_price"]}]
    assert built["crm"]["files"][0]["path"] == "crm/gift_prefs.py"
    assert "CR-1: remember gift-wrap preference" in built["crm"]["commits"][0]

    write(crs.billing, "billing/gift.py", "def gift_wrap_price(order):\n    return 50\n")  # mention removed
    app.index_project(root, str(crs.billing), "billing")
    assert "billing" not in app.cr_context(root, "CR-1")["implemented_in"]


def test_warns_when_code_predates_the_current_contract(crs):
    app, root = crs.app, crs.root
    git(crs.crm, "init", "-q")
    git(crs.crm, "add", ".")
    subprocess.run(["git", "-C", str(crs.crm), "-c", "user.name=Dev", "-c", "user.email=d@x", "commit", "-q",
                    "-m", "CR-1: customer gift flag", "--date", "2026-01-15T10:00:00"],
                   check=True, capture_output=True, env={**__import__("os").environ, "GIT_COMMITTER_DATE": "2026-01-15T10:00:00"})
    app.index_project(root, str(crs.crm), "crm")
    app.propose_contract(root, "CR-1", "crm", "event customer.updated", '{"gift": bool}', approve=True)
    assert not any("predates" in w for w in app.cr_context(root, "CR-1")["warnings"])  # v1: nothing to drift from
    app.propose_contract(root, "CR-1", "crm", "event customer.updated", '{"gift": bool, "giftNote": str}', approve=True)
    warnings = app.cr_context(root, "CR-2")["warnings"]  # seen from the CR that builds on it, too
    assert any(w.startswith("crm provides 'crm event customer.updated', which changed to v2") and "2026-01-15" in w
               for w in warnings)

    # Consumers drift too: billing consumes crm's event and its CR-1 code is older than v3.
    git(crs.billing, "init", "-q")
    git(crs.billing, "add", ".")
    subprocess.run(["git", "-C", str(crs.billing), "-c", "user.name=Dev", "-c", "user.email=d@x", "commit", "-q",
                    "-m", "CR-1: bill gift orders", "--date", "2026-02-01T10:00:00"],
                   check=True, capture_output=True, env={**__import__("os").environ, "GIT_COMMITTER_DATE": "2026-02-01T10:00:00"})
    app.index_project(root, str(crs.billing), "billing")
    app.propose_contract(root, "CR-1", "crm", "event customer.updated", '{"gift": bool, "giftNote": str, "x": 1}',
                         consumers="billing", approve=True)
    ctx = app.cr_context(root, "CR-1", project="billing")
    assert any(w.startswith("billing consumes 'crm event customer.updated', which changed to v3") for w in ctx["warnings"])
    mine = ctx["for_this_repository"]  # the asking repo's own drift comes first, with the new spec and its files
    assert list(ctx)[0] == "for_this_repository" and mine[0]["repository"] == "billing"
    assert '"x": 1' in mine[0]["contract_now_says"] and "billing/invoice.py" in mine[0]["your_files"]


def test_unregistered_crs_and_cycles_are_safe(crs):
    app, root = crs.app, crs.root
    write(crs.billing, "billing/legacy.py", "# CR-77 hotfix from 2019\nRATE = 3\n")
    app.index_project(root, str(crs.billing), "billing")
    ctx = app.cr_context(root, "CR-77")
    assert ctx["registered"] is False and ctx["implemented_in"]["billing"]["files"][0]["path"] == "billing/legacy.py"
    assert any("not registered" in w for w in ctx["warnings"])

    app.save_cr(root, "CR-1", "Gift wrap", depends_on=["CR-2"])  # CR-1 <-> CR-2 cycle
    assert {a["cr"] for a in app.cr_context(root, "CR-2")["builds_on"]} == {"CR-1"}
