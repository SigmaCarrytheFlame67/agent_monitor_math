"""Sign-up limited to eligible addresses (.edu) with emailed confirmation links."""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from http.server import ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from agent_monitor import auth
from agent_monitor import console_server as console

_ENV = (
    "AGENT_MONITOR_APP_URL",
    "AGENT_MONITOR_BASE_PATH",
    "AGENT_MONITOR_REGISTRATION_OPEN",
    "AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES",
    "AGENT_MONITOR_REQUIRE_EMAIL_VERIFICATION",
    "GOOGLE_OAUTH_CLIENT_ID",
    "SMTP_SERVER",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM_EMAIL",
)


@pytest.fixture()
def isolated_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(auth, "_INITIALIZED", False)
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    yield
    auth._INITIALIZED = False


def _smtp_configured(monkeypatch):
    monkeypatch.setenv("SMTP_SERVER", "smtp.example.test")
    monkeypatch.setenv("SMTP_USERNAME", "mailer")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")


def _edu_with_verification(monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES", ".edu")
    monkeypatch.setenv("AGENT_MONITOR_REQUIRE_EMAIL_VERIFICATION", "1")
    _smtp_configured(monkeypatch)


def _capture_mail(monkeypatch):
    sent = []
    monkeypatch.setattr(
        auth, "_send_email", lambda to, subject, body, *, purpose: sent.append((to, subject, body, purpose)) or True
    )
    return sent


def _link_token(body: str) -> str:
    return body.split("verify-email?token=", 1)[1].split()[0]


# ── settings ────────────────────────────────────────────────────────────────

def test_suffix_rules_are_normalized_and_match_whole_domain_labels(isolated_auth, monkeypatch):
    assert auth.registration_email_suffixes() == ()
    assert auth.email_allowed_for_registration("anyone@gmail.com")
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES", " edu , .AC.UK,@edu ")
    assert auth.registration_email_suffixes() == (".edu", ".ac.uk")
    for email in ("a@ucla.edu", "a@cs.ucla.edu", "A@MIT.EDU", "b@ox.ac.uk"):
        assert auth.email_allowed_for_registration(email), email
    for email in ("a@gmail.com", "a@edu.cn", "a@notedu.com", "a@ucla.edu.evil.com"):
        assert not auth.email_allowed_for_registration(email), email
    assert auth.registration_email_rule() == "Sign-up is limited to email addresses ending in .edu or .ac.uk."
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES", ".edu")
    assert auth.registration_email_rule() == "Sign-up is limited to .edu email addresses."


def test_defaults_change_nothing(isolated_auth):
    account = auth.register("someone@gmail.com", "password-1", require_open=False)
    assert auth.login("someone@gmail.com", "password-1")["id"] == account["id"]
    assert not auth.email_verification_required() and not auth.email_delivery_configured()


# ── password sign-up ────────────────────────────────────────────────────────

def test_http_sign_up_requires_an_eligible_address(isolated_auth, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES", ".edu")
    with pytest.raises(ValueError, match=r"limited to \.edu email addresses"):
        auth.register("someone@gmail.com", "password-1", require_open=True)
    assert auth.register("someone@ucla.edu", "password-1", require_open=True)["email"] == "someone@ucla.edu"
    # Operator-created accounts (create-admin) are not limited.
    assert auth.register("owner@example.com", "password-1", require_open=False)["email"] == "owner@example.com"


def test_verification_needs_outgoing_mail(isolated_auth, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    monkeypatch.setenv("AGENT_MONITOR_REQUIRE_EMAIL_VERIFICATION", "1")
    with pytest.raises(auth.EmailDeliveryUnavailable):
        auth.register("someone@ucla.edu", "password-1", require_open=True)
    with auth._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_unverified_account_cannot_sign_in_until_it_confirms(isolated_auth, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    auth.register("first@example.com", "password-1", require_open=True)  # first account: admin
    _edu_with_verification(monkeypatch)
    account = auth.register("student@ucla.edu", "password-1", require_open=True)
    assert auth.email_verification_pending(account["id"])
    with pytest.raises(auth.EmailVerificationRequired) as raised:
        auth.login_and_create_session("student@ucla.edu", "password-1")
    assert raised.value.email == "student@ucla.edu"
    assert "We sent a link to student@ucla.edu" in str(raised.value)
    # A wrong password reveals nothing about verification.
    with pytest.raises(ValueError, match="Incorrect email or password"):
        auth.login_and_create_session("student@ucla.edu", "wrong-password")

    sent = _capture_mail(monkeypatch)
    token = auth.send_email_verification("student@ucla.edu", "https://example.test/proof-deadbeef")
    assert token and len(sent) == 1
    to, subject, body, purpose = sent[0]
    assert to == "student@ucla.edu" and purpose == "verification"
    assert subject == "Confirm your email address for Ansätze"
    assert f"https://example.test/proof-deadbeef/verify-email?token={token}" in body
    with sqlite3.connect(auth.DB_PATH) as conn:
        stored = conn.execute("SELECT token_hash FROM email_verifications").fetchone()[0]
    assert stored != token  # only a hash is stored

    assert auth.verify_email(_link_token(body)) == {"ok": True, "email": "student@ucla.edu"}
    assert not auth.email_verification_pending(account["id"])
    user, session = auth.login_and_create_session("student@ucla.edu", "password-1")
    assert user["id"] == account["id"] and auth.user_for_token(session)["id"] == account["id"]
    with pytest.raises(ValueError, match="already been used"):
        auth.verify_email(token)
    # Verified accounts are not sent further links.
    assert auth.send_email_verification("student@ucla.edu", "https://example.test") is None


def test_expired_and_failed_links_are_removed(isolated_auth, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    _edu_with_verification(monkeypatch)
    auth.register("student@ucla.edu", "password-1", require_open=True)
    sent = _capture_mail(monkeypatch)
    token = auth.send_email_verification("student@ucla.edu", "https://example.test")
    with sqlite3.connect(auth.DB_PATH) as conn:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        conn.execute("UPDATE email_verifications SET expires_at=?", (past,))
    with pytest.raises(ValueError, match="has expired"):
        auth.verify_email(token)
    with sqlite3.connect(auth.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM email_verifications").fetchone()[0] == 0
    monkeypatch.setattr(auth, "_send_email", lambda *a, **k: False)
    assert auth.send_email_verification("student@ucla.edu", "https://example.test") is None
    with sqlite3.connect(auth.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM email_verifications").fetchone()[0] == 0
    assert len(sent) == 1


def test_sessions_of_unverified_accounts_stop_working_once_required(isolated_auth, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    auth.register("owner@example.com", "password-1", require_open=True)
    account = auth.register("late@ucla.edu", "password-1", require_open=True)
    _, session = auth.login_and_create_session("late@ucla.edu", "password-1")  # verification still off
    assert auth.user_for_token(session)["id"] == account["id"]
    _edu_with_verification(monkeypatch)
    assert auth.user_for_token(session) is None
    sent = _capture_mail(monkeypatch)
    auth.send_email_verification("late@ucla.edu", "https://example.test")
    auth.verify_email(_link_token(sent[0][2]))
    assert auth.user_for_token(session)["id"] == account["id"]


def test_ineligible_unverified_accounts_are_told_to_sign_up_again(isolated_auth, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    auth.register("owner@example.com", "password-1", require_open=True)
    auth.register("someone@gmail.com", "password-1", require_open=True)  # before the rules
    _edu_with_verification(monkeypatch)
    with pytest.raises(PermissionError) as raised:
        auth.login_and_create_session("someone@gmail.com", "password-1")
    assert not isinstance(raised.value, auth.EmailVerificationRequired)
    assert "sign up again with an eligible address" in str(raised.value)


def test_administrators_and_existing_accounts_keep_working(isolated_auth, monkeypatch):
    # An account database from before verification existed.
    auth.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(auth.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT,"
            " password_hash BLOB, salt BLOB, google_sub TEXT UNIQUE, picture TEXT, is_admin INTEGER DEFAULT 0,"
            " is_guest INTEGER NOT NULL DEFAULT 0, created_at TEXT)"
        )
        salt = b"0123456789abcdef"
        for email, admin in (("owner@example.com", 1), ("friend@gmail.com", 0)):
            conn.execute(
                "INSERT INTO users (email, password_hash, salt, is_admin, created_at) VALUES (?,?,?,?,?)",
                (email, auth._hash_password("password-1", salt), salt, admin, "2026-09-01T00:00:00+00:00"),
            )
    _edu_with_verification(monkeypatch)
    for email in ("owner@example.com", "friend@gmail.com"):
        assert auth.login_and_create_session(email, "password-1")[0]["email"] == email
    with sqlite3.connect(auth.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE email_verified_at IS NULL").fetchone()[0] == 0


def test_a_completed_password_reset_confirms_the_address(isolated_auth, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    auth.register("owner@example.com", "password-1", require_open=True)
    _edu_with_verification(monkeypatch)
    account = auth.register("student@ucla.edu", "password-1", require_open=True)
    monkeypatch.setattr(auth, "_send_reset_email", lambda _to, _link: True)
    token = auth.handle_forgot_password("student@ucla.edu", "https://example.test")
    assert auth.handle_reset_password(token, "password-2")
    assert not auth.email_verification_pending(account["id"])
    assert auth.login_and_create_session("student@ucla.edu", "password-2")[0]["id"] == account["id"]


def test_operator_can_set_a_password(isolated_auth):
    account = auth.register("someone@example.com", "password-1", require_open=False)
    session = auth.create_session(account["id"])
    assert auth.set_password("SOMEONE@example.com", "password-2")["id"] == account["id"]
    assert auth.user_for_token(session) is None
    assert auth.login("someone@example.com", "password-2")["id"] == account["id"]
    with pytest.raises(ValueError, match="No account uses"):
        auth.set_password("nobody@example.com", "password-2")
    with pytest.raises(ValueError, match="at least 8"):
        auth.set_password("someone@example.com", "short")


# ── Sign in with Google ─────────────────────────────────────────────────────

@pytest.fixture()
def google(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test-key", alg="RS256", use="sig")
    monkeypatch.setattr(auth, "_google_jwks", lambda: {"keys": [jwk]})
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-123.apps.googleusercontent.com")

    def credential(email: str, sub: str = "google-1", verified: bool = True) -> str:
        now = int(time.time())
        claims = {
            "iss": "https://accounts.google.com", "aud": "client-123.apps.googleusercontent.com",
            "sub": sub, "email": email, "email_verified": verified, "name": "Student", "iat": now, "exp": now + 300,
        }
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})

    return credential


def test_google_sign_up_follows_the_address_rule_and_counts_as_verified(isolated_auth, monkeypatch, google):
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    auth.register("owner@example.com", "password-1", require_open=True)
    _edu_with_verification(monkeypatch)
    with pytest.raises(PermissionError, match=r"limited to \.edu"):
        auth.login_with_google(google("someone@gmail.com"), require_open_for_new=True)
    account = auth.login_with_google(google("student@ucla.edu", sub="g-2"), require_open_for_new=True)
    assert account["email"] == "student@ucla.edu" and not auth.email_verification_pending(account["id"])
    session = auth.create_session(account["id"])
    assert auth.user_for_token(session)["id"] == account["id"]


def test_google_confirms_an_unverified_password_account_with_the_same_address(isolated_auth, monkeypatch, google):
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    auth.register("owner@example.com", "password-1", require_open=True)
    _edu_with_verification(monkeypatch)
    account = auth.register("student@ucla.edu", "password-1", require_open=True)
    assert auth.email_verification_pending(account["id"])
    linked = auth.login_with_google(google("student@ucla.edu", sub="g-3"), require_open_for_new=True)
    assert linked["id"] == account["id"] and not auth.email_verification_pending(account["id"])
    assert auth.login_and_create_session("student@ucla.edu", "password-1")[0]["id"] == account["id"]


# ── mail ────────────────────────────────────────────────────────────────────

def test_mail_encodes_the_subject_and_uses_starttls(isolated_auth, monkeypatch):
    calls = {}

    class FakeSMTP:
        def __init__(self, server, port, timeout):
            calls.update(server=server, port=port, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            calls["ehlo"] = calls.get("ehlo", 0) + 1

        def starttls(self, context):
            calls["starttls"] = True

        def login(self, user, password):
            calls["login"] = user

        def send_message(self, message):
            calls["raw"] = message.as_bytes()

    _smtp_configured(monkeypatch)
    monkeypatch.setenv("SMTP_FROM_EMAIL", "Ansätze <noreply@ansatze.ai>")
    monkeypatch.setattr(auth.smtplib, "SMTP", FakeSMTP)
    assert auth._send_email("student@ucla.edu", "Confirm your email address for Ansätze", "Hello ✓", purpose="verification")
    parsed = message_from_bytes(calls["raw"])
    assert str(make_header(decode_header(parsed["Subject"]))) == "Confirm your email address for Ansätze"
    assert parsed["To"] == "student@ucla.edu"
    assert calls["starttls"] and calls["login"] == "mailer" and calls["timeout"] == 15


# ── HTTP routes ─────────────────────────────────────────────────────────────

@contextmanager
def running_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(port, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = None if body is None else json.dumps(body)
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=payload, headers=request_headers)
    response = connection.getresponse()
    data = response.read().decode("utf-8")
    result = response.status, {k.lower(): v for k, v in response.getheaders()}, data
    connection.close()
    return result


@pytest.fixture()
def http_console(isolated_auth, monkeypatch):
    monkeypatch.setattr(console, "BASE_PATH", "/proof-deadbeef")
    monkeypatch.setattr(console, "PUBLIC_MODE", False)
    monkeypatch.setenv("AGENT_MONITOR_APP_URL", "https://ansatze.example")
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_OPEN", "1")
    jobs = []
    monkeypatch.setattr(console, "_enqueue_email_verification", lambda email, base: jobs.append((email, base)) or True)
    yield jobs


def test_routes_cover_sign_up_confirmation_and_sign_in(http_console, monkeypatch):
    jobs = http_console
    auth.register("owner@example.com", "password-1", require_open=False)
    _edu_with_verification(monkeypatch)
    with running_server() as port:
        status, _h, body = request(port, "GET", "/proof-deadbeef/api/auth/me")
        me = json.loads(body)
        assert status == 200 and me["registration_email_suffixes"] == [".edu"]
        assert me["email_verification_required"] is True and me["password_reset_available"] is True

        status, _h, body = request(port, "POST", "/proof-deadbeef/api/auth/register",
                                   body={"email": "someone@gmail.com", "password": "password-1"})
        assert status == 400 and json.loads(body)["error"] == "Sign-up is limited to .edu email addresses."

        status, headers, body = request(port, "POST", "/proof-deadbeef/api/auth/register",
                                        body={"email": "Student@UCLA.edu", "password": "password-1"})
        assert status == 200 and "set-cookie" not in headers
        assert json.loads(body) == {"ok": True, "verification_required": True, "email": "student@ucla.edu"}
        assert jobs == [("student@ucla.edu", "https://ansatze.example/proof-deadbeef")]

        status, headers, body = request(port, "POST", "/proof-deadbeef/api/auth/login",
                                        body={"email": "student@ucla.edu", "password": "password-1"})
        data = json.loads(body)
        assert status == 403 and data["verification_required"] is True and "set-cookie" not in headers

        status, _h, body = request(port, "POST", "/proof-deadbeef/api/auth/resend-verification",
                                   body={"email": "student@ucla.edu"})
        assert status == 200 and "new link is on its way" in json.loads(body)["message"]
        assert len(jobs) == 2

        sent = _capture_mail(monkeypatch)
        auth.send_email_verification("student@ucla.edu", "https://ansatze.example/proof-deadbeef")
        token = _link_token(sent[0][2])
        status, headers, body = request(port, "GET", "/proof-deadbeef/verify-email?token=" + token)
        assert status == 200 and "initVerifyMode" in body
        assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
        status, _h, body = request(port, "POST", "/proof-deadbeef/api/auth/verify-email", body={"token": token})
        assert status == 200 and json.loads(body)["email"] == "student@ucla.edu"
        status, _h, body = request(port, "POST", "/proof-deadbeef/api/auth/verify-email", body={"token": token})
        assert status == 400 and "already been used" in json.loads(body)["error"]

        status, headers, body = request(port, "POST", "/proof-deadbeef/api/auth/login",
                                        body={"email": "student@ucla.edu", "password": "password-1"})
        assert status == 200 and "pc_session=" in headers["set-cookie"]


def test_sign_up_reports_missing_mail_setup(http_console, monkeypatch):
    monkeypatch.setenv("AGENT_MONITOR_REQUIRE_EMAIL_VERIFICATION", "1")
    with running_server() as port:
        status, _h, body = request(port, "GET", "/proof-deadbeef/api/auth/me")
        assert json.loads(body)["password_reset_available"] is False
        status, _h, body = request(port, "POST", "/proof-deadbeef/api/auth/register",
                                   body={"email": "student@ucla.edu", "password": "password-1"})
        assert status == 503 and "not set up on this site yet" in json.loads(body)["error"]


def test_sign_in_page_allows_origin_referrer_for_google(http_console):
    with running_server() as port:
        status, headers, _body = request(port, "GET", "/proof-deadbeef/login")
        assert status == 200 and headers["referrer-policy"] == "strict-origin-when-cross-origin"
        # Other pages keep the strict default.
        status, headers, _body = request(port, "GET", "/proof-deadbeef/api/auth/me")
        assert headers["referrer-policy"] == "no-referrer"


# ── sign-in page ────────────────────────────────────────────────────────────

def test_sign_in_page_handles_confirmation_states():
    from pathlib import Path

    page = (Path(__file__).parents[1] / "agent_monitor" / "web" / "login.html").read_text(encoding="utf-8")
    script = page.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    # setMode ignores calls while a request is pending, so clear it first.
    check_email = script.split("function showCheckEmail(", 1)[1].split("\n}", 1)[0]
    assert check_email.index("setAuthPending(false)") < check_email.index("setMode('check-email')")
    # A confirmation link carries ?token= but is not a password reset.
    assert "const isReset=!isVerify&&(location.pathname.includes('/reset-password')||Boolean(token));" in script
    assert "if(r.ok&&d.verification_required)" in script and "if(!r.ok&&d.verification_required)" in script
    assert "if(!emailAllowed(body.email))throw new Error(registrationRule());" in script
    assert 'id="email-hint"' in page


def test_sign_up_without_verification_signs_in_at_once(http_console, monkeypatch):
    jobs = http_console
    auth.register("owner@example.com", "password-1", require_open=False)
    monkeypatch.setenv("AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES", ".edu")
    with running_server() as port:
        status, headers, body = request(port, "POST", "/proof-deadbeef/api/auth/register",
                                        body={"email": "student@ucla.edu", "password": "password-1"})
    assert status == 200 and json.loads(body)["user"]["email"] == "student@ucla.edu"
    assert "pc_session=" in headers["set-cookie"] and jobs == []
