"""Accounts an administrator manages: creating them in bulk with generated
passwords, emailing each person their sign-in details, and turning each
account's access on or off.

The console's admin view uses these through ``/api/admin/accounts``
(``console_server``); an operator on the server can use them from the
command line too (``python -m agent_monitor.accounts_admin --help``).

* :func:`parse_people` reads a pasted list, one person per line:
  ``Name - email``, ``Name <email>``, ``Name, email`` or ``email``.
* :func:`create_accounts` creates the accounts (signed-in ready: an
  operator-created account needs no email verification), each with a random
  password that is returned once and never stored in clear; with
  ``preview`` it only says what it would do.
* :func:`set_access` turns access off (the person is signed out at once and
  cannot sign in) or on again, optionally turning everyone else in a group
  off.  An administrator's access is never turned off.
* :func:`set_group` puts accounts in a group (a field test's participants,
  say), so that "everyone else" means everyone else in that group, never
  every member of the site.
* :func:`set_full_access` gives an account full access, or takes it back:
  it may then link its own Claude Code and Codex and run every engine,
  including the harnesses that run local tools on this host
  (``account_policy``).
* :func:`new_password` gives an account a new random password (signed out);
  never an administrator's.
* :func:`sign_in_url` is the sign-in address an invitation names: the site's
  configured address, never one taken from a request.
* :func:`invite_email` fills the message; :func:`send_invite` sends it with
  the site's mail settings (``SMTP_*``), from the site's address under the
  sender's name, with replies going to the sender.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import sys
import unicodedata
from contextlib import closing
from email.utils import getaddresses
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from agent_monitor import ROOT, auth

logger = logging.getLogger("agent_monitor.accounts_admin")

# Letters and digits that cannot be mistaken for one another when typed from an email.
PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
DEFAULT_PASSWORD_LENGTH = 12
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 32
MAX_PEOPLE = 200
MAX_LINE_LENGTH = 1000
MAX_GROUP_LENGTH = 60
DEFAULT_SUBJECT = "Your Ansätze account"
DEFAULT_MESSAGE = """Dear {name},

An account has been created for you on Ansätze.

Sign in at: {url}
Email: {email}
Password: {password}

Please keep these details to yourself. If you have any questions, reply to this email.

Best regards,
{sender}
"""
RESET_SUBJECT = "Your new Ansätze password"
RESET_MESSAGE = """Dear {name},

Here is a new password for your Ansätze account.

Sign in at: {url}
Email: {email}
Password: {password}

Please keep it to yourself. If you have any questions, reply to this email.

