"""The per-worker autonomous outer loop — the round driver.

Launched detached by ``cgm start`` (``python -m cgm.execution <worker_dir>``).
Self-contained. Each round runs ONE ``codex exec`` session
whose internal control loop (worker.md + the worker skills) drives toward a full
verified result — a round is *continue solving from persisted memory*, NOT one
increment. The round ends when codex's session ends (its stopping rule, the
per-round hard timeout, or it bails); the loop then relaunches a fresh session
that resumes from memory. Stops on the ``.stop`` flag (graceful, at a round
boundary), the project deadline, or a round backstop.

Config:
  - codex binary resolved via the shared ``cgm.codex`` launcher
    (``CGM_CODEX_BIN`` / ``CODEX_BIN`` alias / PATH);
  - all config read at CALL time from env (matches core/gateway/verify).

Env (all optional; tests inject these):
  CGM_CODEX_BIN            codex binary (default "codex")
  CGM_ROUND_BEAT           seconds to sleep between rounds (default 5)
  CGM_ROUND_HARD_TIMEOUT   per-round hard timeout, seconds (default 14400 = 4h)
  CGM_MAX_ROUNDS           round backstop, 0 = unlimited (default 0)
  CGM_MAX_CONSEC_FAILURES  bail after this many consecutive failed rounds (default 5)
  CGM_MAX_CONSEC_TIMEOUTS  bail after this many consecutive HARD-TIMEOUT rounds
                             (default 0 = never; see below)
  CGM_IDLE_ROUND_SECS      a round shorter than this that exits 0 without even
                             attempting a fact_submit and without banking a new
                             fact counts as IDLE (default 600)
  CGM_IDLE_BACKOFF_BASE    from the 2nd consecutive idle round on, sleep
                             base·2^(k-2) seconds instead of the beat, capped at
                             CGM_IDLE_BACKOFF_MAX (defaults 60 / 1800; base 0
                             disables the backoff)

Idle backoff + parking. A worker whose TASK.md deliverable is done (or that finds
nothing to do) otherwise burns a full context re-read every ``beat`` seconds for
nothing (observed: 167 one-minute rounds, ~7M tokens, 2 facts). Two guards, both
purely delaying — neither ever marks a worker failed:
  - backoff: consecutive idle rounds (see the env above) grow the inter-round
    sleep geometrically; anything productive (a new fact, a submit attempt, a
    non-zero rc, a long round) or a new TASK.md resets it;
  - parking: a worker that declares its bounded lane delivered creates the
    ``.lane_complete`` marker (worker.md); the loop then runs no rounds at all
    until TASK.md changes (``cgm assign`` also clears the marker).
Every wait sleeps in short slices and wakes early on ``.stop``, the project
deadline, or a new TASK.md, so a re-task takes effect within seconds.

A timed-out round (rc 124) is deliberately NOT a failed round: the worker
persists facts as it goes, so a round killed at the hard timeout has usually
still banked verified work. That exemption leaves one blind spot — a codex that
HANGS instead of exiting burns a full hard-timeout every round forever without
ever tripping the failure cap. So each round also records ``timeout_streak`` (the
consecutive-124 count) and ``last_fact_at`` (when a NEW fact id last appeared in
a round log) into ``.status.json``; ``cgm status`` turns "still timing out AND
no new fact in a long while" into the ``timeouts?`` label that the watchdog
alarms on. Acting on it stays opt-in via ``CGM_MAX_CONSEC_TIMEOUTS`` — a slow
but productive worker must not be killed for being slow.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import layout as L
from cgm import codex

_FACT_ID_RE = re.compile(r'"?fact_id"?\s*[:=]\s*"?([0-9a-f]{16})"?')
# the gateway tool-call line codex prints when a round actually tries to submit
# ("mcp: cgm/fact_submit started"); the kickoff prompt only says "fact_submit"
_SUBMIT_RE = re.compile(r"\bcgm/fact_submit\b")
_WAIT_SLICE = 5.0


# --- the per-round prompt (continuation semantics; see worker.md) ----------- #

def kickoff(project: str, worker: str) -> str:
    return (
        f"You are worker '{worker}' on project '{project}'. Continue solving the "
        f"problem (this is a continuation round, not a fresh start).\n"
        f"1. Read TASK.md — your current assignment (which direction/subgoal is yours).\n"
        f"2. Follow AGENTS.md (worker.md) exactly — your standing contract (the adaptive "
        f"control loop, memory discipline, the fact_submit gate). Drive toward a full "
        f"verified result.\n"
        f"3. Resume from state: gm_search relevant findings + dead ends, read the fact "
        f"graph and the latest master_guidance — DO NOT restart from zero; build on what "
        f"is already there.\n"
        f"4. Keep going: assess -> pick skills adaptively -> act -> persist, repeatedly. "
        f"An open problem is not a reason to stop. Do NOT finalize prematurely.\n"
        f"5. Persist as you go: rough progress to local memory; shareable findings via "
        f"gm_add; any verified result via fact_submit."
    )


# --- config (read at call time) -------------------------------------------- #

# codex binary + model/effort defaults are resolved via the shared cgm.codex
# launcher (CGM_CODEX_BIN / CGM_CODEX_MODEL / CGM_CODEX_EFFORT).


# --- small helpers --------------------------------------------------------- #

def _read_role(wl: L.WorkerLayout) -> dict:
    out = {"MODEL": codex.model(),
           "REASONING_EFFORT": "high", "ROLE": "high", "CGM_AUTHOR": wl.name}
    rp = wl.role
    if rp.exists():
        for line in rp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_status(wl: L.WorkerLayout, **fields) -> None:
    """Atomic status write (so `cgm status` never reads a half-written file)."""
    path = wl.status
    cur = {}
    if path.exists():
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cur = {}
    cur.update(fields)
    cur["worker"] = wl.name
    cur["pid"] = os.getpid()
    cur["updated_at"] = time.time()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_status(wl: L.WorkerLayout) -> dict:
    """Best-effort read of ``.status.json`` ({} when absent or corrupt).

    Used at loop start to carry the progress clock (``last_fact_at``) across a
    restart, so ``cgm start`` after a crash does not reset a worker's apparent
    freshness to zero."""
    try:
        return json.loads(wl.status.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _deadline_passed(project_dir: Path) -> bool:
    f = project_dir / L.DEADLINE_FILE
    if not f.exists():
        return False
    try:
        return time.time() >= float(f.read_text().strip())
    except (ValueError, OSError):
        return False


def _parse_last_fact_id(log_path: Path) -> Optional[str]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    ids = _FACT_ID_RE.findall(text)
    return ids[-1] if ids else None


def _round_attempted_submit(log_path: Path) -> bool:
    """True when the round log shows at least one ``fact_submit`` gateway call —
    even a rejected submission is real work, not idling."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _SUBMIT_RE.search(text) is not None


