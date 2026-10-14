import itertools
from pathlib import Path

import pytest

from lineage.config import Settings
from lineage.service import Lineage


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


INVOICE = '''
"""Invoice generation for postpaid customers."""

class InvoiceService:
    """Builds monthly invoices."""

    def generate_invoice(self, account_id):
        """Create the monthly invoice for an account."""
        lines = fetch_usage(account_id)
        return apply_tax(lines)


def fetch_usage(account_id):
    return []


def apply_tax(lines):
    return lines
'''


@pytest.fixture(scope="session")
def postgres_url(tmp_path_factory):
    """A real Postgres 16 + pgvector, embedded for tests (stands in for a client's database)."""
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(str(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
    yield server.get_uri()


_schemas = itertools.count()


@pytest.fixture(params=["sqlite", "postgres"])
def settings(request, tmp_path):
    repos = tmp_path / "repos"
    repos.mkdir()
    if request.param == "sqlite":
        return Settings(db_url=f"sqlite:///{tmp_path / 'index.db'}", embedder="hash",
                        allowed_roots=(str(repos),), user="root", index_workers=1)
    url = request.getfixturevalue("postgres_url")
    return Settings(db_url=url, db_schema=f"t{next(_schemas)}", embedder="hash",
                    allowed_roots=(str(repos),), user="root", index_workers=1)


@pytest.fixture
def env(settings):
    """Two indexed projects (billing, crm). alice maintains billing; bob and carol are developers."""
    repos = Path(settings.allowed_roots[0])
    billing, crm = repos / "billing", repos / "crm"
    write(billing, "billing/invoice.py", INVOICE)
    write(billing, "billing/config.py", 'DB_URL = "postgres://admin:hunter2pass@db/billing"\naws = "AKIAABCDEFGHIJKLMNOP"\n')
    write(billing, ".env", "API_KEY=supersecretvalue123\n")
    write(billing, "web/cart.js", "// Shopping cart totals\nfunction cartTotal(items) { return sum(items); }\nfunction sum(xs) { return xs.reduce((a, b) => a + b, 0); }\n")
    write(crm, "crm/customer.py", '"""Customer profiles."""\n\ndef load_customer(customer_id):\n    """Load a customer profile."""\n    return {}\n')

    app = Lineage(settings)
    root = app.local_principal()
    app.index_project(root, str(billing), "billing")
    app.index_project(root, str(crm), "crm")
    tokens = {n: app.create_user(root, n)["token"] for n in ("alice", "bob", "carol")}
    app.set_maintainer(root, "alice", "billing", True)

    class Env:
        pass
    e = Env()
    e.app, e.settings, e.root, e.billing, e.crm, e.tokens = app, settings, root, billing, crm, tokens
    e.who = lambda name: app.authenticate(tokens[name])
    return e
