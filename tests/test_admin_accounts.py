"""Accounts an administrator manages (``agent_monitor.accounts_admin`` and
``/api/admin/accounts``): creating them in bulk from a pasted list with
generated passwords, emailing each person their sign-in details, and turning
each account's access on or off (off signs the person out and refuses their
sign-in).  Nothing touches the network: mail goes to a stand-in SMTP class.
"""
from __future__ import annotations

import json
import sqlite3
import types
from contextlib import closing

import pytest

from agent_monitor import accounts_admin, auth
from agent_monitor import console_server as console  # noqa: F401  (the fixtures start it)

from test_analyze_console import _admin_cookie, _get, _post  # noqa: E402
from test_console_guest_access import (  # noqa: E402,F401  (fixture import)
    APP_MOUNT,
    _issue_guest,
    guest_console,
    request,
    running_server,
)
from test_member_accounts import _member, member_console  # noqa: E402,F401

PARTICIPANTS = """Ben Antieau - d.ben.antieau@gmail.com
Frédéric Déglise - frederic.deglise@ens-lyon.fr
Elden Elmanto - eldenelmanto@gmail.com

Hiraku Nakajima <hiraku.nakajima@ipmu.jp>
yakerson@imj-prg.fr
Kirsten Wickelgren, kirsten.wickelgren@duke.edu
"""


class Mailbox:
    """A stand-in for smtplib.SMTP that keeps what would be sent."""

    sent: list = []

    def __init__(self, server, port, timeout=None):
        self.server = server

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        return None

    def starttls(self, context=None):
        return None

    def login(self, user, password):
        return None

    def send_message(self, message):
        Mailbox.sent.append(message)


@pytest.fixture()
def mail(monkeypatch):
    for name, value in (("SMTP_SERVER", "smtp.example.test"), ("SMTP_USERNAME", "site"),
                        ("SMTP_PASSWORD", "x" * 12), ("SMTP_FROM_EMAIL", "noreply@example.test"),
                        ("AGENT_MONITOR_APP_URL", "https://ansatze.example")):
        monkeypatch.setenv(name, value)
    Mailbox.sent = []
    monkeypatch.setattr(auth.smtplib, "SMTP", Mailbox)
    yield Mailbox


def _login(port, email, password):
    status, headers, raw = request(port, "POST", f"{APP_MOUNT}/api/auth/login",
                                   body={"email": email, "password": password})
    return status, json.loads(raw or "{}"), headers


def test_a_pasted_list_is_read_one_person_per_line():
    people = accounts_admin.parse_people(PARTICIPANTS + "no address on this line\n")
    assert [(person["name"], person["email"]) for person in people] == [
        ("Ben Antieau", "d.ben.antieau@gmail.com"),
        ("Frédéric Déglise", "frederic.deglise@ens-lyon.fr"),
        ("Elden Elmanto", "eldenelmanto@gmail.com"),
        ("Hiraku Nakajima", "hiraku.nakajima@ipmu.jp"),
        ("", "yakerson@imj-prg.fr"),
        ("Kirsten Wickelgren", "kirsten.wickelgren@duke.edu"),
        ("", ""),
    ]
    assert accounts_admin.parse_people("Jean-Pierre Serre - jps@ihes.fr") == [
        {"name": "Jean-Pierre Serre", "email": "jps@ihes.fr", "line": "Jean-Pierre Serre - jps@ihes.fr"}]


@pytest.mark.parametrize(("line", "name", "email"), [
    # An address is never split: its hyphens and apostrophes belong to it.
    ("mary-jane@uni.example", "", "mary-jane@uni.example"),
    ("jean-pierre.serre@ihes.example", "", "jean-pierre.serre@ihes.example"),
    ("Mary Jane - mary-jane@uni.example", "Mary Jane", "mary-jane@uni.example"),
    ("Marie-Claire Dupont\tmarie-claire.dupont@ens.example", "Marie-Claire Dupont", "marie-claire.dupont@ens.example"),
    ("Ada Lovelace\tada@example.org", "Ada Lovelace", "ada@example.org"),
    ("siobhan.o'brien@example.org", "", "siobhan.o'brien@example.org"),
    # Other ways a list comes: quoted "Last, First", no space, address first, numbered.
    ('"Antieau, Ben",ben@example.org', "Antieau, Ben", "ben@example.org"),
    ("Lovelace, Ada - ada@example.org", "Lovelace, Ada", "ada@example.org"),
    ("Ben Antieau,ben@example.org", "Ben Antieau", "ben@example.org"),
    ("ben@example.org - Ben Antieau", "Ben Antieau", "ben@example.org"),
    ("Ben Antieau <ben@example.org> (UCI)", "Ben Antieau", "ben@example.org"),
    ("1. Ben Antieau - Ben@Example.org.", "Ben Antieau", "ben@example.org"),
    ("• Tom Bachmann - tom.bachmann@zoho.com", "Tom Bachmann", "tom.bachmann@zoho.com"),
    ("mailto:x@example.org", "", "x@example.org"),
    # Characters nobody sees never reach an address.
    ("\ufeffhilbert@uni.example", "", "hilbert@uni.example"),
    ("Ann \u200bann@example.org", "Ann", "ann@example.org"),
    ("Bo B\u200eo@example.org", "Bo", "bo@example.org"),
])
def test_each_way_of_writing_a_line_gives_its_whole_address(line, name, email):
    [person] = accounts_admin.parse_people(line)
    assert (person["name"], person["email"]) == (name, email) and "error" not in person


