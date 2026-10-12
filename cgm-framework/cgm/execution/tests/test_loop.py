"""Offline tests for cgm.execution.loop + __main__ (no real codex, no network).

Covers the round driver end-to-end without ever launching a real codex:

  - ``run_round`` against a FIXED fake-codex stub script: a chosen exit code, a
    hard-timeout (terminate → 124), and a missing binary (→ 127). These drive the
    real ``subprocess.Popen`` path in loop.py.
  - the ``main`` outer loop: stop-flag / deadline / max-rounds / consecutive-
    failure caps, the codex-missing (127) short-circuit, and the ``ok``/``error``
    status writes. ``run_round`` is monkeypatched so no subprocess spawns.
  - the SIGTERM handler (_on_term): terminates the in-flight child, writes
    ``terminated`` status, and exits 0.
  - __main__: ``runpy.run_module("cgm.execution", run_name="__main__")`` with the
    loop entry patched, covering the argv guard + dispatch without spawning.
  - the remaining small error/edge branches in loop / layout / scaffold helpers.

Runs standalone (``python -m cgm.execution.tests.test_loop``) and pytest.
"""

from __future__ import annotations

import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from cgm.execution import layout as L
from cgm.execution import loop, scaffold

# The idle backoff (loop.py) would put REAL sleeps between the instant, fact-less
# rounds most tests here drive. Off by default for this module; the backoff tests
# below switch it on explicitly (with time.sleep stubbed).
os.environ.setdefault("CGM_IDLE_BACKOFF_BASE", "0")


@contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _restore_sigterm():
    """main() installs a SIGTERM handler; save/restore so tests don't leak it."""
    old = signal.getsignal(signal.SIGTERM)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def _mk_worker(tmp: Path, name: str = "high") -> L.WorkerLayout:
    """A minimal worker home under tmp: <tmp>/proj/workers/<name>."""
    wl = L.WorkerLayout(tmp / "proj" / "workers" / name)
    wl.dir.mkdir(parents=True)
    return wl


def _write_fake_codex(tmp: Path, body: str) -> Path:
    """Write an executable python fake-codex stub and return its path. The stub
    ignores all the exec args and just does what ``body`` says."""
    p = tmp / "fake_codex"
    p.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return p


# --- run_round: chosen exit code ------------------------------------------- #

def test_run_round_returns_codex_rc(tmp: Path):
    wl = _mk_worker(tmp)
    fake = _write_fake_codex(tmp, "import sys\nsys.stdout.write('hello from codex\\n')\nsys.exit(3)\n")
    log = wl.dir / "round.log"
    with _env(CGM_CODEX_BIN=str(fake)):
        rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                            "prompt", log, hard_timeout=30)
    assert rc == 3
    assert "hello from codex" in log.read_text()
    assert loop._Child.proc is None            # cleared in finally


def test_run_round_success_rc0(tmp: Path):
    wl = _mk_worker(tmp)
    fake = _write_fake_codex(tmp, "import sys\nsys.exit(0)\n")
    log = wl.dir / "round.log"
    with _env(CGM_CODEX_BIN=str(fake)):
        rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                            "prompt", log, hard_timeout=0)   # 0 => no timeout (wait forever)
    assert rc == 0


# --- run_round: hard timeout → terminate → 124 ----------------------------- #

def test_run_round_hard_timeout_terminates(tmp: Path):
    wl = _mk_worker(tmp)
    # sleeps far past the tiny hard_timeout; a plain terminate() ends it.
    fake = _write_fake_codex(tmp, "import time\ntime.sleep(60)\n")
    log = wl.dir / "round.log"
    with _env(CGM_CODEX_BIN=str(fake)):
        rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                            "prompt", log, hard_timeout=1)
    assert rc == 124
    assert "hard-timeout after 1s" in log.read_text()
    assert loop._Child.proc is None


# --- run_round: missing binary → 127 --------------------------------------- #