Best regards,
{sender}
"""
PLACEHOLDERS = ("name", "email", "password", "url", "sender")
# An address is everything around an @ up to a space or a character no plain
# address has (, ; : brackets, double quotes): a hyphen or an apostrophe
# inside it belongs to it.
_ADDRESS_RE = re.compile(r"[^\s,;:<>()\[\]\"]+@[^\s,;:<>()\[\]\"]+")
# A mailto: link's prefix where a link starts (never in the middle of a word).
_MAILTO_RE = re.compile(r"(?i)(?<![^\s<(\[\"'])mailto:")
# Marks that may wrap an address on both sides: "*a@b*", "`a@b`", "'a@b'".
_WRAPPERS = {"*": "*", "`": "`", "'": "'", "_": "_", "{": "}"}
# Characters an address may legally start with or hold, but which in a pasted
# line more likely belong to what is around it ("_a@b", "-a@b", "Name|a@b"):
# such a line is refused rather than guessed at.
_UNCLEAR_START = "'*`_{}-!?#$%&+=^~|/"
_UNCLEAR_INSIDE = "|/="
# A plain address that mail goes to exactly: a dot-atom, then a host name.
_LOCAL_RE = re.compile(r"[A-Za-z0-9!#$%&'*+=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+=?^_`{|}~-]+)*")
_DOMAIN_RE = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
# A list's numbering or bullet before a name ("1. ", "(2) ", "- ", "• ").
_BULLET_RE = re.compile(r"^(?:\(?\d{1,3}[.):]?\)?|[-*•·–—])(?:\s+|$)")
# What may separate a name from its address: "Name - email", "Name, email",
# "Name <email>", "Name<TAB>email", a quoted "Last, First".
_NAME_EDGES = " \t-–—,;:|<>()[]\"'"
# Pasted text can carry characters nobody sees (a byte-order mark, a
# zero-width space): never part of a name or an address.
_INVISIBLE = dict.fromkeys(map(ord, "\ufeff\u200b\u2060\u00ad"))
_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")


def _clean_name(text: str) -> str:
    name = " ".join(_BULLET_RE.sub("", text.strip()).strip(_NAME_EDGES).split())[:120]
    return name if any(char.isalpha() for char in name) else ""


def _address_in(token: str) -> str | None:
    """The address in ``token`` exactly as written, or None when where it
    starts or ends is unclear.  Only a wrapper on both sides and a
    sentence's punctuation after it (a host name ends with a letter or a
    digit) are taken off, so the address is always on the line as typed."""
    token = "".join(char for char in token if unicodedata.category(char) != "Cf")
    while len(token) > 2 and token[0] in _WRAPPERS and token.endswith(_WRAPPERS[token[0]]):
        token = token[1:-1]
    token = re.sub(r"[^A-Za-z0-9]+$", "", token)
    local = token.rpartition("@")[0]
    if not local or local[0] in _UNCLEAR_START or any(char in local for char in _UNCLEAR_INSIDE):
        return None
    return token


def usable_address(email: str) -> bool:
    """Whether ``email`` is a plain address that mail is delivered to
    exactly as written (so the account and its invitation agree)."""
    local, at, domain = str(email or "").rpartition("@")
    if not at or len(email) > 254 or len(local) > 64:
        return False
    if not (_LOCAL_RE.fullmatch(local) and _DOMAIN_RE.fullmatch(domain)):
        return False
    return getaddresses([email]) == [("", email)]


def parse_people(text: str) -> list[dict[str, str]]:
    """``[{name, email, line}]`` for each non-blank line of ``text``: the
    line's one email address, exactly as written there (only lower-cased),
    and the name before it (or after it).  A line with no address, more
    than one, an address whose start or end is unclear, or a line longer
    than 1000 characters has ``email`` "" and an ``error`` (reported as
    invalid).  ``line`` is the whole line, so it can be pasted again."""
    people: list[dict[str, str]] = []
    for raw in str(text or "").splitlines():
        line = raw.translate(_INVISIBLE).strip()
        if not line:
            continue
        if len(line) > MAX_LINE_LENGTH:
            people.append({"name": "", "email": "", "line": line, "error": "This line is too long"})
            continue
        # "mailto:" is blanked in place, so positions on the line stay as they are.
        scan = _MAILTO_RE.sub(lambda match: " " * len(match.group(0)), line)
        found = list(_ADDRESS_RE.finditer(scan))
        if len(found) != 1:
            people.append({"name": "", "email": "", "line": line,
                           "error": "More than one email address on this line" if found
                           else "No email address on this line"})
            continue
        match = found[0]
        email = _address_in(match.group(0))
        visible = "".join(char for char in line if unicodedata.category(char) != "Cf").lower()
        if email is None or email.lower() not in visible:
            people.append({"name": "", "email": "", "line": line,
                           "error": "Unclear where the address on this line starts or ends: put spaces around it"})
            continue
        name = _clean_name(scan[:match.start()]) or _clean_name(scan[match.end():])
        people.append({"name": name, "email": email.lower(), "line": line})
    return people


def generate_password(length: int = DEFAULT_PASSWORD_LENGTH) -> str:
    """A random password of ``length`` (8-32) unambiguous letters and digits,
    with at least one of each kind."""
    length = max(MIN_PASSWORD_LENGTH, min(MAX_PASSWORD_LENGTH, int(length)))
    while True:
        password = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
        if (any(char.isdigit() for char in password) and any(char.islower() for char in password)
                and any(char.isupper() for char in password)):
            return password


def _clean_length(value: object) -> int:
    try:
        length = int(value) if value not in (None, "") else DEFAULT_PASSWORD_LENGTH
    except (TypeError, ValueError):
        raise ValueError("password_length must be a number") from None
    if not MIN_PASSWORD_LENGTH <= length <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"Passwords must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters")
    return length


def clean_group(value: object) -> str:
    """A group's name as stored: unprintable characters dropped, whitespace
    collapsed, at most 60 characters; "" for none.  Cleaning it again
    changes nothing."""
    if value is not None and not isinstance(value, str):
        raise ValueError("group must be text")
    text = "".join(char for char in value or "" if char.isprintable() or char.isspace())
    return " ".join(text.split())[:MAX_GROUP_LENGTH].strip()


def _group_key(name: str) -> str:
    """How two spellings of a group compare: ignoring case, accents and spacing."""
    folded = "".join(char for char in unicodedata.normalize("NFKD", name) if not unicodedata.combining(char))
    return " ".join(folded.casefold().split())


def resolve_group(value: object) -> tuple[str, bool]:
    """``(group, is_new)``: the cleaned group, written as an existing group is
    when they differ only in case, accents or spacing ("Field Test" is the
    existing "Field test"), and whether no account is in it yet."""
    group = clean_group(value)
    if not group:
        return "", False
    with closing(auth._conn()) as conn:
        existing = [row[0] for row in conn.execute(
            "SELECT DISTINCT account_group FROM users WHERE account_group IS NOT NULL AND account_group != ''"
            " AND is_guest=0")]
    if group in existing:
        return group, False
    key = _group_key(group)
    for name in sorted(existing):
        if _group_key(name) == key:
            return name, False
    return group, True


def list_accounts() -> list[dict[str, Any]]:
    """Every registered (non-guest) account, oldest first, with its access
    state, its group and when it last signed in."""
    with closing(auth._conn()) as conn:
        rows = conn.execute(
            "SELECT u.id, u.email, u.name, u.is_admin, u.created_at, u.access_paused_at, u.account_group,"
            " u.full_access,"
            " COALESCE(u.last_sign_in_at,"
            " (SELECT MAX(s.created_at) FROM sessions s WHERE s.user_id=u.id)) AS last_sign_in_at"
            " FROM users u WHERE u.is_guest=0 ORDER BY u.id"
        ).fetchall()
    return [{
        "id": int(row["id"]),
        "email": row["email"],
        "name": row["name"] or "",
        "is_admin": bool(row["is_admin"]),
        "created_at": row["created_at"],
        "access": "off" if row["access_paused_at"] and not row["is_admin"] else "on",
        "access_paused_at": row["access_paused_at"] if not row["is_admin"] else None,
        "group": row["account_group"] or "",
        "full_access": bool(row["is_admin"] or row["full_access"]),
        "last_sign_in_at": row["last_sign_in_at"],
    } for row in rows]


def _grant_full_access(email: str, on: bool = True) -> bool | None:
    """Give a non-administrator account full access (or, with ``on`` false,
    take it back): True when that changed it, False when it was so already,
    None for an administrator (who always has it)."""
    with closing(auth._conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT id, is_admin, full_access FROM users WHERE email=? AND is_guest=0",
                           (email,)).fetchone()
        if row is None or row["is_admin"]:
            return None
        if bool(row["full_access"]) == on:
            return False
        conn.execute("UPDATE users SET full_access=? WHERE id=?", (int(on), row["id"]))
    return True


def _join_group(email: str, group: str) -> bool | None:
    """Put a non-administrator account in ``group``: True when that changed
    its group, False when it was in it already, None for an administrator
    (whose group is never changed here)."""
    with closing(auth._conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT id, is_admin, account_group FROM users WHERE email=? AND is_guest=0",
                           (email,)).fetchone()
        if row is None or row["is_admin"]:
            return None
        if (row["account_group"] or "") == group:
            return False
        conn.execute("UPDATE users SET account_group=? WHERE id=?", (group, row["id"]))
    return True


def create_accounts(people: Iterable[Mapping[str, Any]], *, password_length: object = None,
                    preview: bool = False, group: object = None,
                    full_access: bool | None = None) -> list[dict[str, Any]]:
    """Create an account for each person (``{name, email, line?}``) with a
    random password.  Returns, in order, ``{email, name, line, status,
    password?, error?}`` where ``status`` is ``created`` (with its password,
    shown only now), ``exists`` (left as it is), ``duplicate`` (named twice
    in the list), ``invalid``, or ``not_processed``: an error the server could
    not get past (the database locked, say) stopped the run at that line, and
    that line and every later one were left for another try.  With
    ``preview`` nothing is created: ``new`` marks an account that would be.

    With ``group``, new accounts are put in it, and so is an existing
    account that is not an administrator's (``group_joined`` when that
    changes its group).  New accounts get full access when ``full_access``
    is true, not when it is false, and by the site's default
    (auth.full_access_by_default) when it is None; an existing account never
    gets it here (``full_access_skipped``): whoever
    registered that address first may not be the person on the list, so it is
    given from the table once checked.  Nothing else about an existing
    account changes."""
    length = _clean_length(password_length)
    if full_access is None:
        full_access = auth.full_access_by_default()
    group, _new = resolve_group(group)
    people = list(people)
    if len(people) > MAX_PEOPLE:
        raise ValueError(f"Add at most {MAX_PEOPLE} people at a time")
    with closing(auth._conn()) as conn:
        # The first account of a site becomes its administrator: never one made here.
        if not auth._registered_user_exists(conn):
            raise ValueError("Create the administrator's own account first")
        existing = {row["email"]: (bool(row["is_admin"]), row["account_group"] or "", bool(row["full_access"]))
                    for row in conn.execute("SELECT email, is_admin, account_group, full_access FROM users"
                                            " WHERE is_guest=0")
                    } if preview else {}
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    stopped = False
    for person in people:
        email = str(person.get("email") or "").strip().lower()
        name = " ".join(str(person.get("name") or "").split())[:120]
        line = str(person.get("line") or email)[:MAX_LINE_LENGTH]
        entry: dict[str, Any] = {"email": email, "name": name, "line": line}
        if stopped:
            entry.update(status="not_processed", error="Not processed: an earlier line stopped the run")
        elif not email or email.endswith("@public.local"):
            entry.update(status="invalid", error=str(person.get("error") or "No valid email address on this line")
                         + ": " + line[:200])
        elif not email.isascii():
            entry.update(status="invalid", error="Only plain (ASCII) email addresses can be added here: " + line[:200])
        elif not usable_address(email) or not auth._EMAIL_RE.match(email):
            entry.update(status="invalid", error="Not a usable email address: " + line[:200])
        elif email in seen:
            entry.update(status="duplicate", error="Listed more than once")
        elif preview:
            seen.add(email)
            if email in existing:
                entry.update(status="exists", error="An account with this email already exists")
                is_admin, current, has_full = existing[email]
                if group and not is_admin:
                    entry["group"] = group
                    if current != group:
                        entry["group_joined"] = True
                if full_access and not is_admin and not has_full:
                    entry["full_access_skipped"] = True
            else:
                entry.update(status="new")
                if group:
                    entry["group"] = group
                if full_access:
                    entry["full_access"] = True
        else:
            seen.add(email)
            password = generate_password(length)
            try:
                auth.register(email, password, name or None)
            except ValueError as exc:
                if "already registered" in str(exc):
                    entry.update(status="exists", error="An account with this email already exists")
                else:
                    entry.update(status="invalid", error=str(exc)[:200])
            except Exception as exc:  # noqa: BLE001 - keep the passwords already made
                logger.exception("[ADMIN] creating an account failed; the run stops at this line")
                stopped = True
                entry.update(status="not_processed",
                             error=f"The server could not create this account ({type(exc).__name__}: "
                                   f"{str(exc)[:160]}); this line and the ones after it were not processed")
            else:
                entry.update(status="created", password=password)
            if group and entry["status"] in ("created", "exists"):
                try:
                    joined = _join_group(email, group)
                except Exception:  # noqa: BLE001 - the account (and its password) stand regardless
                    logger.exception("[ADMIN] putting an account in its group failed")
                    entry["group_error"] = "Could not be put in the group; try Set group"
                else:
                    if joined is not None:
                        entry["group"] = group
                        if joined and entry["status"] == "exists":
                            entry["group_joined"] = True
            if not full_access and entry["status"] == "created":
                try:
                    _grant_full_access(email, on=False)
                except Exception:  # noqa: BLE001 - the account (and its password) stand regardless
                    logger.exception("[ADMIN] taking full access back from a new account failed")
                    entry["full_access_error"] = "Has full access (the default); remove it from the table"
                    entry["full_access"] = True
            if full_access and entry["status"] == "created":
                try:
                    granted = _grant_full_access(email)
                except Exception:  # noqa: BLE001 - the account (and its password) stand regardless
                    logger.exception("[ADMIN] giving an account full access failed")
                    entry["full_access_error"] = "Could not be given full access; try again from the table"
                else:
                    if granted is not None:
                        entry["full_access"] = True
            elif full_access and entry["status"] == "exists":
                with closing(auth._conn()) as conn:
                    row = conn.execute("SELECT is_admin, full_access FROM users WHERE email=? AND is_guest=0",
                                       (email,)).fetchone()
                if row is not None and not row["is_admin"] and not row["full_access"]:
                    entry["full_access_skipped"] = True
        results.append(entry)
    return results


def _account(conn, email: str):
    return conn.execute("SELECT * FROM users WHERE email=? AND is_guest=0", (email,)).fetchone()


def _pause(conn, user_id: int) -> None:
    conn.execute("UPDATE users SET access_paused_at=COALESCE(access_paused_at, ?) WHERE id=?",
                 (auth._now(), user_id))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def set_access(emails: Iterable[str], *, on: bool, others_off: bool = False, group: object = None) -> dict[str, Any]:
    """Turn the access of these accounts on, or off.  Off signs the person
    out at once (every session ends) and refuses their sign-in until it is
    on again.  Returns ``{"updated": [emails], "skipped": [{email, reason}]}``;
    administrators, guests and unknown addresses are skipped.

    With ``others_off`` (only with ``on``, and only within a ``group``),
    every other account of that group that is not an administrator's is
    turned off in the same transaction, whether or not the caller knew of
    it; ``turned_off`` lists the ones that were on.  Accounts outside the
    group are never touched by it."""
    group, is_new = resolve_group(group)
    if others_off and not on:
        raise ValueError("Everyone else can only be turned off when the listed accounts are turned on")
    if others_off and not group:
        raise ValueError("Say which group's other accounts to turn off")
    if others_off and is_new:
        raise ValueError(f"No account is in the group “{group}”")
    updated: list[str] = []
    skipped: list[dict[str, str]] = []
    wanted = list(dict.fromkeys(str(email or "").strip().lower() for email in emails if str(email or "").strip()))
    if len(wanted) > MAX_PEOPLE * 5:
        raise ValueError("Too many accounts in one request")
    listed: set[int] = set()
    turned_off: list[str] = []
    with closing(auth._conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for email in wanted:
            row = _account(conn, email)
            if row is None:
                skipped.append({"email": email, "reason": "No account uses this email"})
                continue
            if row["is_admin"]:
                skipped.append({"email": email, "reason": "An administrator's access is always on"})
                continue
            listed.add(int(row["id"]))
            if on:
                conn.execute("UPDATE users SET access_paused_at=NULL WHERE id=?", (row["id"],))
            else:
                _pause(conn, row["id"])
            updated.append(email)
        if others_off:
            for row in conn.execute("SELECT id, email FROM users WHERE is_guest=0 AND is_admin=0"
                                    " AND access_paused_at IS NULL AND account_group=? ORDER BY id",
                                    (group,)).fetchall():
                if int(row["id"]) not in listed:
                    _pause(conn, row["id"])
                    turned_off.append(row["email"])
    result: dict[str, Any] = {"updated": updated, "skipped": skipped}
    if others_off:
        result["turned_off"] = turned_off
    return result


def set_full_access(emails: Iterable[str], *, on: bool) -> dict[str, Any]:
    """Give these accounts full access, or take it back.  Returns
    ``{"updated": [emails], "skipped": [{email, reason}]}``; administrators
    (who always have it), guests and unknown addresses are skipped.  Taking
    it back applies from the account's next request; a run it already
    started finishes."""
    updated: list[str] = []
    skipped: list[dict[str, str]] = []
    wanted = list(dict.fromkeys(str(email or "").strip().lower() for email in emails if str(email or "").strip()))
    if len(wanted) > MAX_PEOPLE * 5:
        raise ValueError("Too many accounts in one request")
    with closing(auth._conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for email in wanted:
            row = _account(conn, email)
            if row is None:
                skipped.append({"email": email, "reason": "No account uses this email"})
                continue
            if row["is_admin"]:
                skipped.append({"email": email, "reason": "An administrator always has full access"})
                continue
            conn.execute("UPDATE users SET full_access=? WHERE id=?", (1 if on else 0, row["id"]))
            updated.append(email)
    return {"updated": updated, "skipped": skipped}


def set_group(emails: Iterable[str], group: object) -> dict[str, Any]:
    """Put these accounts in ``group`` ("" takes them out of theirs).
    Returns ``{"updated": [emails], "skipped": [{email, reason}], "group",
    "group_is_new"}``, with the group as stored (see :func:`resolve_group`)."""
    group, is_new = resolve_group(group)
    updated: list[str] = []
    skipped: list[dict[str, str]] = []
    wanted = list(dict.fromkeys(str(email or "").strip().lower() for email in emails if str(email or "").strip()))
    if len(wanted) > MAX_PEOPLE * 5:
        raise ValueError("Too many accounts in one request")
    with closing(auth._conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for email in wanted:
            row = _account(conn, email)
            if row is None:
                skipped.append({"email": email, "reason": "No account uses this email"})
                continue
            conn.execute("UPDATE users SET account_group=? WHERE id=?", (group or None, row["id"]))
            updated.append(email)
    return {"updated": updated, "skipped": skipped, "group": group, "group_is_new": is_new and bool(updated)}


def new_password(email: str, *, password_length: object = None) -> str:
    """Give an account a new random password (signing it out everywhere) and
    return it.  Never an administrator's: one administrator must not be able
    to take over another (their own password is changed from the sign-in
    page, or on the server with ``agent-monitor set-password``)."""
    email = str(email or "").strip().lower()
    length = _clean_length(password_length)
    with closing(auth._conn()) as conn:
        row = _account(conn, email)
    if row is None:
        raise ValueError("No account uses this email")
    if row["is_admin"]:
        raise ValueError("An administrator's password is not changed here: use \"Forgot password\" on the "
                         "sign-in page, or agent-monitor set-password on the server")
    password = generate_password(length)
    auth.set_password(email, password)
    return password


def sign_in_url(base_path: str | None = None) -> str:
    """The sign-in address an invitation names: ``AGENT_MONITOR_APP_URL``
    (with the console's mount, ``base_path`` or ``AGENT_MONITOR_BASE_PATH``,
    when the URL has no path of its own) + ``/login``; "" when the site's
    address is not set.  Never taken from a request's Host."""
    mount = os.environ.get("AGENT_MONITOR_BASE_PATH", "") if base_path is None else base_path
    base = auth._validated_reset_base(os.environ.get("AGENT_MONITOR_APP_URL", ""), mount)
    return f"{base}/login" if base else ""


