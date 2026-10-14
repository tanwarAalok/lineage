import pytest
from starlette.testclient import TestClient

from lineage.server import build_server
from lineage.web import register_web


@pytest.fixture
def client(env):
    mcp, app = build_server(env.settings, http=True)
    register_web(mcp, app)
    with TestClient(mcp.streamable_http_app()) as c:
        yield c


def auth(env, name):
    return {"Authorization": f"Bearer {env.tokens[name]}"}


def test_page_served_with_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "Lineage" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert client.get("/static/app.js").status_code == 200


def test_api_requires_a_valid_token(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer lin_nope"}).status_code == 401


def test_member_and_maintainer_flow(client, env):
    me = client.get("/api/me", headers=auth(env, "bob")).json()
    assert me == {"name": "bob", "is_admin": False, "maintains": []}

    r = client.post("/api/projects/billing/terms", headers=auth(env, "bob"),
                    json={"term": "Bill run", "definition": "Monthly invoice batch", "approve": True})
    assert r.status_code == 403  # members cannot approve
    r = client.post("/api/projects/billing/terms", headers=auth(env, "bob"),
                    json={"term": "Bill run", "definition": "Monthly invoice batch"})
    assert r.json()["status"] == "proposed"

    term_id = client.get("/api/projects/billing/terms", headers=auth(env, "bob")).json()[0]["id"]
    assert client.post("/api/projects/billing/review", headers=auth(env, "bob"),
                       json={"kind": "term", "id": term_id, "status": "approved"}).status_code == 403
    assert client.post("/api/projects/billing/review", headers=auth(env, "alice"),
                       json={"kind": "term", "id": term_id, "status": "approved"}).status_code == 200

    md = client.get("/api/projects/billing/assets/copilot-instructions.md", headers=auth(env, "bob"))
    assert md.status_code == 200 and "Monthly invoice batch" in md.text
    assert "attachment" in md.headers["content-disposition"]


def test_api_rejects_bad_input_and_foreign_projects(client, env):
    assert client.post("/api/projects/crm/review", headers=auth(env, "alice"),
                       json={"kind": "term", "id": 1, "status": "approved"}).status_code == 403  # maintains billing only
    assert client.get("/api/projects/billing/assets/..%2F..%2Fsecret", headers=auth(env, "bob")).status_code in (404, 400)
    assert client.post("/api/projects/billing/review", headers=auth(env, "alice"),
                       json={"kind": "users", "id": 1, "status": "approved"}).status_code == 400
    assert client.post("/api/projects/billing/terms", headers=auth(env, "bob"),
                       content=b"x" * 70000).status_code == 400
    assert client.get("/api/users", headers=auth(env, "alice")).status_code == 403