def test_run_round_missing_binary_returns_127(tmp: Path):
    wl = _mk_worker(tmp)
    missing = tmp / "does_not_exist_codex"
    log = wl.dir / "round.log"
    with _env(CGM_CODEX_BIN=str(missing)):
        rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                            "prompt", log, hard_timeout=30)
    assert rc == 127
    assert "codex binary not found" in log.read_text()


# --- run_round: unresponsive child → terminate times out → kill → 124 ------ #

def test_run_round_timeout_then_kill(tmp: Path):
    """A child that ignores terminate() (wait(10) times out) is force-killed. We
    fake Popen so the 10s terminate-grace does not slow the test."""
    wl = _mk_worker(tmp)
    log = wl.dir / "round.log"

    class _StubProc:
        def __init__(self):
            self.terminated = False
            self.killed = False
            self._waits = 0

        def wait(self, timeout=None):
            self._waits += 1
            # 1st wait = the hard-timeout expiry; 2nd wait = the 10s grace expiry.
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout)

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    stub = _StubProc()
    orig_popen = subprocess.Popen
    subprocess.Popen = lambda *a, **k: stub
    try:
        with _env(CGM_CODEX_BIN=str(tmp / "anything")):
            rc = loop.run_round(wl, {"MODEL": "m", "REASONING_EFFORT": "high"},
                                "prompt", log, hard_timeout=1)
    finally:
        subprocess.Popen = orig_popen
    assert rc == 124
    assert stub.terminated and stub.killed          # terminate → (grace expired) → kill
    assert loop._Child.proc is None


# --- main loop: stop flag → graceful stop ---------------------------------- #

def test_main_stops_on_stop_flag(tmp: Path):
    wl = _mk_worker(tmp)
    wl.stop.touch()          # stop before the first round
    with _restore_sigterm(), _env(CGM_ROUND_BEAT="0"):
        _patch_run_round(lambda *a, **k: 0)
        try:
            rc = loop.main(str(wl.dir))
        finally:
            _unpatch_run_round()
    assert rc == 0
    assert not wl.stop.exists()                       # consumed
    assert json.loads(wl.status.read_text())["state"] == "stopped"


# --- main loop: deadline → stop -------------------------------------------- #

def test_main_stops_on_deadline(tmp: Path):
    wl = _mk_worker(tmp)
    (wl.project_dir / L.DEADLINE_FILE).write_text("1")   # epoch 1 = long past
    with _restore_sigterm(), _env(CGM_ROUND_BEAT="0"):
        _patch_run_round(lambda *a, **k: 0)
        try:
            rc = loop.main(str(wl.dir))
        finally:
            _unpatch_run_round()
    assert rc == 0
    assert json.loads(wl.status.read_text())["state"] == "deadline"


# --- main loop: max-rounds cap --------------------------------------------- #

def test_main_max_rounds_cap(tmp: Path):
    wl = _mk_worker(tmp)
    calls = []
    with _restore_sigterm(), _env(CGM_ROUND_BEAT="0", CGM_MAX_ROUNDS="2",
                                  CGM_MAX_CONSEC_FAILURES="0"):
        _patch_run_round(lambda *a, **k: (calls.append(1) or 0))
        try:
            rc = loop.main(str(wl.dir))
        finally:
            _unpatch_run_round()
    assert rc == 0
    assert len(calls) == 2                              # exactly max_rounds rounds ran
    st = json.loads(wl.status.read_text())
    assert st["state"] == "max_rounds"
    assert st["round"] == 2 and st["last_rc"] == 0


# --- main loop: consecutive-failure cap → error / rc 1 --------------------- #