def _task_mtime(wl: L.WorkerLayout) -> int:
    """TASK.md mtime (ns); 0 when absent. ``cgm assign`` replaces the file
    atomically, so a change here means a new assignment."""
    try:
        return wl.task.stat().st_mtime_ns
    except OSError:
        return 0


def _wait(wl: L.WorkerLayout, project_dir: Path, seconds: Optional[float],
          task_mtime: int) -> str:
    """Sleep up to ``seconds`` (``None`` = until re-tasked) in short slices,
    waking early on ``.stop``, the project deadline, or a new TASK.md. Returns
    ``"stop"`` / ``"deadline"`` / ``"reassigned"`` / ``"elapsed"``. Counts the
    slices it *requested* rather than reading the clock, so a stubbed
    ``time.sleep`` (tests) still terminates."""
    remaining = seconds
    while True:
        if wl.stop.exists():
            return "stop"
        if _deadline_passed(project_dir):
            return "deadline"
        if _task_mtime(wl) != task_mtime:
            return "reassigned"
        if remaining is not None and remaining <= 0:
            return "elapsed"
        step = _WAIT_SLICE if remaining is None else min(_WAIT_SLICE, remaining)
        time.sleep(step)
        if remaining is not None:
            remaining -= step


# --- one round ------------------------------------------------------------- #