def invite_email(*, template: str, name: str, email: str, password: str, url: str, sender: str) -> str:
    """``template`` with its placeholders ({name}, {email}, {password},
    {url}, {sender}) filled in; other braces are left as written."""
    values = {"name": name or email.split("@")[0], "email": email, "password": password,
              "url": url or "(the site's address)", "sender": sender}
    return _PLACEHOLDER_RE.sub(lambda match: values[match.group(1)], str(template or ""))


def send_invite(*, email: str, name: str, password: str, url: str, sender_name: str, reply_to: str | None,
                subject: str | None = None, message: str | None = None) -> bool:
    """Email one person their sign-in details with the site's mail settings;
    True when the mail server accepted it.  Never raises: a mail that fails
    must not lose the password it carries.  A message without the password
    in it is never sent."""
    try:
        template = message if str(message or "").strip() else DEFAULT_MESSAGE
        body = invite_email(template=template, name=name, email=email, password=password, url=url,
                            sender=sender_name or "")
        if password not in body:
            logger.error("[ADMIN] Account invitation for %s not sent: its message has no {password}", email)
            return False
        title = " ".join(str(subject or DEFAULT_SUBJECT).split())[:200] or DEFAULT_SUBJECT
        return auth._send_email(email, title, body, purpose="account invitation",
                                from_name=f"{sender_name} via Ansätze" if sender_name else None, reply_to=reply_to)
    except Exception as exc:  # noqa: BLE001
        logger.error("[ADMIN] Account invitation email for %s failed (%s)", email, type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# command line (an operator on the server, with the site's environment)
# ---------------------------------------------------------------------------


def _shown(value: object) -> str:
    """``value`` safe to print on a terminal: control characters (a name
    chosen at sign-up could carry some) are written as escapes."""
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in str(value or ""))