def test_main_consecutive_failure_cap(tmp: Path):
    wl = _mk_worker(tmp)
    log = wl.logs / "round_1.log"   # a fact id in a round log flows into status
    with _restore_sigterm(), _env(CGM_ROUND_BEAT="0", CGM_MAX_CONSEC_FAILURES="2",
                                  CGM_MAX_ROUNDS="0"):
        def _fail(w, role, prompt, log_path, ht):
            log_path.write_text('"fact_id": "0123456789abcdef"\n')
            return 5                                    # a failing rc (not 0/124)
        _patch_run_round(_fail)
        try:
            rc = loop.main(str(wl.dir))
        finally:
            _unpatch_run_round()
    assert rc == 1
    st = json.loads(wl.status.read_text())
    assert st["state"] == "error" and "consecutive failed rounds" in st["error"]
    # last idle status carried the parsed fact id
    assert st.get("last_fact_id") == "0123456789abcdef" or st["last_rc"] == 5


def test_main_timeout_rc124_does_not_count_as_failure(tmp: Path):
    """rc 124 (hard-timeout) resets the consecutive-failure counter, so a run of
    124s never trips the failure cap — it must stop via max_rounds instead."""
    wl = _mk_worker(tmp)
    with _restore_sigterm(), _env(CGM_ROUND_BEAT="0", CGM_MAX_CONSEC_FAILURES="2",
                                  CGM_MAX_ROUNDS="3"):
        _patch_run_round(lambda *a, **k: 124)
        try:
            rc = loop.main(str(wl.dir))
        finally:
            _unpatch_run_round()
    assert rc == 0
    assert json.loads(wl.status.read_text())["state"] == "max_rounds"


# --- main loop: the timeout streak + progress clock ------------------------ #

def test_main_timeout_streak_accumulates_and_resets(tmp: Path):
    """timeout_streak counts CONSECUTIVE 124s and is cleared by any other rc — it
    is what `cgm status` ages into the 'timeouts?' label."""
    wl = _mk_worker(tmp)
    seen = []
    rcs = iter([124, 124, 0, 124])
    with _restore_sigterm(), _env(CGM_ROUND_BEAT="0", CGM_MAX_ROUNDS="4",
                                  CGM_MAX_CONSEC_FAILURES="0"):
        def _round(w, role, prompt, log_path, ht):
            log_path.write_text("")
            rc = next(rcs)
            return rc
        _patch_run_round(_round)
        try:
            orig = loop.write_status

            def _spy(w, **f):
                if "timeout_streak" in f and f.get("state") == "idle":
                    seen.append(f["timeout_streak"])
                return orig(w, **f)
            loop.write_status = _spy
            try:
                rc = loop.main(str(wl.dir))
            finally:
                loop.write_status = orig
        finally:
            _unpatch_run_round()
    assert rc == 0
    assert seen == [1, 2, 0, 1]                       # reset by the clean round
    assert json.loads(wl.status.read_text())["timeout_streak"] == 1


def test_main_max_consec_timeouts_is_opt_in(tmp: Path):
    """Default 0 => a run of 124s never bails (a slow but productive worker must
    not be killed for being slow); set it and the loop stops with an error."""
    wl = _mk_worker(tmp)
    with _restore_sigterm(), _env(CGM_ROUND_BEAT="0", CGM_MAX_ROUNDS="3",
                                  CGM_MAX_CONSEC_FAILURES="0"):
        _patch_run_round(lambda *a, **k: 124)          # CGM_MAX_CONSEC_TIMEOUTS unset
        try:
            assert loop.main(str(wl.dir)) == 0
        finally:
            _unpatch_run_round()
    assert json.loads(wl.status.read_text())["state"] == "max_rounds"

    wl2 = _mk_worker(tmp, name="w2")
    with _restore_sigterm(), _env(CGM_ROUND_BEAT="0", CGM_MAX_ROUNDS="0",
                                  CGM_MAX_CONSEC_FAILURES="0",
                                  CGM_MAX_CONSEC_TIMEOUTS="2"):
        _patch_run_round(lambda *a, **k: 124)
        try:
            assert loop.main(str(wl2.dir)) == 1
        finally:
            _unpatch_run_round()
    st = json.loads(wl2.status.read_text())
    assert st["state"] == "error" and "consecutive round timeouts" in st["error"]


