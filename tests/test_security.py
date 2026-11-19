import pytest

from lineage.security import REDACTED, is_secret_file, redact


@pytest.mark.parametrize("path", [".env", "config/.env.prod", "certs/server.pem", "id_rsa", "deploy/prod.tfvars",
                                  "k8s/service-account-ci.json", "app/secrets.yaml"])
def test_secret_files_skipped(path):
    assert is_secret_file(path)


@pytest.mark.parametrize("path", ["src/env.py", "src/secretary.py", "README.md", "keyboard.ts"])
def test_normal_files_kept(path):
    assert not is_secret_file(path)


@pytest.mark.parametrize("secret", [
    "AKIAABCDEFGHIJKLMNOP",
    "ghp_" + "a" * 36,
    "xoxb-1234567890-abcdef",
    "sk-" + "A" * 30,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    'password = "correcthorsebattery"',
    "https://user:pa55word@example.com/repo",
    "sk_live_" + "a1B2" * 6,
    "npm_" + "A1b2" * 9,
    "hf_" + "Ab" * 17,
    "glpat-" + "x1Y2" * 5,
    "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=" + "Ab9+" * 11 + "==",
    'headers = {"Authorization": "Bearer abcdef0123456789abcdef0123"}',
])
def test_tokens_redacted(secret):
    text, n = redact(f"x = 1\n{secret}\ny = 2")
    assert n >= 1 and REDACTED in text
    assert "correcthorsebattery" not in text and "pa55word" not in text


def test_redaction_keeps_line_numbers():
    key = "-----BEGIN RSA PRIVATE KEY-----\nAAAA\nBBBB\n-----END RSA PRIVATE KEY-----"
    text, n = redact(f"a\n{key}\ndef after(): pass\n")
    assert n == 1
    assert text.splitlines()[5] == "def after(): pass"


def test_ordinary_code_untouched():
    code = ('token = get_token()\npassword = form["password"]\nnpm_config_registry_override_for_ci_builds = 1\n'
            'headers = {"Authorization": f"Bearer {token}"}\n')
    assert redact(code) == (code, 0)