class _Child:
    """Holds the running codex subprocess so the SIGTERM handler can kill it."""
    proc: "subprocess.Popen | None" = None


def run_round(wl: L.WorkerLayout, role: dict, prompt: str, log_path: Path,
              hard_timeout: int) -> int:
    """Exec one ``codex exec`` continuation session. Returns codex's rc, 124 on
    hard-timeout (terminate → wait 10s → kill), or 127 if the codex binary is
    missing."""
    wdir = wl.dir
    codex_bin = codex.resolve_bin()
    cmd = codex.exec_cmd(
        codex_bin, role["MODEL"], role["REASONING_EFFORT"],
        "-C", str(wdir),
        # on an install without .git (tarball download), codex's
        # trusted-directory check refuses to run the worker round
        "--skip-git-repo-check",
        # web search follows the deployment switch CGM_CODEX_WEB_SEARCH (default
        # off = offline-by-contract), overriding the scaffolded .codex/config.toml;
        # codex cross-session memories stay off (CODEX_HOME is shared across runs)
        "-c", codex.web_search_flag(),
        "-c", "features.memories=false",
        "-c", "memories.generate_memories=false",
        "-c", "memories.use_memories=false",
        "--dangerously-bypass-approvals-and-sandbox",
        prompt,
    )
    with open(log_path, "w", encoding="utf-8") as logf:
        try:
            _Child.proc = subprocess.Popen(
                cmd, stdout=logf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=str(wdir),
                env=codex.subprocess_env(codex_bin),
            )
        except FileNotFoundError:
            logf.write(f"[worker_loop] codex binary not found: {cmd[0]}\n")
            return 127
        try:
            return _Child.proc.wait(timeout=hard_timeout if hard_timeout > 0 else None)
        except subprocess.TimeoutExpired:
            _Child.proc.terminate()
            try:
                _Child.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _Child.proc.kill()
            logf.write(f"\n[worker_loop] round hard-timeout after {hard_timeout}s\n")
            return 124
        finally:
            _Child.proc = None


# --- the loop -------------------------------------------------------------- #

def _cleanup_pid(wl: L.WorkerLayout) -> None:
    """Remove our own .pid if it still points at us (clean exit only)."""
    pf = wl.pid
    try:
        if pf.exists() and pf.read_text().strip() == str(os.getpid()):
            pf.unlink(missing_ok=True)
    except OSError:
        pass