def test_main_last_fact_at_only_moves_on_a_new_fact(tmp: Path):
    """The progress clock advances when a round banks a NEW fact id and stands
    still otherwise — that is what separates 'slow but productive' from 'wedged'.
    It is also seeded from the previous status, so a restart does not fake
    freshness."""
    wl = _mk_worker(tmp)
    wl.status.write_text(json.dumps({"last_fact_id": "0123456789abcdef",
                                     "last_fact_at": 1000.0}))
    stamps = []
    ids = iter(["0123456789abcdef", None, "fedcba9876543210"])
    with _restore_sigterm(), _env(CGM_ROUND_BEAT="0", CGM_MAX_ROUNDS="3",
                                  CGM_MAX_CONSEC_FAILURES="0"):
        def _round(w, role, prompt, log_path, ht):
            fid = next(ids)
            log_path.write_text(f'"fact_id": "{fid}"\n' if fid else "no facts here\n")
            return 0
        _patch_run_round(_round)
        try:
            orig = loop.write_status

            def _spy(w, **f):
                if f.get("state") == "idle":
                    stamps.append(f.get("last_fact_at"))
                return orig(w, **f)
            loop.write_status = _spy
            try:
                loop.main(str(wl.dir))
            finally:
                loop.write_status = orig
        finally:
            _unpatch_run_round()
    # round 1 re-sees the SAME id as the seeded status, round 2 banks nothing:
    # the clock keeps the seeded value. Round 3's new id moves it.
    assert stamps[0] == 1000.0 and stamps[1] == 1000.0
    assert stamps[2] is not None and stamps[2] > 1000.0


# --- main loop: codex missing (127) short-circuits ------------------------- #

def test_main_codex_missing_127(tmp: Path):
    wl = _mk_worker(tmp)
    with _restore_sigterm(), _env(CGM_ROUND_BEAT="0"):
        _patch_run_round(lambda *a, **k: 127)
        try:
            rc = loop.main(str(wl.dir))
        finally:
            _unpatch_run_round()
    assert rc == 127
    st = json.loads(wl.status.read_text())
    assert st["state"] == "error" and st["error"] == "codex binary not found"


# --- main: bad worker dir → rc 2 ------------------------------------------- #

def test_main_missing_worker_dir(tmp: Path):
    rc = loop.main(str(tmp / "nope"))
    assert rc == 2


# --- SIGTERM handler: terminate child, write terminated, exit 0 ------------ #

def test_main_sigterm_handler(tmp: Path):
    wl = _mk_worker(tmp)

    class _FakeProc:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    fake_proc = _FakeProc()

    # run_round: install a live child then deliver SIGTERM to ourselves so the
    # loop's own handler fires (covers _on_term end to end).
    def _round(w, role, prompt, log_path, ht):
        loop._Child.proc = fake_proc
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(2)                     # give the signal time to be delivered
        return 0

    with _restore_sigterm(), _env(CGM_ROUND_BEAT="0"):
        _patch_run_round(_round)
        try:
            try:
                loop.main(str(wl.dir))
                assert False, "handler should sys.exit(0)"
            except SystemExit as e:
                assert e.code == 0
        finally:
            _unpatch_run_round()
            loop._Child.proc = None
    assert fake_proc.terminated
    assert json.loads(wl.status.read_text())["state"] == "terminated"


# --- write_status: recovers from a corrupt existing status ----------------- #

def test_write_status_corrupt_existing_recovers(tmp: Path):
    wl = _mk_worker(tmp)
    wl.status.write_text("{not json")            # corrupt → JSONDecodeError branch
    loop.write_status(wl, state="running")
    st = json.loads(wl.status.read_text())
    assert st["state"] == "running" and st["worker"] == "high"


# --- _parse_last_fact_id: unreadable path → None --------------------------- #

def test_parse_last_fact_id_missing_file(tmp: Path):
    assert loop._parse_last_fact_id(tmp / "no_such.log") is None   # OSError branch