def test_a_line_with_two_addresses_or_none_is_invalid(member_console):
    people = accounts_admin.parse_people("a@example.org, b@example.org\nName,Email\nJosé <josé@example.org>")
    assert [person.get("error") for person in people] == [
        "More than one email address on this line", "No email address on this line", None]
    results = accounts_admin.create_accounts(people)
    assert [result["status"] for result in results] == ["invalid", "invalid", "invalid"]
    assert results[0]["error"].startswith("More than one email address on this line: a@example.org")
    assert "plain (ASCII)" in results[2]["error"]
    assert [account["email"] for account in accounts_admin.list_accounts()] == ["owner@example.test"]


def test_passwords_are_random_unambiguous_and_mixed():
    passwords = {accounts_admin.generate_password(12) for _ in range(200)}
    assert len(passwords) == 200
    for password in passwords:
        assert len(password) == 12 and set(password) <= set(accounts_admin.PASSWORD_ALPHABET)
        assert any(c.isdigit() for c in password) and any(c.islower() for c in password)
        assert any(c.isupper() for c in password)
    assert not set("0O1lI") & set(accounts_admin.PASSWORD_ALPHABET)
    assert len(accounts_admin.generate_password(4)) == 8 and len(accounts_admin.generate_password(99)) == 32


def test_created_accounts_sign_in_at_once_and_existing_ones_are_left_alone(member_console, monkeypatch):
    auth.register("yakerson@imj-prg.fr", "her-own-password")
    people = accounts_admin.parse_people(PARTICIPANTS + "ben antieau - D.Ben.Antieau@gmail.com\nnobody\n")
    results = accounts_admin.create_accounts(people, password_length=10)
    statuses = [(result["email"], result["status"]) for result in results]
    assert statuses == [
        ("d.ben.antieau@gmail.com", "created"), ("frederic.deglise@ens-lyon.fr", "created"),
        ("eldenelmanto@gmail.com", "created"), ("hiraku.nakajima@ipmu.jp", "created"),
        ("yakerson@imj-prg.fr", "exists"), ("kirsten.wickelgren@duke.edu", "created"),
        ("d.ben.antieau@gmail.com", "duplicate"), ("", "invalid"),
    ]
    created = [result for result in results if result["status"] == "created"]
    assert all(len(result["password"]) == 10 for result in created)
    assert all("password" not in result for result in results if result["status"] != "created")
    # An existing account keeps its password; a new one signs in with its own,
    # even where sign-ups must verify their address, and is no administrator.
    auth.login("yakerson@imj-prg.fr", "her-own-password")
    monkeypatch.setenv("AGENT_MONITOR_REQUIRE_EMAIL_VERIFICATION", "1")
    for result in created:
        account = auth.login(result["email"], result["password"])
        assert account["is_admin"] is False and account["name"] == (result["name"] or result["email"].split("@")[0])
    listed = {account["email"]: account for account in accounts_admin.list_accounts()}
    assert listed["frederic.deglise@ens-lyon.fr"]["name"] == "Frédéric Déglise"
    assert listed["owner@example.test"]["is_admin"] is True
    with pytest.raises(ValueError):
        accounts_admin.create_accounts(people, password_length=7)


def test_access_off_signs_the_person_out_and_refuses_sign_in_until_it_is_on(member_console):
    [result] = accounts_admin.create_accounts(accounts_admin.parse_people("Ben Antieau - ben@example.test"))
    _account, token = auth.login_and_create_session("ben@example.test", result["password"])
    assert auth.user_for_token(token)["email"] == "ben@example.test"
    changed = accounts_admin.set_access(["ben@example.test", "owner@example.test", "nobody@example.test"], on=False)
    assert changed["updated"] == ["ben@example.test"]
    assert {item["email"] for item in changed["skipped"]} == {"owner@example.test", "nobody@example.test"}
    # Signed out at once, and refused with a plain message.
    assert auth.user_for_token(token) is None
    with pytest.raises(auth.AccessPaused) as refused:
        auth.login_and_create_session("ben@example.test", result["password"])
    assert "paused" in str(refused.value)
    with pytest.raises(auth.AccessPaused):
        auth.login("ben@example.test", result["password"])
    # A wrong password still reads as a wrong password, not as paused.
    with pytest.raises(ValueError):
        auth.login("ben@example.test", "not-the-password")
    listed = {account["email"]: account for account in accounts_admin.list_accounts()}
    assert listed["ben@example.test"]["access"] == "off" and listed["ben@example.test"]["access_paused_at"]
    assert listed["owner@example.test"]["access"] == "on"
    # Turning it off again keeps the first time; on again lets them sign in.
    paused_at = listed["ben@example.test"]["access_paused_at"]
    accounts_admin.set_access(["ben@example.test"], on=False)
    assert {a["email"]: a for a in accounts_admin.list_accounts()}["ben@example.test"]["access_paused_at"] == paused_at
    assert accounts_admin.set_access(["BEN@example.test "], on=True)["updated"] == ["ben@example.test"]
    _account, token = auth.login_and_create_session("ben@example.test", result["password"])
    assert auth.user_for_token(token)["email"] == "ben@example.test"


def test_an_administrators_access_is_never_paused(member_console):
    owner = next(account for account in accounts_admin.list_accounts() if account["is_admin"])
    with closing(auth._conn()) as conn, conn:
        # Even a paused mark written by hand does not lock an administrator out.
        conn.execute("UPDATE users SET access_paused_at=? WHERE id=?", (auth._now(), owner["id"]))
    _account, token = auth.login_and_create_session("owner@example.test", "long-enough-password")
    assert auth.user_for_token(token)["is_admin"] is True


