"""Regression tests for the guest-isolation and web findings (GI-1..4, WEB-1/2/5).

The HTTP tests reuse the isolated console fixture from
``test_console_guest_access``: a temporary users database, a fake job starter
and no provider access.
"""
from __future__ import annotations

import base64
import http.client
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from test_console_guest_access import (  # noqa: F401  (fixture import)
    APP_MOUNT,
    _cookie_from,
    _database_counts,
    guest_console,
    request,
    running_server,
)
from agent_monitor import attachments as research_attachments
from agent_monitor import auth, auto_pipeline, jobs, lean_verify
from agent_monitor import console_server as console


def _wait_until(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _guest_from(port: int, client: str | None = None) -> tuple[int, dict, str | None]:
    headers = {"X-Forwarded-Proto": "https"}
    if client:
        headers["X-Forwarded-For"] = client
    status, response_headers, raw = request(
        port, "POST", f"{APP_MOUNT}/api/auth/guest", body={}, headers=headers
    )
    if status != 200:
        return status, response_headers, None
    cookie, _token = _cookie_from(response_headers)
    return status, json.loads(raw)["user"], cookie


@pytest.fixture()
def derived_state():
    def clear():
        console._GUEST_DERIVED_WINDOWS.clear()
        console._GUEST_DERIVED_GLOBAL_WINDOW.clear()
        console._GUEST_DERIVED_ACTIVE_USERS.clear()
        console._GUEST_DERIVED_CLIENT_WINDOWS.clear()
        console._GUEST_PIPELINE_BOOTSTRAPS.clear()

    clear()
    yield
    clear()


# ── GI-2: per-client guest accounting ─────────────────────────────────────


def test_guest_client_key_buckets_by_the_proxied_client_address():
    key = console.guest_client_key
    # Host-local callers (smoke checks, tests) are exempt.
    assert key("127.0.0.1", None) is None
    assert key("::1", "") is None
    assert key("127.0.0.1", "127.0.0.1") is None
    # Behind the loopback proxy the last hop is the client Caddy accepted.
    assert key("127.0.0.1", "203.0.113.5") == "203.0.113.5"
    assert key("127.0.0.1", "198.51.100.1, 203.0.113.5") == "203.0.113.5"
    assert key("127.0.0.1", "::ffff:203.0.113.5") == "203.0.113.5"
    # A direct remote peer is keyed on itself; its header is ignored.
    assert key("198.51.100.7", "203.0.113.5") == "198.51.100.7"
    # IPv6 clients share their /64.
    assert key("127.0.0.1", "2001:db8:1:2:aaaa::1") == "2001:db8:1:2::/64"
    assert key("127.0.0.1", "2001:db8:1:2:bbbb::9") == "2001:db8:1:2::/64"
    assert key("127.0.0.1", "not-an-address") == "unparsed"


def test_guest_sessions_are_limited_per_client_before_the_site_window(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AGENT_MONITOR_GUEST_SESSIONS_PER_CLIENT_PER_HOUR", "3")
    with running_server() as port:
        attacker = [_guest_from(port, "203.0.113.5") for _ in range(5)]
        visitor = _guest_from(port, "198.51.100.20")
        other_v6 = _guest_from(port, "2001:db8:9::1")

    assert [status for status, _user, _cookie in attacker] == [200, 200, 200, 429, 429]
    refused_headers = attacker[3][1]
    assert int(refused_headers["retry-after"]) > 60  # the hourly client window
    # The site-wide per-minute window still has room for other visitors.
    assert visitor[0] == 200 and other_v6[0] == 200
    assert _database_counts(guest_console["db"])[0] == 5


def _fake_runs(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    active: list[dict] = []

    def fake_start_job(**kwargs):
        job = {
            "job_id": f"job{len(active)}",
            "run_id": f"kimi_fake_job{len(active)}",
            "engine": kwargs["engine"],
            "status": "running",
            "owner_id": kwargs["user"]["id"],
            "guest": True,
        }
        active.append(job)
        return dict(job)

    monkeypatch.setattr(console.job_manager, "start_job", fake_start_job)
    monkeypatch.setattr(console.job_manager, "list_jobs", lambda *a, **k: [dict(j) for j in active])
    return active


def test_one_client_cannot_hold_all_guest_run_capacity(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    active = _fake_runs(monkeypatch)
    body = {"engine": "kimi", "problem_text": "Prove that 2 + 2 = 4."}
    with running_server() as port:
        _, _a1, cookie_a1 = _guest_from(port, "203.0.113.5")
        _, _a2, cookie_a2 = _guest_from(port, "203.0.113.5")
        _, _b1, cookie_b1 = _guest_from(port, "198.51.100.20")
        first = request(port, "POST", f"{APP_MOUNT}/api/runs", body=body,
                        headers={"Cookie": cookie_a1, "X-Forwarded-For": "203.0.113.5"})
        # A second identity from the same client may not take the other
        # site-wide slot (global guest concurrency is 2).
        second = request(port, "POST", f"{APP_MOUNT}/api/runs", body=body,
                         headers={"Cookie": cookie_a2, "X-Forwarded-For": "203.0.113.5"})
        other = request(port, "POST", f"{APP_MOUNT}/api/runs", body=body,
                        headers={"Cookie": cookie_b1, "X-Forwarded-For": "198.51.100.20"})

        # Hourly per-client budget: finished runs free the concurrency slot
        # but still count.
        for job in active:
            job["status"] = "stopped"
        monkeypatch.setenv("AGENT_MONITOR_GUEST_RUNS_PER_CLIENT_PER_HOUR", "2")
        again = request(port, "POST", f"{APP_MOUNT}/api/runs", body=body,
                        headers={"Cookie": cookie_a2, "X-Forwarded-For": "203.0.113.5"})
        for job in active:
            job["status"] = "stopped"
        over = request(port, "POST", f"{APP_MOUNT}/api/runs", body=body,
                       headers={"Cookie": cookie_a2, "X-Forwarded-For": "203.0.113.5"})

    assert first[0] == 200, first[2]
    assert second[0] == 429 and "your network" in json.loads(second[2])["error"]
    assert other[0] == 200, other[2]
    assert again[0] == 200, again[2]
    assert over[0] == 429 and int(over[1]["retry-after"]) > 60
    assert "your network" in json.loads(over[2])["error"]
    assert len(active) == 3


def test_guest_derived_actions_are_limited_per_client(
    guest_console, derived_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from agent_monitor import proof_graph

    workspace = tmp_path / "kimi_run"
    workspace.mkdir()
    monkeypatch.setattr(console, "_workspace_for_run", lambda _rid: workspace)
    monkeypatch.setattr(console.Handler, "_owns_run", lambda _s, _r, _u: True)
    monkeypatch.setattr(console.Handler, "_is_run_owner", lambda _s, _r, _u: True)
    monkeypatch.setattr(console, "_run_record_for", lambda _rid: {"engine": "kimi", "guest": True})
    monkeypatch.setattr(proof_graph, "generate", lambda **kw: {"kind": kw["kind"], "graph": {"nodes": []}})
    monkeypatch.setenv("AGENT_MONITOR_GUEST_DERIVED_ACTIONS_PER_CLIENT_PER_HOUR", "1")
    path = f"{APP_MOUNT}/api/run/kimi_run/proof_graph"
    with running_server() as port:
        _, _g1, c1 = _guest_from(port, "203.0.113.5")
        _, _g2, c2 = _guest_from(port, "203.0.113.5")
        _, _g3, c3 = _guest_from(port, "198.51.100.20")
        ok = request(port, "POST", path, body={"kind": "informal"},
                     headers={"Cookie": c1, "X-Forwarded-For": "203.0.113.5"})
        same_client = request(port, "POST", path, body={"kind": "informal"},
                              headers={"Cookie": c2, "X-Forwarded-For": "203.0.113.5"})
        other_client = request(port, "POST", path, body={"kind": "informal"},
                               headers={"Cookie": c3, "X-Forwarded-For": "198.51.100.20"})
    assert ok[0] == 200, ok[2]
    assert same_client[0] == 429 and "your network" in json.loads(same_client[2])["error"]
    assert other_client[0] == 200, other_client[2]


# ── GI-1: the background formal refresh holds the guest's slot ───────────


def test_background_formal_refresh_counts_against_guest_derived_concurrency(
    guest_console, derived_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspaces = {}
    for name in ("run1", "run2", "run3", "run4"):
        workspaces[name] = tmp_path / name
        workspaces[name].mkdir()
    monkeypatch.setattr(console, "_workspace_for_run", lambda rid: workspaces.get(rid))
    monkeypatch.setattr(console.Handler, "_owns_run", lambda _s, _r, _u: True)
    monkeypatch.setattr(console.Handler, "_is_run_owner", lambda _s, _r, _u: True)
    monkeypatch.setattr(console, "_run_record_for", lambda rid: {"run_id": rid, "engine": "kimi", "guest": True})
    monkeypatch.setattr(
        lean_verify, "compile_or_check", lambda **_kw: {"status": "toolchain_missing"}
    )
    gate = threading.Event()
    lock = threading.Lock()
    in_flight = {"now": 0, "peak": 0, "calls": 0}

    def formal_dag_model_call(**_kwargs):
        # Stands in for proof_graph.generate(kind="formal") -> sponsored Kimi.
        with lock:
            in_flight["now"] += 1
            in_flight["calls"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        try:
            gate.wait(10)
        finally:
            with lock:
                in_flight["now"] -= 1

    monkeypatch.setattr(auto_pipeline, "_refresh_formal_once", formal_dag_model_call)
    monkeypatch.delenv("AGENT_MONITOR_GUEST_DERIVED_GLOBAL_CONCURRENCY", raising=False)

    def check(port: int, cookie: str, run: str):
        return request(port, "POST", f"{APP_MOUNT}/api/run/{run}/lean_verify",
                       body={"action": "check", "lean": "theorem t : True := trivial"},
                       headers={"Cookie": cookie})

    try:
        with running_server() as port:
            guests = [_guest_from(port) for _ in range(3)]
            (_, g1, c1), (_, g2, c2), (_, _g3, c3) = guests
            first = check(port, c1, "run1")
            second = check(port, c2, "run2")
            assert _wait_until(lambda: in_flight["now"] == 2)
            # Both site-wide slots are held by the two background model calls.
            third = check(port, c3, "run3")
            # And the first guest is still "busy" on its own refresh.
            again = check(port, c1, "run4")
            assert in_flight["peak"] == 2
            assert console._GUEST_DERIVED_ACTIVE_USERS == {g1["id"], g2["id"]}

            gate.set()
            assert _wait_until(lambda: not console._GUEST_DERIVED_ACTIVE_USERS)
            after = check(port, c3, "run3")
            assert _wait_until(lambda: not console._GUEST_DERIVED_ACTIVE_USERS)
    finally:
        gate.set()

    assert first[0] == 200 and second[0] == 200, (first, second)
    assert third[0] == 429 and "capacity" in json.loads(third[2])["error"]
    assert again[0] == 429 and "current Formal or DAG action" in json.loads(again[2])["error"]
    assert after[0] == 200, after[2]
    assert in_flight["peak"] <= 2 and in_flight["calls"] == 3
    # Refused requests were not counted against the hourly windows.
    assert len(console._GUEST_DERIVED_WINDOWS[g1["id"]]) == 1
    assert len(console._GUEST_DERIVED_GLOBAL_WINDOW) == 3


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_guest_slot_is_released_when_the_background_refresh_fails(
    guest_console, derived_state, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace = tmp_path / "run1"
    workspace.mkdir()
    monkeypatch.setattr(console, "_workspace_for_run", lambda _rid: workspace)
    monkeypatch.setattr(console.Handler, "_owns_run", lambda _s, _r, _u: True)
    monkeypatch.setattr(console.Handler, "_is_run_owner", lambda _s, _r, _u: True)
    monkeypatch.setattr(console, "_run_record_for", lambda rid: {"run_id": rid, "engine": "kimi"})
    monkeypatch.setattr(lean_verify, "compile_or_check", lambda **_kw: {"status": "failed"})

    def broken(**_kwargs):
        raise RuntimeError("model call failed")

    monkeypatch.setattr(auto_pipeline, "_refresh_formal_once", broken)
    with running_server() as port:
        _, guest, cookie = _guest_from(port)
        status, _, raw = request(port, "POST", f"{APP_MOUNT}/api/run/run1/lean_verify",
                                 body={"action": "compile"}, headers={"Cookie": cookie})
        assert status == 200, raw
        assert _wait_until(lambda: guest["id"] not in console._GUEST_DERIVED_ACTIVE_USERS)


def test_refresh_formal_on_exit_waits_for_coalesced_work(tmp_path: Path, monkeypatch):
    gate = threading.Event()
    calls: list[int] = []

    def fake_once(**_kwargs):
        calls.append(1)
        gate.wait(10)

    monkeypatch.setattr(auto_pipeline, "_refresh_formal_once", fake_once)
    released: list[str] = []
    kwargs = dict(run_id="r", workspace=tmp_path, owner_id=1, model=None, engine="kimi")
    try:
        assert auto_pipeline.refresh_formal(**kwargs, on_exit=lambda: released.append("a")) is True
        assert _wait_until(lambda: len(calls) == 1)
        # A second request joins the running worker (and its callback waits
        # for the extra iteration it causes).
        assert auto_pipeline.refresh_formal(**kwargs, on_exit=lambda: released.append("b")) is False
        assert released == []
    finally:
        gate.set()
    key = str(tmp_path.resolve())
    assert _wait_until(lambda: key not in auto_pipeline._FORMAL_REFRESH_ACTIVE and len(released) == 2)
    assert len(calls) == 2
    assert sorted(released) == ["a", "b"]
    assert key not in auto_pipeline._FORMAL_REFRESH_ON_EXIT


# ── GI-3: guest bodies are refused before they are read ───────────────────


def _raw_post(port: int, path: str, headers: dict[str, str], body: bytes = b"") -> tuple[int, str]:
    """Send headers (and an optional partial body), then read the answer."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        head = f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n" + "".join(
            f"{name}: {value}\r\n" for name, value in headers.items()
        ) + "\r\n"
        sock.sendall(head.encode("latin-1") + body)
        response = http.client.HTTPResponse(sock)
        response.begin()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        sock.close()


def test_guest_chat_and_oversized_guest_runs_are_refused_before_the_body_is_read(
    guest_console,
):
    with running_server() as port:
        _, _guest, cookie = _guest_from(port)
        started = time.monotonic()
        # 29 MiB announced, nothing sent: the answer must not wait for it.
        chat = _raw_post(port, f"{APP_MOUNT}/api/runs/anything/chat", {
            "Cookie": cookie, "Content-Type": "application/json",
            "Content-Length": str(29 * 1024 * 1024),
        })
        run = _raw_post(port, f"{APP_MOUNT}/api/runs", {
            "Cookie": cookie, "Content-Type": "application/json",
            "Content-Length": str(console.GUEST_MAX_REQUEST_BYTES + 1),
        })
        elapsed = time.monotonic() - started

    assert chat[0] == 403 and "Sign in" in chat[1]
    assert run[0] == 413
    assert elapsed < 4
    assert console.Handler.timeout and console.Handler.timeout <= 600


# ── GI-4: guest attachments are small text files only ─────────────────────


def _file(name: str, data: bytes) -> dict:
    return {"name": name, "data": base64.b64encode(data).decode("ascii")}


def test_guest_attachment_policy_is_text_only_and_small():
    small = _file("notes.md", b"# Notes\nA lemma.\n")
    assert len(research_attachments.validate([small, small], guest=True)) == 2
    with pytest.raises(ValueError, match="text files only"):
        research_attachments.validate([_file("paper.pdf", b"%PDF-1.4 x")], guest=True)
    with pytest.raises(ValueError, match="text files only"):
        research_attachments.validate([_file("fig.png", b"\x89PNG\r\n\x1a\n...")], guest=True)
    with pytest.raises(ValueError, match="at most 2"):
        research_attachments.validate([small, small, small], guest=True)
    with pytest.raises(ValueError, match="64 KB"):
        research_attachments.validate([_file("big.txt", b"x" * (64 * 1024 + 1))], guest=True)
    with pytest.raises(ValueError, match="64 KB"):
        research_attachments.validate(
            [_file("a.txt", b"x" * 40_000), _file("b.txt", b"y" * 40_000)], guest=True
        )
    # Members keep the documented allowance.
    assert research_attachments.validate([_file("paper.pdf", b"%PDF-1.4 x")])
    policy = research_attachments.guest_policy()
    assert policy["max_files"] == 2 and policy["max_total_bytes"] == 65536
    assert "pdf" not in policy["extensions"] and "md" in policy["extensions"]


def test_guest_run_with_a_disallowed_attachment_is_refused_before_a_slot(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    active = _fake_runs(monkeypatch)
    with running_server() as port:
        _, guest, cookie = _guest_from(port)
        status, _, raw = request(port, "POST", f"{APP_MOUNT}/api/runs", body={
            "engine": "kimi", "problem_text": "Prove it.",
            "attachments": [_file("paper.pdf", b"%PDF-1.4 x")],
        }, headers={"Cookie": cookie})
        me = json.loads(request(port, "GET", f"{APP_MOUNT}/api/auth/me",
                                headers={"Cookie": cookie})[2])
    assert status == 400 and "text files only" in json.loads(raw)["error"]
    assert active == []
    assert not console._GUEST_RUN_WINDOWS.get(guest["id"])
    assert me["guest_policy"]["attachments"] == research_attachments.guest_policy()


def test_start_job_applies_the_guest_attachment_policy(monkeypatch: pytest.MonkeyPatch):
    seen: list[bool] = []

    class Stop(Exception):
        pass

    def spy(items, *, guest=False):
        seen.append(guest)
        raise Stop

    from agent_monitor import engines_registry, sponsored_kimi_client

    plain = {"id": "plain", "label": "Plain", "available": True, "auth_modes": ["api_key"]}
    monkeypatch.setattr(engines_registry, "all_engine_ids", lambda: {"plain"})
    monkeypatch.setattr(engines_registry, "list_engines", lambda: [dict(plain)])
    monkeypatch.setattr(engines_registry, "supported_models", lambda *a, **k: [])
    monkeypatch.setattr(jobs, "_user_extra_env", lambda *a, **k: {})
    monkeypatch.setattr(sponsored_kimi_client, "configured", lambda: True)
    monkeypatch.setattr(sponsored_kimi_client, "endpoint", lambda: "http://127.0.0.1:4610")
    monkeypatch.setattr(jobs.research_attachments, "validate", spy)
    for user, expected in (({"id": 7, "guest": True}, True), ({"id": 8, "guest": False}, False)):
        with pytest.raises(Stop):
            jobs.start_job(
                engine="plain", problem_text="Prove it.", model="kimi-k3",
                user=user, attachments=[], auth_route="api_key",
            )
        assert seen[-1] is expected


# ── GI-4: retention runs inside the console ───────────────────────────────


def test_guest_run_retention_is_opt_in_and_runs_in_process(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[float, bool]] = []
    monkeypatch.setattr(
        console.job_manager,
        "purge_guest_runs",
        lambda *, older_than_days, dry_run=True: calls.append((older_than_days, dry_run)) or ["x"],
    )
    monkeypatch.delenv("AGENT_MONITOR_GUEST_RUN_RETENTION_DAYS", raising=False)
    assert console.guest_run_retention_days() == 0
    assert console._purge_expired_guest_runs() == []
    assert console.start_guest_retention_thread() is None
    monkeypatch.setenv("AGENT_MONITOR_GUEST_RUN_RETENTION_DAYS", "30")
    assert console._purge_expired_guest_runs() == ["x"]
    assert calls == [(30, False)]

    monkeypatch.setattr(console, "_GUEST_RETENTION_FIRST_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(console, "_GUEST_RETENTION_INTERVAL_SECONDS", 0.01)
    stop = console.start_guest_retention_thread()
    try:
        assert stop is not None
        assert _wait_until(lambda: len(calls) >= 3)
    finally:
        stop.set()


# ── WEB-1 / WEB-5: same-origin guard on every POST and DELETE ─────────────


def _post(port: int, path: str, *, body: bytes = b"{}", headers: dict[str, str]) -> tuple[int, dict, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        return (
            response.status,
            {key.lower(): value for key, value in response.getheaders()},
            response.read().decode("utf-8"),
        )
    finally:
        connection.close()


def test_cross_site_auth_posts_cannot_set_or_clear_the_session_cookie(guest_console):
    admin = auth.register("admin@example.test", "long-enough-password")
    token = auth.create_session(admin["id"])
    with running_server() as port:
        host = f"127.0.0.1:{port}"
        # An auto-submitted text/plain form from another site (no cookie).
        forged_guest = _post(port, f"{APP_MOUNT}/api/auth/guest", headers={
            "Content-Type": "text/plain", "Origin": "https://evil.test",
            "Sec-Fetch-Site": "cross-site", "Host": host,
        })
        forged_logout = _post(port, f"{APP_MOUNT}/api/auth/logout", headers={
            "Content-Type": "application/x-www-form-urlencoded", "Origin": "https://evil.test",
            "Host": host, "Cookie": f"{auth.SESSION_COOKIE}={token}",
        })
        # Sec-Fetch-Site alone (a browser that omits Origin) is enough.
        fetch_metadata_only = _post(port, f"{APP_MOUNT}/api/auth/login", body=json.dumps(
            {"email": "admin@example.test", "password": "long-enough-password"}).encode(),
            headers={"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site", "Host": host})
        # A form content type without any browser metadata is still refused.
        form_only = _post(port, f"{APP_MOUNT}/api/auth/guest", headers={
            "Content-Type": "text/plain;charset=UTF-8", "Host": host,
        })
        null_origin = _post(port, f"{APP_MOUNT}/api/auth/guest", headers={
            "Content-Type": "application/json", "Origin": "null", "Host": host,
        })
        still_admin = request(port, "GET", f"{APP_MOUNT}/api/auth/me",
                              headers={"Cookie": f"{auth.SESSION_COOKIE}={token}"})
        # The console's own same-origin requests and non-browser clients work.
        same_origin = _post(port, f"{APP_MOUNT}/api/auth/guest", headers={
            "Content-Type": "application/json", "Origin": f"http://{host}",
            "Sec-Fetch-Site": "same-origin", "Host": host, "X-Forwarded-Proto": "https",
        })
        tool = request(port, "POST", f"{APP_MOUNT}/api/auth/guest", body={})

    for status, headers, raw in (forged_guest, forged_logout, fetch_metadata_only, null_origin):
        assert status == 403, raw
        assert "set-cookie" not in headers
    assert form_only[0] == 415 and "set-cookie" not in form_only[1]
    assert json.loads(still_admin[2])["user"]["email"] == "admin@example.test"
    assert same_origin[0] == 200 and "pc_session=" in same_origin[1]["set-cookie"]
    assert tool[0] == 200
    # Only the two legitimate requests created guest identities.
    assert _database_counts(guest_console["db"])[0] == 3


def test_same_site_pages_cannot_drive_admin_posts_or_deletes(
    guest_console, monkeypatch: pytest.MonkeyPatch
):
    from agent_monitor import settings

    admin = auth.register("admin@example.test", "long-enough-password")
    cookie = f"{auth.SESSION_COOKIE}={auth.create_session(admin['id'])}"
    saved: list[dict] = []
    monkeypatch.setattr(settings, "save_settings", lambda body, user=None: saved.append(body) or {"ok": True})
    stopped: list[str] = []
    monkeypatch.setattr(console.job_manager, "stop_run", lambda rid: stopped.append(rid) or {"ok": True})
    deleted: list[str] = []
    monkeypatch.setattr(console.job_manager, "delete_run", lambda rid: deleted.append(rid) or {"ok": True})
    monkeypatch.setattr(console.Handler, "_owns_run", lambda _s, _r, _u: True)
    with running_server() as port:
        host = f"127.0.0.1:{port}"
        sibling = {"Origin": "https://evil.ansatze.ai", "Sec-Fetch-Site": "same-site",
                   "Host": host, "Cookie": cookie}
        forged_settings = _post(port, f"{APP_MOUNT}/api/settings",
                                body=b'{"max_iterations": 1}',
                                headers={**sibling, "Content-Type": "text/plain"})
        forged_json = _post(port, f"{APP_MOUNT}/api/settings", body=b'{"max_iterations": 1}',
                            headers={**sibling, "Content-Type": "application/json"})
        forged_stop = _post(port, f"{APP_MOUNT}/api/runs/r1/stop", headers={**sibling})
        forged_delete = request(port, "DELETE", f"{APP_MOUNT}/api/runs/r1", headers=sibling)
        own = request(port, "POST", f"{APP_MOUNT}/api/settings", body={"max_iterations": 2},
                      headers={"Cookie": cookie, "Origin": f"http://{host}", "Host": host})
        own_delete = request(port, "DELETE", f"{APP_MOUNT}/api/runs/r1",
                             headers={"Cookie": cookie, "Origin": f"http://{host}", "Host": host})

    assert forged_settings[0] == 403 and forged_json[0] == 403
    assert forged_stop[0] == 403 and forged_delete[0] == 403
    assert own[0] == 200 and own_delete[0] == 200
    assert saved == [{"max_iterations": 2}]
    assert stopped == [] and deleted == ["r1"]


def test_cross_origin_post_is_refused_before_its_body_is_read(guest_console):
    with running_server() as port:
        started = time.monotonic()
        status, _ = _raw_post(port, f"{APP_MOUNT}/api/auth/login", {
            "Origin": "https://evil.test", "Content-Type": "application/json",
            "Content-Length": "900000",
        })
    assert status == 403
    assert time.monotonic() - started < 4


# ── WEB-2: /guest cannot sign an account out on page load ─────────────────


def test_guest_intent_never_switches_a_signed_in_account_without_a_click():
    root = Path(console.__file__).resolve().parent
    login = (root / "web" / "login.html").read_text(encoding="utf-8")
    boot = login[login.index("setAuthPending(true);\n(async()=>{"):]
    # On load, a signed-in account only gets the confirmation; the switch
    # itself is bound to the guest button's click handler.
    assert "switchAccount:true" not in boot
    assert "confirmGuestSwitch()" in boot
    confirm = login[login.index("function confirmGuestSwitch(){"):login.index("function takeGuestIntent(){")]
    assert "$('guest-button').onclick=()=>startGuest({switchAccount:true});" in confirm
    assert "startGuest(" not in confirm.replace(
        "$('guest-button').onclick=()=>startGuest({switchAccount:true});", ""
    )
    assert "textContent" in confirm and "innerHTML" not in confirm