# --- _cleanup_pid: only removes a .pid that points at us ------------------- #

def test_cleanup_pid_removes_own(tmp: Path):
    wl = _mk_worker(tmp)
    wl.pid.write_text(str(os.getpid()))
    loop._cleanup_pid(wl)
    assert not wl.pid.exists()


def test_cleanup_pid_keeps_foreign(tmp: Path):
    wl = _mk_worker(tmp)
    wl.pid.write_text("999999999")            # some other pid
    loop._cleanup_pid(wl)
    assert wl.pid.exists()                     # left intact


def test_cleanup_pid_swallows_oserror(tmp: Path):
    """A .pid that cannot be read (here: it is a directory) → OSError swallowed."""
    wl = _mk_worker(tmp)
    wl.pid.mkdir()                             # read_text on a dir raises OSError
    loop._cleanup_pid(wl)                      # must not raise
    assert wl.pid.exists()


# --- main loop: positive beat sleeps between rounds ------------------------ #

def test_main_beat_sleep_between_rounds(tmp: Path):
    """A positive CGM_ROUND_BEAT makes the loop sleep between rounds; we stub
    time.sleep so no real wall-clock time passes and record it fired."""
    wl = _mk_worker(tmp)
    slept = []
    orig_sleep = time.sleep

    def _one_then_stop(*a, **k):
        wl.stop.touch()          # stop after the first round completes
        return 0

    time.sleep = lambda s: slept.append(s)
    try:
        with _restore_sigterm(), _env(CGM_ROUND_BEAT="7", CGM_MAX_ROUNDS="0",
                                      CGM_MAX_CONSEC_FAILURES="0"):
            _patch_run_round(_one_then_stop)
            try:
                rc = loop.main(str(wl.dir))
            finally:
                _unpatch_run_round()
    finally:
        time.sleep = orig_sleep
    assert rc == 0
    assert 7 in slept                          # the beat sleep fired once


# --- kickoff prompt -------------------------------------------------------- #

def test_kickoff_mentions_worker_and_project():
    p = loop.kickoff("ProjX", "wkrY")
    assert "wkrY" in p and "ProjX" in p and "TASK.md" in p


# --- __main__ entry -------------------------------------------------------- #

def test_dunder_main_dispatches(tmp: Path):
    """runpy the package as __main__ with the loop entry patched: the guard runs
    and dispatches to main() without spawning anything."""
    seen = {}

    def _fake_main(arg):
        seen["arg"] = arg
        return 0

    orig = loop.main
    loop.main = _fake_main
    argv = sys.argv
    sys.argv = ["prog", "/some/worker/dir"]
    try:
        try:
            runpy.run_module("cgm.execution", run_name="__main__")
            assert False, "should sys.exit"
        except SystemExit as e:
            assert e.code == 0
    finally:
        loop.main = orig
        sys.argv = argv
    assert seen["arg"] == "/some/worker/dir"


def test_dunder_main_usage_guard():
    """Wrong argc → usage message + exit 2 (no dispatch)."""
    argv = sys.argv
    sys.argv = ["prog"]                        # missing worker_dir
    try:
        try:
            runpy.run_module("cgm.execution", run_name="__main__")
            assert False, "should sys.exit(2)"
        except SystemExit as e:
            assert e.code == 2
    finally:
        sys.argv = argv


# --- layout defaults (no env overrides) ------------------------------------ #

def test_layout_defaults_and_empties(tmp: Path):
    with _env(CGM_WORKER_CONTRACT=None, CGM_WORKER_SKILLS=None,
              CGM_AGENTS_ROOT=None):
        # repo_root / worker_md / worker_skills_dir defaults
        rr = L.repo_root()
        assert L.worker_md() == rr / "agents" / "contracts" / "worker.md"
        assert L.worker_skills_dir() == rr / "agents" / "skills" / "worker"
        # agents_root default = <cwd>/runtime/projects
        assert L.agents_root() == (Path.cwd() / "runtime" / "projects").resolve()
    # list_workers / list_projects on a nonexistent root → []
    with _env(CGM_AGENTS_ROOT=str(tmp / "no_such_root")):
        assert L.list_workers("ghost") == []
        assert L.list_projects() == []