def test_a_new_password_replaces_the_old_one_and_signs_out(member_console):
    [result] = accounts_admin.create_accounts(accounts_admin.parse_people("m@example.test"))
    _account, token = auth.login_and_create_session("m@example.test", result["password"])
    # The last sign-in is kept on the account after its sessions end.
    signed_in = {a["email"]: a for a in accounts_admin.list_accounts()}["m@example.test"]["last_sign_in_at"]
    assert signed_in
    password = accounts_admin.new_password("m@example.test", password_length=16)
    assert len(password) == 16 and auth.user_for_token(token) is None
    with pytest.raises(ValueError):
        auth.login("m@example.test", result["password"])
    auth.login("m@example.test", password)
    assert {a["email"]: a for a in accounts_admin.list_accounts()}["m@example.test"]["last_sign_in_at"] == signed_in
    with pytest.raises(ValueError):
        accounts_admin.new_password("nobody@example.test")


def test_the_invitation_is_filled_in_and_sent_from_the_site_under_the_senders_name(mail):
    body = accounts_admin.invite_email(template=accounts_admin.DEFAULT_MESSAGE, name="Ben Antieau",
                                       email="ben@example.test", password="Pa55word42xy",
                                       url="https://ansatze.example/login", sender="Eric Jiang")
    assert body.startswith("Dear Ben Antieau,") and "Password: Pa55word42xy" in body
    assert "Sign in at: https://ansatze.example/login" in body and body.rstrip().endswith("Eric Jiang")
    # Other braces stay as written.
    assert accounts_admin.invite_email(template="{name} {x} {password}", name="", email="a@b.cd", password="p",
                                       url="", sender="") == "a {x} p"
    assert accounts_admin.send_invite(email="ben@example.test", name="Ben", password="Pa55word42xy",
                                      url="https://ansatze.example/login", sender_name="Eric Jiang",
                                      reply_to="eric@example.test", subject="Workshop access",
                                      message="Hi {name}, your password is {password}.")
    [message] = mail.sent
    assert message["To"] == "ben@example.test" and message["Subject"] == "Workshop access"
    assert message["From"] == "Eric Jiang via Ansätze <noreply@example.test>"
    assert message["Reply-To"] == "eric@example.test"
    assert message.get_content().strip() == "Hi Ben, your password is Pa55word42xy."


def test_the_admin_api_creates_emails_and_turns_access_on_and_off(member_console, mail):
    with running_server() as port:
        admin = _admin_cookie()
        listed = _get(port, "/api/admin/accounts", admin)
        created = _post(port, "/api/admin/accounts", admin,
                        {"text": "Ben Antieau - ben@example.test\nmaria@example.test", "password_length": 12,
                         "send_email": True, "subject": "Ansätze for the workshop", "sender_name": "Eric Jiang"})
        ben_password = next(item["password"] for item in created[2]["results"] if item["email"] == "ben@example.test")
        signed_in = _login(port, "ben@example.test", ben_password)
        off = _post(port, "/api/admin/accounts/access", admin, {"emails": ["ben@example.test"], "access": "off"})
        refused = _login(port, "ben@example.test", ben_password)
        on = _post(port, "/api/admin/accounts/access", admin, {"emails": ["ben@example.test"], "access": "on"})
        again = _login(port, "ben@example.test", ben_password)
        reset = _post(port, "/api/admin/accounts/password", admin,
                      {"email": "maria@example.test", "send_email": True, "message": "Account created! {password}"})
        bad = _post(port, "/api/admin/accounts/access", admin, {"emails": "ben@example.test", "access": "off"})
        listed_after = _get(port, "/api/admin/accounts", admin)
    assert listed[0] == 200 and listed[1]["email_available"] is True
    assert listed[1]["defaults"]["password_length"] == 12 and "{password}" in listed[1]["defaults"]["message"]
    assert listed[1]["sender"]["email"] == "owner@example.test"
    assert created[0] == 200 and [item["status"] for item in created[2]["results"]] == ["created", "created"]
    assert all(item["email_sent"] is True for item in created[2]["results"])
    assert [message["To"] for message in mail.sent] == ["ben@example.test", "maria@example.test", "maria@example.test"]
    assert all(message["Reply-To"] == "owner@example.test" for message in mail.sent)
    assert ben_password in mail.sent[0].get_content() and "Dear Ben Antieau," in mail.sent[0].get_content()
    assert created[1].get("cache-control") == "no-store"
    assert signed_in[0] == 200
    assert off[0] == 200 and off[2]["updated"] == ["ben@example.test"]
    assert refused[0] == 403 and "paused" in refused[1]["error"]
    assert on[0] == 200 and again[0] == 200
    assert reset[0] == 200 and len(reset[2]["password"]) == 12 and reset[2]["email_sent"] is True
    # A new password goes out with its own message, not the add form's.
    assert mail.sent[-1]["Subject"] == accounts_admin.RESET_SUBJECT
    assert "Here is a new password" in mail.sent[-1].get_content()
    assert reset[2]["password"] in mail.sent[-1].get_content()
    assert bad[0] == 400
    access = {item["email"]: item["access"] for item in listed_after[1]["accounts"]}
    assert access == {"owner@example.test": "on", "ben@example.test": "on", "maria@example.test": "on"}


def test_only_an_administrator_may_manage_accounts(member_console):
    with running_server() as port:
        _member_user, member_cookie = _member(port)
        _guest, guest_cookie, _ = _issue_guest(port)
        for cookie in (member_cookie, guest_cookie):
            assert _get(port, "/api/admin/accounts", cookie)[0] == 403
            assert _post(port, "/api/admin/accounts/group", cookie,
                         {"emails": ["member@example.test"], "group": "x"})[0] == 403
            assert _post(port, "/api/admin/accounts", cookie, {"text": "x@example.test"})[0] == 403
            assert _post(port, "/api/admin/accounts/access", cookie,
                         {"emails": ["member@example.test"], "access": "off"})[0] == 403
            assert _post(port, "/api/admin/accounts/password", cookie, {"email": "member@example.test"})[0] == 403


