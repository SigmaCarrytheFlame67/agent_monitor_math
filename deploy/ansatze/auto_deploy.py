#!/usr/bin/env python3
"""Trusted, credential-free host pull deployment. Install with install_auto_deploy.py.

Only public GitHub metadata is fetched. No repository program runs as root.
The installer and this agent are updated manually, never by a deployed commit.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request

REPOSITORY = "ericjiang18/agent_monitor_math"
CONFIG = Path("/etc/ansatze/auto-deploy.json")
INSTALL = Path("/usr/local/lib/ansatze")
RUNTIME = Path("/run/ansatze-deploy")
SHA = re.compile(r"[0-9a-f]{40}\Z")
ACTIVE = {"running", "queued", "starting", "continuing", "stopping"}
SERVICES = ("ansatze-kimi.service", "agent-monitor.service")
BACKGROUND = ("ansatze-news.service", "ansatze-problems.service")


def command(args: list[str], **kwargs) -> str:
    """No shell, no inherited user Git configuration or credential helpers."""
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(args, check=True, text=True, capture_output=True,
                            timeout=kwargs.pop("timeout", 120), env=env, **kwargs)
    return result.stdout.strip()


def public_json(url: str, *, timeout: int = 25) -> dict:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "ansatze-release-agent",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(2_000_000))


def eligible_sha(document: dict) -> str | None:
    """Do not accept fork PR checks, approval events, or incomplete jobs."""
    runs = document.get("workflow_runs", [])
    if not isinstance(runs, list) or not runs:
        return None
    run = runs[0]
    if not isinstance(run, dict):
        return None
    if (run.get("event") != "push" or run.get("head_branch") != "main"
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or run.get("repository", {}).get("full_name") != REPOSITORY
            or run.get("head_repository", {}).get("full_name") != REPOSITORY):
        return None
    sha = str(run.get("head_sha", ""))
    return sha if SHA.fullmatch(sha) else None


def latest_passed_sha() -> str | None:
    # One unauthenticated REST request per two-minute poll (public repo).
    url = (f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/ci.yml/runs"
           "?branch=main&event=push&per_page=1")
    # Inspect the latest run, including running/failed reruns. An older passing
    # run must not override a newer failed run for the same commit.
    return eligible_sha(public_json(url))


def main_sha() -> str:
    line = command(["git", "-c", "credential.helper=", "ls-remote", "--exit-code",
                    f"https://github.com/{REPOSITORY}.git", "refs/heads/main"])
    sha = line.split()[0] if line else ""
    if not SHA.fullmatch(sha):
        raise RuntimeError("GitHub returned an invalid main revision")
    return sha


def read_json(path: Path, limit: int = 20_000_000) -> dict:
    # Never follow run-workspace symlinks, and never emit user content to logs.
    if path.is_symlink() or path.stat().st_size > limit:
        raise RuntimeError("Uncertain activity state; deployment deferred")
    with path.open("rb") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise RuntimeError("Uncertain activity state; deployment deferred")
    return data


def record_busy(record: dict) -> bool:
    if str(record.get("status", "")).lower() in ACTIVE:
        return True
    # A completed analysis can have a running recheck, a readability run a
    # question being answered, and a Lean result a still-running harness. Do
    # not inspect proof text or user settings.
    for name in ("recheck", "harness", "analyze", "readability", "ask"):
        child = record.get(name)
        if isinstance(child, dict) and record_busy(child):
            return True
    return bool(record.get("harness_running"))


def active_work(data: Path) -> bool:
    for relative in ("cache", "cache/harness", "runs", "runs/workspaces"):
        if (data / relative).is_symlink():
            raise RuntimeError("Uncertain activity directory; deployment deferred")
    paths = list((data / "cache" / "harness").glob("*.json"))
    paths += list((data / "runs").glob("*.json"))
    workspaces = data / "runs" / "workspaces"
    if workspaces.exists():
        for workspace in workspaces.iterdir():
            if workspace.is_symlink():
                raise RuntimeError("Uncertain workspace state; deployment deferred")
            if not workspace.is_dir():
                continue
            for name in ("auto_pipeline.json", "lean_verify.json", "proof_graph.json", "lean_proof_graph.json"):
                path = workspace / name
                if path.exists():
                    paths.append(path)
    for path in paths:
        try:
            if record_busy(read_json(path)):
                return True
        except FileNotFoundError:
            continue  # a completed guest run was purged during the scan
    for service in BACKGROUND:
        state = command(["systemctl", "show", service, "-p", "ActiveState", "--value"])
        if state in {"active", "activating", "deactivating", "reloading"}:
            return True
    return False


def legacy_connections_busy(proc: Path = Path("/proc/net")) -> bool:
    """Legacy console is HTTP/1.0: no idle keep-alive connections to mistake
    for in-flight work. Include broker connections conservatively as well.
    Never log peer addresses, request paths, or account data.
    """
    for family in ("tcp", "tcp6"):
        path = proc / family
        if not path.exists():
            continue
        for line in path.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4:
                raise RuntimeError("Cannot establish legacy connection activity")
            port = int(fields[1].rsplit(":", 1)[1], 16)
            state = fields[3]
            if port in {4600, 4610} and state not in {"0A", "06", "07"}:
                return True
    return False


def atomic_link(target: Path, current: Path) -> None:
    temporary = current.with_name(current.name + ".next")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, current)


def save_status(root: Path, **values) -> None:
    values["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = root / "deployment-status.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(values, indent=2) + "\n")
    os.replace(temporary, path)


def release_sha(current: Path) -> str | None:
    try:
        receipt = current / ".ansatze-release.json"
        if receipt.is_symlink() or receipt.stat().st_uid != 0 or current.stat().st_uid != 0:
            return None
        value = read_json(receipt).get("sha", "")
        return value if SHA.fullmatch(str(value)) else None
    except FileNotFoundError:
        return None  # initial legacy-checkout rollback target


def validate_tree(root: Path, python: Path) -> None:
    """Reject build outputs that could redirect subsequent root writes/chowns."""
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("Build output root is not a real directory")
    resolved = root.resolve()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = path.resolve()
                python_link = path.parent == root / ".venv/bin" and name.startswith("python")
                if not target.is_relative_to(resolved) and not (python_link and target == python):
                    raise RuntimeError("Build output contains an escaping symlink")
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise RuntimeError("Build output contains a hard-linked file")
            elif not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("Build output contains a special file")


def write_receipt(root: Path, relative: str, value: dict) -> None:
    """Open every ancestor and the destination without following symlinks."""
    parts = Path(relative).parts
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in parts[:-1]:
            next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = next_fd
        temporary = f".{parts[-1]}.{os.getpid()}.new"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(json.dumps(value) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent)


def freeze_tree(root: Path) -> None:
    # chown/chmod never follow symlinks created by package installation.
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            os.chown(path, 0, 0, follow_symlinks=False)
            if not path.is_symlink():
                mode = path.stat().st_mode
                path.chmod(0o755 if path.is_dir() or mode & 0o111 else 0o644)
    os.chown(root, 0, 0)
    root.chmod(0o755)


def prepare_release(config: dict, sha: str) -> Path:
    root = Path(config["root"])
    release = root / "releases" / sha
    if release.exists() and release_sha(release) == sha:
        return release
    # Build at the final absolute path: venv entrypoint shebangs must not move.
    stage = release
    if stage.is_symlink():
        raise RuntimeError("Release directory cannot be a symlink")
    if stage.exists():
        if (root / "current").resolve() == stage or (root / "previous").resolve() == stage:
            raise RuntimeError("Refusing to replace an active release")
        shutil.rmtree(stage)
    stage.mkdir(mode=0o755)
    mirror = root / "source.git"
    if not mirror.exists():
        command(["git", "init", "--bare", str(mirror)])
    command(["git", "-c", "credential.helper=", "--git-dir", str(mirror), "fetch", "--depth=1",
             f"https://github.com/{REPOSITORY}.git", "refs/heads/main"], timeout=180)
    fetched = command(["git", "--git-dir", str(mirror), "rev-parse", "FETCH_HEAD"])
    if fetched != sha:
        raise RuntimeError("Main changed before build; wait for its passing CI")
    archive = root / "build" / f"{sha}.tar"
    command(["git", "--git-dir", str(mirror), "archive", "--format=tar", f"--output={archive}", sha])
    try:
        with tarfile.open(archive) as stream:
            # The archive is source-only. Reject traversal and links escaping
            # the tree before any unprivileged package hooks are launched.
            stream.extractall(stage, filter="data")
    finally:
        archive.unlink(missing_ok=True)
    account = pwd.getpwnam("ansatze-build")
    for directory, dirs, files in os.walk(stage, followlinks=False):
        for name in dirs + files:
            os.chown(Path(directory) / name, account.pw_uid, account.pw_gid, follow_symlinks=False)
    os.chown(stage, account.pw_uid, account.pw_gid)
    python = Path(config["python"])
    node_bin = Path(config["node_bin"])
    # No user-home credentials, persistent application state, system writes,
    # sudo, or surviving child processes are accessible to dependency hooks.
    write_receipt(RUNTIME, "build-unit.json", {"unit": f"ansatze-build-{sha[:12]}.service"})
    command([
        "systemd-run", "--quiet", "--wait", "--collect", "--pipe",
        f"--unit=ansatze-build-{sha[:12]}", "--uid=ansatze-build", "--gid=ansatze-build",
        "-p", "NoNewPrivileges=yes", "-p", "ProtectSystem=strict", "-p", "ProtectHome=tmpfs",
        "-p", "IPAddressDeny=169.254.0.0/16 fe80::/10 fd00::/8 127.0.0.0/8 ::1/128 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16",
        "-p", "IPAddressAllow=127.0.0.53/32",
        "-p", "PrivateTmp=yes", "-p", "PrivateDevices=yes", "-p", "KillMode=control-group",
        "-p", "CapabilityBoundingSet=", "-p", "RestrictSUIDSGID=yes",
        "-p", "InaccessiblePaths=/var/lib/ansatze /etc/ansatze/credentials /run/user",
        "-p", f"ReadWritePaths={stage}", "-p", f"BindReadOnlyPaths={python.parent.parent} {node_bin.parent}",
        "-p", "MemoryMax=6G", "-p", "TasksMax=512", "-p", "RuntimeMaxSec=20min",
        "/bin/bash", str(INSTALL / "build_release.sh"), str(stage), str(python), str(node_bin),
    ], timeout=1260)
    (RUNTIME / "build-unit.json").unlink(missing_ok=True)
    # Build unit has exited and killed all of its children before ownership
    # changes. Release code remains readable but not writable by the app user.
    validate_tree(stage, python)
    if not (stage / "docs/.vitepress/dist/index.html").is_file():
        raise RuntimeError("Documentation build is missing its index")
    receipt = {"sha": sha, "repository": REPOSITORY}
    write_receipt(stage, ".ansatze-release.json", receipt)
    write_receipt(stage, "docs/.vitepress/dist/release.json", receipt)
    freeze_tree(stage)
    return release


def healthy(sha: str) -> bool:
    try:
        for url in ("http://127.0.0.1:4610/healthz", "http://127.0.0.1:4600/health"):
            body = public_json(url, timeout=5)
            if not body.get("ok"):
                return False
        marker = command(["curl", "--silent", "--show-error", "--fail", "--max-time", "5",
                          "--resolve", "ansatze.ai:443:127.0.0.1", "https://ansatze.ai/release.json"])
        return json.loads(marker).get("sha") == sha
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def restart_services() -> None:
    for service in SERVICES:
        command(["systemctl", "restart", service], timeout=90)


def activate(config: dict, release: Path, sha: str) -> None:
    root = Path(config["root"])
    current = root / "current"
    previous = current.resolve(strict=True)
    marker = RUNTIME / "maintenance"
    bootstrap_marker = RUNTIME / "bootstrap-maintenance"
    paused_timers = []
    # The Caddy matcher is installed once. A short admission pause prevents
    # fresh browser actions (including GET-triggered guest sidecars) while in-flight calls and background work drain.
    marker.touch(mode=0o644)
    try:
        for service in BACKGROUND:
            timer = service.replace(".service", ".timer")
            if command(["systemctl", "show", timer, "-p", "ActiveState", "--value"]) == "active":
                paused_timers.append(timer)
                write_receipt(RUNTIME, "paused-timers.json", {"timers": paused_timers})
                command(["systemctl", "stop", timer])
        time.sleep(5)
        health = public_json("http://127.0.0.1:4600/health", timeout=5)
        activity = health.get("deployment", {})
        legacy = activity.get("protocol") != 1
        if legacy:
            # Only the explicitly configured legacy checkout may use this
            # one-time route. Future releases must implement the counter.
            if previous != Path(config["legacy_checkout"]).resolve():
                raise RuntimeError("Release lacks required deployment activity protocol")
            bootstrap_marker.touch(mode=0o644)
            time.sleep(5)
            if legacy_connections_busy():
                save_status(root, state="deferred_legacy_connections", candidate=sha)
                print("Initial handoff deferred until all legacy connections drain.")
                return
        if (not legacy and activity.get("active_requests") != 0) or active_work(Path(config["data_dir"])):
            save_status(root, state="deferred_busy", candidate=sha)
            print("Deployment deferred: active mathematical work; retry on the next poll.")
            return
        # Recheck exact main + latest CI after the build and drain interval.
        if main_sha() != sha or latest_passed_sha() != sha:
            save_status(root, state="deferred_new_main", candidate=sha)
            return
        if active_work(Path(config["data_dir"])) or (legacy and legacy_connections_busy()):
            save_status(root, state="deferred_busy", candidate=sha)
            return
        atomic_link(previous, root / "previous")
        write_receipt(root, "activation-in-progress.json", {"previous": str(previous), "candidate": sha})
        try:
            atomic_link(release, current)
            restart_services()
            deadline = time.monotonic() + 60
            for _ in range(15):
                if healthy(sha):
                    save_status(root, state="deployed", sha=sha, previous=str(previous))
                    (root / "activation-in-progress.json").unlink(missing_ok=True)
                    print(f"Deployed {sha}; console, broker, and public-site checks passed.")
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(2)
            raise RuntimeError("Release health checks failed")
        except Exception:
            atomic_link(previous, current)
            # Persist rejection before attempting recovery: even a broken old
            # unit must not cause this bad candidate to be retried forever.
            save_status(root, state="rollback_restarting", rejected_sha=sha)
            try:
                restart_services()
            except Exception:
                save_status(root, state="rollback_restart_failed", rejected_sha=sha)
                raise RuntimeError("Previous code restored, but its services could not restart; operator recovery required") from None
            save_status(root, state="rolled_back", rejected_sha=sha)
            (root / "activation-in-progress.json").unlink(missing_ok=True)
            raise RuntimeError("Release failed; restored the previous code and restarted its services") from None
    finally:
        cleanup()


def run(config: dict) -> None:
    root = Path(config["root"])
    sha = latest_passed_sha()
    if not sha or main_sha() != sha:
        print("No new passing main commit is ready.")
        return
    if release_sha(root / "current") == sha:
        if not healthy(sha):
            save_status(root, state="current_release_unhealthy", sha=sha)
            raise RuntimeError("Current release health check failed; inspect services before another deployment")
        return
    # A failed release stays rejected until a new commit or operator retry;
    # don't repeatedly interrupt production with the same failed release.
    status_path = root / "deployment-status.json"
    if status_path.exists() and read_json(status_path).get("rejected_sha") == sha:
        print("Latest main failed its health checks; operator review required.")
        return
    if active_work(Path(config["data_dir"])):
        save_status(root, state="deferred_busy", candidate=sha)
        print("Deployment deferred: active mathematical work.")
        return
    release = prepare_release(config, sha)
    activate(config, release, sha)


def cleanup() -> None:
    """Used both by finally and systemd ExecStopPost after interruption."""
    config = read_json(CONFIG) if CONFIG.exists() else {}
    root = Path(config["root"]) if config else None
    activation = root / "activation-in-progress.json" if root else None
    if activation is not None and activation.exists():
        pending = read_json(activation)
        previous = Path(str(pending.get("previous", "")))
        candidate = str(pending.get("candidate", ""))
        previous_is_release = previous.parent == root / "releases" and SHA.fullmatch(previous.name)
        if not SHA.fullmatch(candidate) or not (previous_is_release or previous == Path(config["legacy_checkout"]).resolve()):
            raise RuntimeError("Invalid interrupted activation receipt")
        atomic_link(previous, root / "current")
        save_status(root, state="interrupted_rollback_restarting", rejected_sha=candidate)
        try:
            restart_services()
        except Exception:
            save_status(root, state="rollback_restart_failed", rejected_sha=candidate)
            raise RuntimeError("Interrupted deployment restored previous code but service recovery needs operator attention") from None
        save_status(root, state="interrupted_rolled_back", rejected_sha=candidate)
        activation.unlink()
    (RUNTIME / "maintenance").unlink(missing_ok=True)
    (RUNTIME / "bootstrap-maintenance").unlink(missing_ok=True)
    path = RUNTIME / "paused-timers.json"
    if path.exists():
        timers = read_json(path).get("timers", [])
        allowed = {service.replace(".service", ".timer") for service in BACKGROUND}
        if not isinstance(timers, list) or any(timer not in allowed for timer in timers):
            raise RuntimeError("Invalid paused timer receipt")
        for timer in timers:
            command(["systemctl", "start", timer])
        path.unlink()
    build = RUNTIME / "build-unit.json"
    if build.exists():
        name = str(read_json(build).get("unit", ""))
        if not re.fullmatch(r"ansatze-build-[0-9a-f]{12}\.service", name):
            raise RuntimeError("Invalid build unit receipt")
        # A killed controller must not leave a build unit writing while a new
        # poll attempts to clean or publish the same directory.
        if command(["systemctl", "show", name, "-p", "LoadState", "--value"]) != "not-found":
            command(["systemctl", "stop", name])
        build.unlink()


def interrupted(_number, _frame):
    raise InterruptedError("Deployment interrupted; restoring the previous code when necessary")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Read public main/CI metadata only; do not deploy")
    parser.add_argument("--cleanup", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check:
        sha = latest_passed_sha()
        print(json.dumps({"passing_main": sha, "current_main": main_sha()}))
        return 0
    if os.geteuid() != 0:
        parser.error("Deployment runs as root through the installed systemd unit; use --check for read-only metadata")
    if args.cleanup:
        cleanup()
        return 0
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    RUNTIME.mkdir(mode=0o755, parents=True, exist_ok=True)
    with (RUNTIME / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            cleanup()
            run(read_json(CONFIG))
        except Exception as exc:
            # Avoid printing subprocess output, run contents, or environment.
            print(f"Deployment stopped ({type(exc).__name__}): {str(exc)[:350]}", file=sys.stderr)
            return 1
        finally:
            cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