# --- scaffold.symlink branches --------------------------------------------- #

def test_symlink_skips_existing(tmp: Path):
    target = tmp / "target"
    target.write_text("x")
    link = tmp / "link"
    link.write_text("already here")            # link path exists → early return
    scaffold.symlink(target, link)
    assert link.read_text() == "already here"  # untouched


def test_symlink_swallows_oserror(tmp: Path):
    target = tmp / "target"
    target.write_text("x")
    # a link path whose parent does not exist → os.symlink raises OSError, swallowed
    link = tmp / "no_parent_dir" / "link"
    scaffold.symlink(target, link)             # must not raise
    assert not link.exists()


# --- runner ---------------------------------------------------------------- #

# run_round monkeypatch helpers (so the standalone runner works without pytest's
# monkeypatch fixture): swap loop.run_round for the duration of a test.
_ORIG_RUN_ROUND = loop.run_round


def _patch_run_round(fn):
    loop.run_round = fn


def _unpatch_run_round():
    loop.run_round = _ORIG_RUN_ROUND


# --- idle backoff + lane-complete parking --------------------------------- #

def _stub_sleep(record, on_call=None):
    """A time.sleep replacement: records each requested duration; ``on_call(n)``
    runs after the n-th call (to flip a flag mid-wait)."""
    n = [0]

    def _sleep(sec):
        record.append(sec)
        n[0] += 1
        if on_call:
            on_call(n[0])
    return _sleep