def test_emails_are_refused_when_the_site_cannot_send_them(member_console, monkeypatch):
    for name in ("SMTP_SERVER", "SMTP_USERNAME", "SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    with running_server() as port:
        admin = _admin_cookie()
        listed = _get(port, "/api/admin/accounts", admin)
        refused = _post(port, "/api/admin/accounts", admin, {"text": "x@example.test", "send_email": True})
        created = _post(port, "/api/admin/accounts", admin, {"text": "x@example.test"})
    assert listed[1]["email_available"] is False
    assert refused[0] == 400 and "cannot send email" in refused[2]["error"]
    # Nothing was created by the refused request; the plain one creates it.
    assert created[0] == 200 and created[2]["results"][0]["status"] == "created"


def test_no_account_made_here_becomes_the_first_administrator(guest_console):
    # A site without an account yet: its first account would be the administrator.
    with pytest.raises(ValueError, match="administrator's own account first"):
        accounts_admin.create_accounts(accounts_admin.parse_people("x@example.test"))
    assert accounts_admin.list_accounts() == []


def test_a_preview_says_what_would_happen_and_creates_nothing(member_console):
    auth.register("yakerson@imj-prg.fr", "her-own-password")
    people = accounts_admin.parse_people(PARTICIPANTS + "Ben - d.ben.antieau@gmail.com\nnobody\n")
    results = accounts_admin.create_accounts(people, preview=True)
    assert [(result["email"], result["status"]) for result in results] == [
        ("d.ben.antieau@gmail.com", "new"), ("frederic.deglise@ens-lyon.fr", "new"), ("eldenelmanto@gmail.com", "new"),
        ("hiraku.nakajima@ipmu.jp", "new"), ("yakerson@imj-prg.fr", "exists"),
        ("kirsten.wickelgren@duke.edu", "new"), ("d.ben.antieau@gmail.com", "duplicate"), ("", "invalid")]
    assert results[3]["line"] == "Hiraku Nakajima <hiraku.nakajima@ipmu.jp>"
    assert all("password" not in result for result in results)
    assert sorted(account["email"] for account in accounts_admin.list_accounts()) == [
        "owner@example.test", "yakerson@imj-prg.fr"]


def test_a_server_error_stops_the_run_and_keeps_the_passwords_already_made(member_console, monkeypatch):
    register = auth.register
    calls = []

    def flaky(email, password, name=None, **kwargs):
        calls.append(email)
        if len(calls) == 2:
            raise sqlite3.OperationalError("database is locked")
        return register(email, password, name, **kwargs)

    monkeypatch.setattr(auth, "register", flaky)
    results = accounts_admin.create_accounts(accounts_admin.parse_people(PARTICIPANTS))
    assert [result["status"] for result in results] == ["created"] + ["not_processed"] * 5
    assert "database is locked" in results[1]["error"] and "not processed" in results[1]["error"]
    assert results[2]["error"] == "Not processed: an earlier line stopped the run"
    assert len(calls) == 2
    auth.login("d.ben.antieau@gmail.com", results[0]["password"])


def test_access_from_a_list_turns_everyone_else_in_the_group_off_in_one_go(member_console):
    auth.register("member@example.test", "member-password-1")  # a member of the site, in no group
    created = accounts_admin.create_accounts(accounts_admin.parse_people("a@example.test\nb@example.test\nc@example.test"),
                                             group=" Field   test ")
    password = {result["email"]: result["password"] for result in created}
    assert all(result["group"] == "Field test" for result in created)
    _account, token = auth.login_and_create_session("b@example.test", password["b@example.test"])
    accounts_admin.set_access(["c@example.test"], on=False)
    # An account of the group the caller never saw is turned off too; administrators never.
    auth.register("late@example.test", "late-password-1")
    accounts_admin.set_group(["late@example.test"], "Field test")
    changed = accounts_admin.set_access(["a@example.test", "c@example.test", "owner@example.test"], on=True,
                                        others_off=True, group="Field test")
    assert changed["updated"] == ["a@example.test", "c@example.test"]
    assert changed["turned_off"] == ["b@example.test", "late@example.test"]
    assert [item["email"] for item in changed["skipped"]] == ["owner@example.test"]
    access = {account["email"]: account["access"] for account in accounts_admin.list_accounts()}
    assert access == {"owner@example.test": "on", "member@example.test": "on", "a@example.test": "on",
                      "b@example.test": "off", "c@example.test": "on", "late@example.test": "off"}
    assert auth.user_for_token(token) is None
    # Listing nobody turns the whole group off, and nobody outside it.
    assert accounts_admin.set_access([], on=True, others_off=True, group="Field test")["turned_off"] == [
        "a@example.test", "c@example.test"]
    assert {a["email"]: a["access"] for a in accounts_admin.list_accounts()}["member@example.test"] == "on"
    # "Others off" only goes with "on", and only within a group.
    with pytest.raises(ValueError):
        accounts_admin.set_access(["a@example.test"], on=False, others_off=True, group="Field test")
    with pytest.raises(ValueError, match="which group"):
        accounts_admin.set_access(["a@example.test"], on=True, others_off=True)


def test_existing_accounts_join_the_group_but_are_otherwise_left_alone(member_console):
    auth.register("had-one@example.test", "had-one-password")
    people = accounts_admin.parse_people("had-one@example.test\nowner@example.test\nnew@example.test")
    preview = accounts_admin.create_accounts(people, preview=True, group="Field test")
    assert [(r["status"], r.get("group_joined"), r.get("group")) for r in preview] == [
        ("exists", True, "Field test"), ("exists", None, None), ("new", None, "Field test")]
    assert {a["email"]: a["group"] for a in accounts_admin.list_accounts()}["had-one@example.test"] == ""
    results = accounts_admin.create_accounts(people, group="Field test")
    assert [(r["status"], r.get("group_joined")) for r in results] == [("exists", True), ("exists", None), ("created", None)]
    groups = {a["email"]: a["group"] for a in accounts_admin.list_accounts()}
    assert groups == {"owner@example.test": "", "had-one@example.test": "Field test", "new@example.test": "Field test"}
    # Run again: already in the group, so nobody "joins" it.
    again = accounts_admin.create_accounts(people, preview=True, group="Field test")
    assert [(r["status"], r.get("group_joined"), r.get("group")) for r in again] == [
        ("exists", None, "Field test"), ("exists", None, None), ("exists", None, "Field test")]
    again = accounts_admin.create_accounts(people, group="Field test")
    assert [(r["status"], r.get("group_joined"), r.get("group")) for r in again] == [
        ("exists", None, "Field test"), ("exists", None, None), ("exists", None, "Field test")]
    auth.login("had-one@example.test", "had-one-password")
    changed = accounts_admin.set_group(["had-one@example.test", "nobody@example.test"], "")
    assert changed["updated"] == ["had-one@example.test"] and changed["skipped"][0]["email"] == "nobody@example.test"
    assert {a["email"]: a["group"] for a in accounts_admin.list_accounts()}["had-one@example.test"] == ""
    with pytest.raises(ValueError):
        accounts_admin.set_group(["new@example.test"], 7)
    assert accounts_admin.clean_group("  a\tb\x1b  ") == "a b" and len(accounts_admin.clean_group("x" * 99)) == 60


def test_no_administrator_is_given_a_new_password_here(member_console):
    auth.register("second-admin@example.test", "second-admin-password")
    with closing(auth._conn()) as conn, conn:
        conn.execute("UPDATE users SET is_admin=1 WHERE email='second-admin@example.test'")
    for email in ("owner@example.test", "second-admin@example.test"):
        with pytest.raises(ValueError, match="administrator's password is not changed here"):
            accounts_admin.new_password(email)
    auth.login("second-admin@example.test", "second-admin-password")


def test_a_sender_written_with_its_name_still_sends(mail, monkeypatch):
    monkeypatch.setenv("SMTP_FROM_EMAIL", "Ansätze <noreply@ansatze.example>")
    assert accounts_admin.send_invite(email="ben@example.test", name="Ben", password="Pa55word42xy",
                                      url="https://ansatze.example/login", sender_name="Eric Jiang",
                                      reply_to="eric@example.test")
    [message] = mail.sent
    assert message["From"] == "Eric Jiang via Ansätze <noreply@ansatze.example>"


def test_an_invitation_that_cannot_be_sent_never_raises(mail, monkeypatch):
    def broken(*_args, **_kwargs):
        raise UnicodeEncodeError("ascii", "ä", 0, 1, "no")

    monkeypatch.setattr(auth, "_send_email", broken)
    assert accounts_admin.send_invite(email="ben@example.test", name="Ben", password="Pa55word42xy", url="u",
                                      sender_name="Eric", reply_to=None) is False


def test_the_sign_in_address_is_only_the_sites_own(monkeypatch):
    monkeypatch.delenv("AGENT_MONITOR_APP_URL", raising=False)
    monkeypatch.setenv("AGENT_MONITOR_BASE_PATH", "/mount-x")
    assert accounts_admin.sign_in_url() == ""
    monkeypatch.setenv("AGENT_MONITOR_APP_URL", "https://ansatze.example/")
    assert accounts_admin.sign_in_url() == "https://ansatze.example/mount-x/login"
    assert accounts_admin.sign_in_url("/proof-deadbeef") == "https://ansatze.example/proof-deadbeef/login"
    monkeypatch.setenv("AGENT_MONITOR_APP_URL", "https://ansatze.example/console")
    assert accounts_admin.sign_in_url() == "https://ansatze.example/console/login"
    monkeypatch.setenv("AGENT_MONITOR_APP_URL", "javascript:alert(1)")
    assert accounts_admin.sign_in_url() == ""


def test_the_admin_api_previews_stops_and_sets_access_from_a_list(member_console, mail):
    with running_server() as port:
        admin = _admin_cookie()
        preview = _post(port, "/api/admin/accounts", admin,
                        {"text": "mary-jane@uni.example\nBen - ben@example.test", "preview": True, "send_email": True})
        nothing_yet = _get(port, "/api/admin/accounts", admin)
        created = _post(port, "/api/admin/accounts", admin, {"text": "a@example.test\nb@example.test", "group": "FT"})
        outsider = _post(port, "/api/admin/accounts", admin, {"text": "c@example.test"})
        from_list = _post(port, "/api/admin/accounts/access", admin,
                          {"emails": ["a@example.test"], "access": "on", "others": "off", "group": "FT"})
        no_group = _post(port, "/api/admin/accounts/access", admin,
                         {"emails": ["a@example.test"], "access": "on", "others": "off"})
        wrong = _post(port, "/api/admin/accounts/access", admin,
                      {"emails": ["a@example.test"], "access": "off", "others": "off", "group": "FT"})
        odd = _post(port, "/api/admin/accounts/access", admin, {"emails": [], "access": "on", "others": "on"})
        grouped = _post(port, "/api/admin/accounts/group", admin, {"emails": ["c@example.test"], "group": "Other"})
        bad_group = _post(port, "/api/admin/accounts/group", admin, {"emails": "c@example.test", "group": "x"})
        admin_reset = _post(port, "/api/admin/accounts/password", admin, {"email": "owner@example.test"})
        listed = _get(port, "/api/admin/accounts", admin)
    assert preview[0] == 200 and [(r["email"], r["status"]) for r in preview[2]["results"]] == [
        ("mary-jane@uni.example", "new"), ("ben@example.test", "new")]
    assert mail.sent == [] and [a["email"] for a in nothing_yet[1]["accounts"]] == ["owner@example.test"]
    assert created[0] == 200 and "stopped" not in created[2] and outsider[0] == 200
    assert from_list[0] == 200 and from_list[2]["turned_off"] == ["b@example.test"]
    assert no_group[0] == 400 and wrong[0] == 400 and odd[0] == 400
    assert grouped[0] == 200 and grouped[2]["updated"] == ["c@example.test"] and bad_group[0] == 400
    assert listed[1]["groups"] == ["FT", "Other"]
    assert admin_reset[0] == 400 and "administrator" in admin_reset[2]["error"]
    assert {a["email"]: (a["access"], a["group"]) for a in listed[1]["accounts"]} == {
        "owner@example.test": ("on", ""), "a@example.test": ("on", "FT"), "b@example.test": ("off", "FT"),
        "c@example.test": ("on", "Other")}
    assert listed[1]["sign_in_url"] == "https://ansatze.example/proof-deadbeef/login"
    assert listed[1]["open_doors"] == {"sign_ups": True, "guests": True}
    assert listed[1]["defaults"]["reset_subject"] == accounts_admin.RESET_SUBJECT


def test_the_admin_api_says_where_a_stopped_run_stopped(member_console, monkeypatch):
    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(auth, "register", locked)
    with running_server() as port:
        status, _headers, payload = _post(port, "/api/admin/accounts", _admin_cookie(), {"text": "a@example.test"})
    assert status == 200 and payload["results"][0]["status"] == "not_processed"
    assert "database is locked" in payload["stopped"]


def test_emails_need_the_sites_address(member_console, mail, monkeypatch):
    monkeypatch.delenv("AGENT_MONITOR_APP_URL")
    with running_server() as port:
        admin = _admin_cookie()
        # Even from the server itself, a request's Host is never the address.
        listed = _get(port, "/api/admin/accounts", admin)
        refused = _post(port, "/api/admin/accounts", admin, {"text": "x@example.test", "send_email": True})
        after = _get(port, "/api/admin/accounts", admin)
    assert listed[1]["sign_in_url"] == ""
    assert refused[0] == 400 and "AGENT_MONITOR_APP_URL" in refused[2]["error"]
    assert [a["email"] for a in after[1]["accounts"]] == ["owner@example.test"] and mail.sent == []


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli(member_console, monkeypatch):
    monkeypatch.setattr(accounts_admin, "_load_site_environment", lambda: None)
    return accounts_admin.main


def test_the_cli_refuses_a_missing_database(cli, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "elsewhere" / "users.db")
    assert cli(["off", "a@example.test"]) == 2
    assert "No account database" in capsys.readouterr().err
    assert not (tmp_path / "elsewhere").exists()


def test_the_cli_lists_names_with_their_control_characters_escaped(cli, capsys):
    auth.register("odd@example.test", "odd-password-1", "on\r\x1b[2Koff")
    assert cli(["list"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\r" not in out and "on\\r\\x1b[2Koff" in out


def test_the_cli_creates_from_a_file_with_a_byte_order_mark(cli, tmp_path, capsys):
    path = tmp_path / "people.txt"
    path.write_text("hilbert@uni.example\nAda Lovelace - ada@example.org\n", encoding="utf-8-sig")
    assert cli(["create", str(path)]) == 0
    results = json.loads(capsys.readouterr().out)
    assert [result["email"] for result in results] == ["hilbert@uni.example", "ada@example.org"]
    auth.login("hilbert@uni.example", results[0]["password"])


def test_the_cli_mails_only_when_it_can_and_prints_passwords_first(cli, mail, tmp_path, capsys, monkeypatch):
    path = tmp_path / "people.txt"
    path.write_text("Ada - ada@example.org\nBen - ben@example.org\n", encoding="utf-8")
    monkeypatch.delenv("SMTP_SERVER")
    assert cli(["create", str(path), "--send-email"]) == 2
    assert "nothing was created" in capsys.readouterr().err
    assert [account["email"] for account in accounts_admin.list_accounts()] == ["owner@example.test"]
    monkeypatch.setenv("SMTP_SERVER", "smtp.example.test")
    sends = []

    def flaky_send(**kwargs):
        sends.append(kwargs["email"])
        return kwargs["email"] == "ada@example.org"

    monkeypatch.setattr(accounts_admin, "send_invite", flaky_send)
    # A bare site address is not trusted to be the sign-in page, and emails need a sender and a reply-to.
    assert cli(["create", str(path), "--send-email", "--sender", "Eric", "--reply-to", "eric@example.test"]) == 2
    assert "pass --url" in capsys.readouterr().err
    assert cli(["create", str(path), "--send-email", "--url", "https://ansatze.example/sign-in", "--sender", "Eric"]) == 2
    assert "--reply-to" in capsys.readouterr().err
    assert cli(["create", str(path), "--send-email", "--url", "ftp://x", "--sender", "Eric",
                "--reply-to", "eric@example.test"]) == 2
    assert "--url must be" in capsys.readouterr().err
    assert [account["email"] for account in accounts_admin.list_accounts()] == ["owner@example.test"]
    assert cli(["create", str(path), "--send-email", "--url", "https://ansatze.example/sign-in", "--sender", "Eric",
                "--reply-to", "eric@example.test"]) == 1
    out = capsys.readouterr().out
    printed = json.loads(out[:out.index("]") + 1])
    assert out.index('"password"') < out.index("email sent: ada@example.org") < out.index(
        "email NOT sent: ben@example.org")
    assert [result["status"] for result in printed] == ["created", "created"] and sends == [
        "ada@example.org", "ben@example.org"]


def test_the_cli_access_exit_status_says_whether_all_of_it_was_done(cli, capsys):
    accounts_admin.create_accounts(accounts_admin.parse_people("a@example.test"))
    assert cli(["off", "a@example.test"]) == 0
    assert cli(["off", "a@example.test", "nobody@example.test"]) == 1
    assert cli(["on", "nobody@example.test"]) == 1
    assert cli(["group", "Field test", "a@example.test"]) == 0
    assert cli(["group", "Field test", "nobody@example.test"]) == 1
    capsys.readouterr()
    assert cli(["list"]) == 0
    assert "off  a@example.test    [Field test]" in capsys.readouterr().out


def test_the_cli_puts_created_accounts_in_a_group(cli, tmp_path, capsys):
    path = tmp_path / "people.txt"
    path.write_text("Ada - ada@example.org\n", encoding="utf-8")
    assert cli(["create", str(path), "--group", "Field test"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["group"] == "Field test"
    assert {a["email"]: a["group"] for a in accounts_admin.list_accounts()}["ada@example.org"] == "Field test"


def test_the_cli_reads_the_sites_env_file(member_console, monkeypatch, tmp_path):
    env_file = tmp_path / "console.env"
    env_file.write_text("AGENT_MONITOR_APP_URL=https://from-env.example\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_MONITOR_ENV_PATH", str(env_file))
    # Registered with monkeypatch first, so whatever the file sets is undone afterwards.
    monkeypatch.setenv("AGENT_MONITOR_APP_URL", "")
    monkeypatch.delenv("AGENT_MONITOR_APP_URL")
    monkeypatch.setenv("AGENT_MONITOR_BASE_PATH", "/m")
    accounts_admin._load_site_environment()
    assert accounts_admin.sign_in_url() == "https://from-env.example/m/login"


def test_an_older_database_gains_the_columns_with_every_account_on(monkeypatch, tmp_path):
    db_path = tmp_path / "users.db"
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT,"
                   " password_hash BLOB, salt BLOB, google_sub TEXT UNIQUE, picture TEXT, is_admin INTEGER DEFAULT 0,"
                   " is_guest INTEGER NOT NULL DEFAULT 0, created_at TEXT, email_verified_at TEXT)")
        db.execute("INSERT INTO users (email, name, is_admin, created_at) VALUES ('old@example.test', 'Old', 0, 'x')")
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "_INITIALIZED", False)
    [account] = accounts_admin.list_accounts()
    assert (account["email"], account["access"], account["group"], account["last_sign_in_at"]) == (
        "old@example.test", "on", "", None)


def test_the_migration_tolerates_another_process_adding_a_column_first(tmp_path):
    db = sqlite3.connect(tmp_path / "users.db")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")

    class Racing:
        """Read the columns before another process adds them, then try to add them too."""

        def execute(self, sql, *args):
            if sql.startswith("PRAGMA table_info"):
                return types.SimpleNamespace(fetchall=lambda: [{"name": "id"}, {"name": "email"}])
            column = sql.split("ADD COLUMN ")[1].split()[0]
            db.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")  # the other process
            return db.execute(sql, *args)

    auth._migrate_access_pause(Racing())
    assert {row["name"] for row in db.execute("PRAGMA table_info(users)")} >= {
        "access_paused_at", "last_sign_in_at", "account_group"}
    db.close()


@pytest.mark.parametrize(("line", "name", "email"), [
    # Separators stuck to an address are not part of it.
    ("Ada Lovelace:ada@uni.example", "Ada Lovelace", "ada@uni.example"),
    ("Ben <mailto:ben@x.org>", "Ben", "ben@x.org"),
    ("Ben - MAILTO:ben@x.org", "Ben", "ben@x.org"),
    # "mailto:" inside a word is not a link's prefix: the address after the colon is the one on the line.
    ("ada.mailto:bob@x.org", "ada.mailto", "bob@x.org"),
    ("(see ada@uni.example).", "see", "ada@uni.example"),
    ("'ada@uni.example'", "", "ada@uni.example"),
    ("{ada@uni.example}", "", "ada@uni.example"),
    # Nor are Markdown and a sentence's punctuation around it.
    ("*ada@uni.example*", "", "ada@uni.example"),
    ("`ada@uni.example`", "", "ada@uni.example"),
    ("_ada@uni.example_", "", "ada@uni.example"),
    ("Write to ada@uni.example!", "Write to", "ada@uni.example"),
    # A list's number before a bare address is not a name; a name after it is used.
    ("1. hilbert@uni.example", "", "hilbert@uni.example"),
    ("12. hilbert@uni.example - David Hilbert", "David Hilbert", "hilbert@uni.example"),
    ("(2) hilbert@uni.example (David Hilbert)", "David Hilbert", "hilbert@uni.example"),
    ("• hilbert@uni.example", "", "hilbert@uni.example"),
])
def test_separators_markdown_and_numbering_never_reach_an_address_or_a_name(line, name, email):
    [person] = accounts_admin.parse_people(line)
    assert (person["name"], person["email"]) == (name, email)
    assert accounts_admin.usable_address(person["email"])


@pytest.mark.parametrize("line", [
    "emmy\x7f@uni.example", ".emmy@uni.example", "emmy.@uni.example", "emmy@-uni.example", "emmy@uni..example",
    "emmy@uni", "a@b@uni.example", "x" * 300 + "@gmail.com",
    # Where the address starts is unclear: refused, never guessed at.
    "Ada Lovelace|ada@uni.example", "Ada Lovelace -ada@uni.example", "_ada@uni.example", "*ada@uni.example",
    "a/b@uni.example", "a=b@uni.example", "Name " + "y" * 996 + "@x.org"])
def test_an_address_mail_would_not_reach_exactly_is_invalid(member_console, line):
    [result] = accounts_admin.create_accounts(accounts_admin.parse_people(line))
    assert result["status"] == "invalid", result
    assert [account["email"] for account in accounts_admin.list_accounts()] == ["owner@example.test"]


TRICKY_LINES = [
    "Ada Lovelace <ada@uni.example>", "ada.mailto:bob@x.org", "mailto:mailto:x@y.org", "x.mailto:y@z.org",
    "Ben, ben@x.org; extra", "*ben@x.org", "ben@x.org*", "**ben@x.org**", "__ben@x.org__", "'o'brien@x.org'",
    "Name:ada@uni.example:", "<<ada@uni.example>>", "ada@uni.example.", "ada@uni.example...!?", "ada@ex-ample.org-",
    "Ada \u200bLove <a\u200bda@uni.example>", "\u202eada@uni.example", "Ada (ada@uni.example)", "[ada@uni.example](x)",
    "ada@uni.example\tAda", "1) ada@uni.example", "José <jose@uni.example>", "ＡＢ＠example.org", "ab＠example.org",
]


@pytest.mark.parametrize("line", TRICKY_LINES)
def test_every_address_taken_is_on_its_line_exactly_as_written(line):
    import unicodedata

    for person in accounts_admin.parse_people(line):
        if person["email"]:
            visible = "".join(char for char in person["line"] if unicodedata.category(char) != "Cf").lower()
            assert person["email"] in visible, (line, person)


def test_a_long_line_comes_back_whole_and_a_too_long_one_is_refused(member_console):
    line = "Ada " + "Lovelace " * 40 + "<ada@uni.example>"
    [person] = accounts_admin.parse_people(line)
    assert len(line) > 300 and person["line"] == line and person["email"] == "ada@uni.example"
    [result] = accounts_admin.create_accounts([person], preview=True)
    assert result["line"] == line
    [too_long] = accounts_admin.parse_people("x" * 1001 + " ada@uni.example")
    assert too_long["error"] == "This line is too long" and too_long["email"] == ""


def test_a_group_is_cleaned_the_same_way_twice_and_found_whatever_its_spelling(member_console):
    for raw in ("Field \u200b test", "  Field\ttest ", "x" * 70 + " y", "Fi\x1beld  test"):
        assert accounts_admin.clean_group(accounts_admin.clean_group(raw)) == accounts_admin.clean_group(raw)
    assert accounts_admin.clean_group("Field \u200b test") == "Field test"
    assert accounts_admin.resolve_group("Field Test") == ("Field Test", True)
    accounts_admin.create_accounts(accounts_admin.parse_people("a@example.test"), group="Field test")
    for spelling in ("Field Test", "field  TEST", "Fïeld test", " Field\u200btest "):
        assert accounts_admin.resolve_group(spelling)[0] in ("Field test", "Fieldtest")
    assert accounts_admin.resolve_group("FIELD TEST") == ("Field test", False)
    [late] = accounts_admin.create_accounts(accounts_admin.parse_people("b@example.test"), group="field test")
    assert late["group"] == "Field test"
    changed = accounts_admin.set_group(["a@example.test"], "Other")
    assert (changed["group"], changed["group_is_new"]) == ("Other", True)
    with pytest.raises(ValueError, match="No account is in the group"):
        accounts_admin.set_access([], on=True, others_off=True, group="Nobody's group")
    assert accounts_admin.set_access([], on=True, others_off=True, group="FIELD test")["turned_off"] == [
        "b@example.test"]


def test_an_invitation_without_its_password_is_never_sent(mail):
    assert not accounts_admin.send_invite(email="ben@example.test", name="Ben", password="Pa55word42xy", url="u",
                                          sender_name="Eric", reply_to=None, message="Hello {name}, welcome!")
    assert mail.sent == []
    # A message of only whitespace is the default one.
    assert accounts_admin.send_invite(email="ben@example.test", name="Ben", password="Pa55word42xy", url="u",
                                      sender_name="Eric", reply_to=None, message=" \n\t ")
    assert "Password: Pa55word42xy" in mail.sent[0].get_content()


def test_the_admin_api_refuses_a_message_without_the_password_and_keeps_group_spellings(member_console, mail):
    with running_server() as port:
        admin = _admin_cookie()
        refused = _post(port, "/api/admin/accounts", admin,
                        {"text": "a@example.test", "send_email": True, "message": "Welcome, {name}!"})
        blank = _post(port, "/api/admin/accounts", admin,
                      {"text": "a@example.test", "send_email": True, "message": "\n  ", "group": "Field test"})
        preview_new = _post(port, "/api/admin/accounts", admin, {"text": "b@example.test", "preview": True,
                                                                   "group": "Other group"})
        later = _post(port, "/api/admin/accounts", admin, {"text": "b@example.test", "group": "FIELD  test"})
        grouped = _post(port, "/api/admin/accounts/group", admin, {"emails": ["b@example.test"], "group": "field test"})
        nobody = _post(port, "/api/admin/accounts/access", admin,
                       {"emails": [], "access": "on", "others": "off", "group": "Other group"})
    assert refused[0] == 400 and "{password}" in refused[2]["error"]
    assert blank[0] == 200 and blank[2]["results"][0]["email_sent"] is True
    assert blank[2]["results"][0]["password"] in mail.sent[0].get_content()
    assert (blank[2]["group"], blank[2]["group_is_new"]) == ("Field test", True)
    assert (preview_new[2]["group"], preview_new[2]["group_is_new"]) == ("Other group", True)
    assert (later[2]["group"], later[2]["group_is_new"]) == ("Field test", False)
    assert later[2]["results"][0]["group"] == "Field test"
    assert (grouped[2]["group"], grouped[2]["group_is_new"]) == ("Field test", False)
    assert nobody[0] == 400 and "No account is in the group" in nobody[2]["error"]