def main(worker_dir: str) -> int:
    wdir = Path(worker_dir).resolve()
    if not wdir.is_dir():
        print(f"worker dir not found: {wdir}", file=sys.stderr)
        return 2
    wl = L.WorkerLayout(wdir)
    project_dir = wl.project_dir
    project = wl.project
    worker = wl.name
    role = _read_role(wl)

    beat = float(os.environ.get("CGM_ROUND_BEAT", "5"))
    hard_timeout = int(os.environ.get("CGM_ROUND_HARD_TIMEOUT", "14400"))
    max_rounds = int(os.environ.get("CGM_MAX_ROUNDS", "0"))
    max_fail = int(os.environ.get("CGM_MAX_CONSEC_FAILURES", "5"))
    max_timeout = int(os.environ.get("CGM_MAX_CONSEC_TIMEOUTS", "0"))
    idle_round_secs = float(os.environ.get("CGM_IDLE_ROUND_SECS", "600"))
    backoff_base = float(os.environ.get("CGM_IDLE_BACKOFF_BASE", "60"))
    backoff_max = float(os.environ.get("CGM_IDLE_BACKOFF_MAX", "1800"))
    wl.logs.mkdir(parents=True, exist_ok=True)
    prompt = kickoff(project, worker)

    # progress clock, carried across restarts: the last fact id we have already
    # credited, and when we credited it. A round that banks no NEW fact leaves
    # both untouched, so `cgm status` can age them.
    prev = read_status(wl)
    seen_fact_id = prev.get("last_fact_id")
    last_fact_at = prev.get("last_fact_at")

    def _on_term(signum, _frame):
        if _Child.proc is not None:
            _Child.proc.terminate()
        write_status(wl, state="terminated")
        _cleanup_pid(wl)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)

    write_status(wl, state="running", round=0, started_at=time.time(),
                 last_fact_at=last_fact_at, timeout_streak=0)
    rnd = 0
    consec_fail = 0
    consec_timeout = 0
    idle_streak = 0                     # consecutive idle rounds (see module doc)
    task_seen = _task_mtime(wl)         # the TASK.md this streak was measured against
    try:
        while True:
            if wl.stop.exists():
                wl.stop.unlink(missing_ok=True)
                write_status(wl, state="stopped")
                break
            if _deadline_passed(project_dir):
                write_status(wl, state="deadline")
                break
            if max_rounds and rnd >= max_rounds:
                write_status(wl, state="max_rounds")
                break
            # parked: the worker declared its bounded lane delivered. Run no
            # round until the main agent re-tasks it (a new TASK.md), which also
            # retires the marker. Stop/deadline are re-checked at the loop top.
            if wl.lane_complete.exists():
                write_status(wl, state="parked", parked_at=time.time(),
                             round=rnd, idle_streak=idle_streak)
                if _wait(wl, project_dir, None, task_seen) == "reassigned":
                    wl.lane_complete.unlink(missing_ok=True)
                    task_seen, idle_streak = _task_mtime(wl), 0
                continue

            rnd += 1
            log_path = wl.logs / f"round_{rnd}.log"
            round_started = time.time()
            write_status(wl, state="running", round=rnd, round_started_at=round_started)
            rc = run_round(wl, role, prompt, log_path, hard_timeout)
            round_secs = time.time() - round_started
            fact_id = _parse_last_fact_id(log_path)
            new_fact = bool(fact_id) and fact_id != seen_fact_id
            if new_fact:                                  # this round banked a NEW fact
                seen_fact_id, last_fact_at = fact_id, time.time()
            consec_timeout = consec_timeout + 1 if rc == 124 else 0
            # idle = a short, clean round that neither banked a fact nor even tried
            # to submit one, against an unchanged TASK.md. Anything else resets.
            task_now = _task_mtime(wl)
            if (task_now != task_seen or new_fact or rc != 0
                    or round_secs >= idle_round_secs or _round_attempted_submit(log_path)):
                idle_streak = 0
            else:
                idle_streak += 1
            task_seen = task_now
            write_status(
                wl, state="idle", round=rnd, last_round_at=time.time(),
                last_rc=rc, last_fact_id=fact_id,
                last_fact_at=last_fact_at, timeout_streak=consec_timeout,
                idle_streak=idle_streak,
            )

            if rc == 127:                    # codex missing — do not spin
                write_status(wl, state="error", error="codex binary not found")
                return 127
            consec_fail = consec_fail + 1 if rc not in (0, 124) else 0
            if max_fail and consec_fail >= max_fail:
                write_status(wl, state="error", error=f"{consec_fail} consecutive failed rounds")
                return 1
            # opt-in (default off): a codex that HANGS every round never trips the
            # failure cap, since each round comes back 124. Only act when the
            # operator asked for it — a slow but productive worker is not broken.
            if max_timeout and consec_timeout >= max_timeout:
                write_status(wl, state="error",
                             error=f"{consec_timeout} consecutive round timeouts")
                return 1

            # idle backoff — unless we are about to exit anyway (stop / deadline /
            # round cap), in which case the loop top records the terminal state
            # after the ordinary beat, exactly as before
            exiting = (wl.stop.exists() or _deadline_passed(project_dir)
                       or bool(max_rounds and rnd >= max_rounds))
            if idle_streak >= 2 and backoff_base > 0 and not exiting:
                delay = min(backoff_base * 2 ** (idle_streak - 2), backoff_max)
                write_status(wl, state="backoff", round=rnd, idle_streak=idle_streak,
                             resume_at=time.time() + delay)
                if _wait(wl, project_dir, delay, task_seen) == "reassigned":
                    task_seen, idle_streak = _task_mtime(wl), 0
                continue
            if beat > 0:
                time.sleep(beat)
    finally:
        _cleanup_pid(wl)
    return 0