def _load_site_environment() -> None:
    """The console's own settings (mail, the site's address) from its env
    file, as the console loads them: never overriding what is already set."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(os.environ.get("AGENT_MONITOR_ENV_PATH") or ROOT / ".env", override=False)


def _cli_sign_in_url(explicit: str) -> tuple[str, str | None]:
    """The sign-in address for the command line's emails, or why there is none.
    On a host the console's own address and mount are often set only for its
    service, so a bare site address is not trusted to be the sign-in page."""
    if explicit.strip():
        url = auth._validated_reset_base(explicit.strip())
        return (url, None) if url else ("", "--url must be an http(s) address")
    app_url = os.environ.get("AGENT_MONITOR_APP_URL", "").strip()
    if not app_url:
        return "", "No sign-in address for the emails (set AGENT_MONITOR_APP_URL or pass --url)"
    if not urlsplit(app_url).path.strip("/") and not os.environ.get("AGENT_MONITOR_BASE_PATH", "").strip():
        return "", ("AGENT_MONITOR_APP_URL has no path and AGENT_MONITOR_BASE_PATH is not set, so the sign-in "
                    "page's address is unknown: pass --url (for example https://<site>/sign-in)")
    return sign_in_url(), None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_monitor.accounts_admin",
                                     description="Create accounts and turn their access on or off. Run it with "
                                                 "the site's AGENT_MONITOR_DATA_DIR and AGENT_MONITOR_ENV_PATH.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list accounts and their access")
    create = commands.add_parser("create", help="create accounts from a file, one person per line")
    create.add_argument("file", help="the list: 'Name - email', 'Name <email>', 'Name, email' or 'email' per line")
    create.add_argument("--password-length", type=int, default=DEFAULT_PASSWORD_LENGTH)
    create.add_argument("--send-email", action="store_true", help="email each new person their sign-in details")
    create.add_argument("--url", default="", help="the sign-in address to put in the emails (default: the "
                                                  "site's, from AGENT_MONITOR_APP_URL and AGENT_MONITOR_BASE_PATH)")
    create.add_argument("--sender", default="", help="the name the emails are signed with (needed with --send-email)")
    create.add_argument("--reply-to", default="", help="the address replies go to (needed with --send-email)")
    create.add_argument("--group", default="", help="put the new (and existing) accounts in this group")
    create.add_argument("--full-access", action=argparse.BooleanOptionalAction, default=None,
                        help="give the new accounts full access (or, with --no-full-access, not): their own "
                             "Claude Code and Codex, and every engine, including harnesses that run local tools "
                             "on this host (default: the site's, AGENT_MONITOR_FULL_ACCESS_DEFAULT, on unless 0; "
                             "an address that already has an account gets it only with the full-access command)")
    for name, help_text in (("on", "turn access on"), ("off", "turn access off (signs the person out)")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("emails", nargs="+")
    full = commands.add_parser("full-access", help="give accounts full access, or take it back")
    full.add_argument("state", choices=("on", "off"))
    full.add_argument("emails", nargs="+")
    grouping = commands.add_parser("group", help="put accounts in a group ('' takes them out)")
    grouping.add_argument("group")
    grouping.add_argument("emails", nargs="+")
    reset = commands.add_parser("password", help="give an account a new random password")
    reset.add_argument("email")
    reset.add_argument("--password-length", type=int, default=DEFAULT_PASSWORD_LENGTH)
    args = parser.parse_args(argv)
    _load_site_environment()
    if not auth.DB_PATH.exists():
        print(f"No account database at {auth.DB_PATH}: set AGENT_MONITOR_DATA_DIR to the site's data directory.",
              file=sys.stderr)
        return 2
    print(f"Account database: {auth.DB_PATH}", file=sys.stderr)
    if args.command == "list":
        for account in list_accounts():
            print(f"{account['access']:3}  {_shown(account['email'])}  {_shown(account['name'])}"
                  + (f"  [{_shown(account['group'])}]" if account["group"] else "")
                  + ("  (full access)" if account["full_access"] and not account["is_admin"] else "")
                  + ("  (administrator)" if account["is_admin"] else ""))
        return 0
    if args.command == "create":
        url, problem = _cli_sign_in_url(args.url)
        if args.send_email:
            if not auth.email_delivery_configured():
                problem = "This site cannot send email (SMTP_* is not set)"
            elif not args.sender.strip() or not args.reply_to.strip():
                problem = "--send-email needs --sender (the name the emails are signed with) and --reply-to"
            elif args.reply_to.strip() and not usable_address(args.reply_to.strip().lower()):
                problem = "--reply-to is not a usable email address"
            if problem:
                print(f"{problem}: nothing was created.", file=sys.stderr)
                return 2
        with open(args.file, encoding="utf-8-sig") as handle:
            results = create_accounts(parse_people(handle.read()), password_length=args.password_length,
                                      group=args.group, full_access=args.full_access)
        # Every password is out before the first email is tried, so a run
        # stopped while mailing loses none of them.
        print(json.dumps(results, ensure_ascii=False, indent=1), flush=True)
        ok = all(result["status"] in ("created", "exists") for result in results)
        if args.send_email:
            for result in results:
                if result["status"] != "created":
                    continue
                sent = send_invite(email=result["email"], name=result["name"], password=result["password"], url=url,
                                   sender_name=args.sender, reply_to=args.reply_to or None)
                print(f"email {'sent' if sent else 'NOT sent'}: {result['email']}", flush=True)
                ok = ok and sent
        return 0 if ok else 1
    if args.command in ("on", "off"):
        result = set_access(args.emails, on=args.command == "on")
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0 if result["updated"] and not result["skipped"] else 1
    if args.command == "full-access":
        result = set_full_access(args.emails, on=args.state == "on")
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0 if result["updated"] and not result["skipped"] else 1
    if args.command == "group":
        result = set_group(args.emails, args.group)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0 if result["updated"] and not result["skipped"] else 1
    print(new_password(args.email, password_length=args.password_length))
    return 0


if __name__ == "__main__":
    sys.exit(main())