def _bump_task(wl: L.WorkerLayout, text: str = "a new assignment\n") -> None:
    """Write TASK.md and force a strictly newer mtime (coarse filesystems)."""
    wl.task.write_text(text)
    st = wl.task.stat()
    os.utime(wl.task, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


_BACKOFF_ENV = dict(CGM_ROUND_BEAT="0", CGM_MAX_CONSEC_FAILURES="0",
                    CGM_IDLE_BACKOFF_BASE="60", CGM_IDLE_BACKOFF_MAX="1800",
                    CGM_IDLE_ROUND_SECS="600")


def test_main_idle_backoff_grows_then_exits_without_waiting(tmp: Path):
    """Consecutive idle rounds (rc 0, no fact, no submit attempt, same TASK.md)
    sleep 60 s after the 2nd, 120 s after the 3rd; the round cap exits with no
    wait. The wait is sliced (5 s) and summed from requested durations."""
    wl = _mk_worker(tmp)
    wl.task.write_text("lane\n")
    slept = []
    orig = time.sleep
    time.sleep = _stub_sleep(slept)
    try:
        with _restore_sigterm(), _env(CGM_MAX_ROUNDS="4", **_BACKOFF_ENV):
            def _idle(w, r, p, log_path, ht):
                log_path.write_text("nothing to do\n")
                return 0
            _patch_run_round(_idle)
            try:
                rc = loop.main(str(wl.dir))
            finally:
                _unpatch_run_round()
    finally:
        time.sleep = orig
    assert rc == 0
    assert sum(slept) == 180 and max(slept) == 5
    st = json.loads(wl.status.read_text())
    assert st["state"] == "max_rounds" and st["idle_streak"] == 4


def test_main_idle_streak_resets_on_submit_attempt_or_new_fact(tmp: Path):
    """A submit attempt (even rejected) or a NEW fact id is real work: it resets
    the streak, so no backoff follows it."""
    wl = _mk_worker(tmp)
    wl.task.write_text("lane\n")
    logs = iter([
        "idle\n", "idle\n",                                  # streak 1, 2 -> wait 60
        "mcp: cgm/fact_submit started\nrejected\n",       # attempt -> reset
        "idle\n", "idle\n",                                  # streak 1, 2 -> wait 60
        '"fact_id": "0123456789abcdef"\n',                   # new fact -> reset
        "idle\n",                                            # streak 1, then round cap
    ])
    slept = []
    orig = time.sleep
    time.sleep = _stub_sleep(slept)
    try:
        with _restore_sigterm(), _env(CGM_MAX_ROUNDS="7", **_BACKOFF_ENV):
            def _scripted(w, r, p, log_path, ht):
                log_path.write_text(next(logs))
                return 0
            _patch_run_round(_scripted)
            try:
                rc = loop.main(str(wl.dir))
            finally:
                _unpatch_run_round()
    finally:
        time.sleep = orig
    assert rc == 0 and sum(slept) == 120
    assert json.loads(wl.status.read_text())["idle_streak"] == 1


def test_main_idle_backoff_wakes_on_new_task_and_on_stop(tmp: Path):
    """A backoff wait ends within one slice when TASK.md changes (re-task) — the
    streak resets and the next round runs at once — and likewise on .stop."""
    wl = _mk_worker(tmp)
    wl.task.write_text("lane\n")
    rounds = []

    def _on_call(n):
        if n == 1:
            _bump_task(wl)          # during the first wait: re-tasked
        elif n == 2:
            wl.stop.touch()         # during the second wait: stop
    slept = []
    orig = time.sleep
    time.sleep = _stub_sleep(slept, _on_call)
    try:
        with _restore_sigterm(), _env(CGM_MAX_ROUNDS="0", **_BACKOFF_ENV):
            def _idle(w, r, p, log_path, ht):
                rounds.append(1)
                log_path.write_text("idle\n")
                return 0
            _patch_run_round(_idle)
            try:
                rc = loop.main(str(wl.dir))
            finally:
                _unpatch_run_round()
    finally:
        time.sleep = orig
    # r1, r2 (streak 2) -> wait: slice 1 re-tasks -> r3, r4 (streak 2) -> wait: slice 2 stops
    assert rc == 0 and slept == [5, 5] and len(rounds) == 4
    assert json.loads(wl.status.read_text())["state"] == "stopped"


def test_main_lane_complete_parks_until_reassigned(tmp: Path):
    """With .lane_complete present the loop runs NO round; a new TASK.md retires
    the marker and work resumes immediately."""
    wl = _mk_worker(tmp)
    wl.task.write_text("bounded lane\n")
    wl.lane_complete.write_text("delivered\n")
    rounds = []
    states = []

    def _round(w, role, prompt, log_path, ht):
        rounds.append(1)
        log_path.write_text("working again\n")
        wl.stop.touch()
        return 0
    slept = []
    orig_sleep, orig_ws = time.sleep, loop.write_status
    time.sleep = _stub_sleep(slept, lambda n: _bump_task(wl) if n == 3 else None)
    loop.write_status = lambda w, **f: (states.append(f.get("state")), orig_ws(w, **f))[1]
    try:
        with _restore_sigterm(), _env(CGM_ROUND_BEAT="0", CGM_MAX_ROUNDS="0",
                                      CGM_MAX_CONSEC_FAILURES="0"):
            _patch_run_round(_round)
            try:
                rc = loop.main(str(wl.dir))
            finally:
                _unpatch_run_round()
    finally:
        time.sleep, loop.write_status = orig_sleep, orig_ws
    assert rc == 0 and slept == [5, 5, 5] and len(rounds) == 1
    assert "parked" in states and not wl.lane_complete.exists()
    assert json.loads(wl.status.read_text())["state"] == "stopped"


def test_main_lane_complete_parked_then_stopped_keeps_marker(tmp: Path):
    """Parked + .stop: the loop stops without ever running a round, and the
    worker's declaration stays on disk (it is the worker's, not a command)."""
    wl = _mk_worker(tmp)
    wl.task.write_text("bounded lane\n")
    wl.lane_complete.touch()
    rounds = []
    slept = []
    orig = time.sleep
    time.sleep = _stub_sleep(slept, lambda n: wl.stop.touch() if n == 2 else None)
    try:
        with _restore_sigterm(), _env(CGM_ROUND_BEAT="0"):
            _patch_run_round(lambda *a, **k: rounds.append(1) or 0)
            try:
                rc = loop.main(str(wl.dir))
            finally:
                _unpatch_run_round()
    finally:
        time.sleep = orig
    assert rc == 0 and rounds == [] and slept == [5, 5]
    assert wl.lane_complete.exists()
    assert json.loads(wl.status.read_text())["state"] == "stopped"


def test_wait_reasons(tmp: Path):
    wl = _mk_worker(tmp)
    orig = time.sleep
    slept = []
    time.sleep = _stub_sleep(slept)
    try:
        assert loop._wait(wl, wl.project_dir, 7, loop._task_mtime(wl)) == "elapsed"
        assert slept == [5, 2]
        (wl.project_dir / L.DEADLINE_FILE).write_text("1")
        assert loop._wait(wl, wl.project_dir, 60, loop._task_mtime(wl)) == "deadline"
        (wl.project_dir / L.DEADLINE_FILE).unlink()
        wl.stop.touch()
        assert loop._wait(wl, wl.project_dir, None, loop._task_mtime(wl)) == "stop"
    finally:
        time.sleep = orig


def test_round_attempted_submit_ignores_prompt_echo(tmp: Path):
    log = tmp / "r.log"
    log.write_text("any verified result via fact_submit.\n")   # the kickoff prompt echo
    assert loop._round_attempted_submit(log) is False
    log.write_text("mcp: cgm/fact_submit started\n")
    assert loop._round_attempted_submit(log) is True
    assert loop._round_attempted_submit(tmp / "missing.log") is False


_NO_TMP = {test_kickoff_mentions_worker_and_project, test_dunder_main_usage_guard}


def main() -> None:
    tests = [
        test_run_round_returns_codex_rc,
        test_run_round_success_rc0,
        test_run_round_hard_timeout_terminates,
        test_run_round_missing_binary_returns_127,
        test_run_round_timeout_then_kill,
        test_main_stops_on_stop_flag,
        test_main_stops_on_deadline,
        test_main_max_rounds_cap,
        test_main_consecutive_failure_cap,
        test_main_timeout_rc124_does_not_count_as_failure,
        test_main_timeout_streak_accumulates_and_resets,
        test_main_max_consec_timeouts_is_opt_in,
        test_main_last_fact_at_only_moves_on_a_new_fact,
        test_main_codex_missing_127,
        test_main_missing_worker_dir,
        test_main_sigterm_handler,
        test_write_status_corrupt_existing_recovers,
        test_parse_last_fact_id_missing_file,
        test_cleanup_pid_removes_own,
        test_cleanup_pid_keeps_foreign,
        test_cleanup_pid_swallows_oserror,
        test_main_beat_sleep_between_rounds,
        test_kickoff_mentions_worker_and_project,
        test_dunder_main_dispatches,
        test_dunder_main_usage_guard,
        test_layout_defaults_and_empties,
        test_symlink_skips_existing,
        test_symlink_swallows_oserror,
        test_main_idle_backoff_grows_then_exits_without_waiting,
        test_main_idle_streak_resets_on_submit_attempt_or_new_fact,
        test_main_idle_backoff_wakes_on_new_task_and_on_stop,
        test_main_lane_complete_parks_until_reassigned,
        test_main_lane_complete_parked_then_stopped_keeps_marker,
        test_wait_reasons,
        test_round_attempted_submit_ignores_prompt_echo,
    ]
    for t in tests:
        if t in _NO_TMP:
            t()
        else:
            with tempfile.TemporaryDirectory() as d:
                t(Path(d))
        print(f"  [ok] {t.__name__}")
    print("ALL LOOP TESTS PASSED")


if __name__ == "__main__":
    main()
