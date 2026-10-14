import pytest


def test_tokens_authenticate_and_can_be_revoked(env):
    app = env.app
    alice = env.who("alice")
    assert alice.name == "alice" and alice.roles == {"billing": "maintainer"} and not alice.is_admin
    assert app.authenticate("lin_wrong") is None and app.authenticate(None) is None

    new = app.rotate_token(env.root, "alice")["token"]
    assert app.authenticate(env.tokens["alice"]) is None  # old token dead
    assert app.authenticate(new).name == "alice"

    app.set_disabled(env.root, "alice", True)
    assert app.authenticate(new) is None


def test_everyone_is_a_developer_on_every_project(env):
    app, carol = env.app, env.who("carol")
    projects = {p["id"]: p["role"] for p in app.list_projects(carol)}
    assert projects == {"billing": "developer", "crm": "developer"}
    assert app.search_code(carol, "generate monthly invoice", project="billing")[0]["path"] == "billing/invoice.py"
    assert app.save_term(carol, "crm", "Lead", "A prospect.")["status"] == "proposed"
    assert {p["id"]: p["role"] for p in app.list_projects(env.who("alice"))}["billing"] == "maintainer"


def test_only_admins_onboard_and_manage_people(env):
    app, alice, bob = env.app, env.who("alice"), env.who("bob")
    with pytest.raises(PermissionError):
        app.start_index_job(alice, str(env.crm), "new-project")  # maintainer elsewhere, not admin
    with pytest.raises(PermissionError):
        app.reindex(bob, "billing")  # developers cannot re-index
    with pytest.raises(PermissionError):
        app.reindex(alice, "crm")  # alice maintains billing only
    for call in (lambda: app.create_user(alice, "mallory"), lambda: app.users(alice),
                 lambda: app.set_maintainer(alice, "bob", "billing", True)):
        with pytest.raises(PermissionError):
            call()


def test_developers_propose_maintainers_decide(env):
    app, alice, bob = env.app, env.who("alice"), env.who("bob")
    assert app.save_term(bob, "billing", "Dunning", "Chasing unpaid invoices.")["status"] == "proposed"
    with pytest.raises(PermissionError):
        app.save_term(bob, "billing", "Dunning", "Mine now.", approve=True)
    term_id = app.terms(alice, "billing")[0]["id"]
    with pytest.raises(PermissionError):
        app.review(bob, "billing", "term", term_id, "approved")

    app.review(alice, "billing", "term", term_id, "approved")
    with pytest.raises(ValueError, match="already approved"):
        app.save_term(bob, "billing", "dunning", "Overwrite attempt.")  # case-insensitive match
    assert app.terms(bob, "billing")[0]["definition"] == "Chasing unpaid invoices."

    app.save_rule(bob, "billing", "Tests", "Every bug fix gets a test.")
    rule_id = app.rules(alice, "billing")[0]["id"]
    with pytest.raises(PermissionError):
        app.save_rule(bob, "billing", "Tests", "Edited by a developer", rule_id=rule_id)


def test_ids_from_another_project_never_match(env):
    app, root = env.app, env.root
    app.save_term(root, "billing", "Bill run", "Monthly batch.", approve=True)
    term_id = app.terms(root, "billing")[0]["id"]
    app.set_maintainer(root, "alice", "crm", True)
    alice = env.who("alice")
    with pytest.raises(LookupError):
        app.review(alice, "crm", "term", term_id, "rejected")  # billing's id via crm
    with pytest.raises(LookupError):
        app.delete(alice, "crm", "term", term_id)
    assert app.terms(alice, "billing")[0]["status"] == "approved"
