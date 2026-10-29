#!/usr/bin/env python3
"""One-time migration from checkout-based service paths to immutable releases.

Default mode renders a plan without changing the host. --apply installs the
reviewed local agent (not code fetched from GitHub), then enables its timer.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
LEGACY = Path("/home/ubuntu/Ansatze")
RELEASE_ROOT = Path("/opt/ansatze")
INSTALL = Path("/usr/local/lib/ansatze")
CONFIG = Path("/etc/ansatze/auto-deploy.json")
UNITS = Path("/etc/systemd/system")
CADDY = Path("/etc/caddy/Caddyfile")

MAINTENANCE = '''
        # Managed by install_auto_deploy.py. GET can start guest sidecars;
        # pause all admissions while existing work drains.
        @release_bootstrap file {
            root /run/ansatze-deploy
            try_files /bootstrap-maintenance
        }
        header @release_bootstrap Retry-After "15"
        respond @release_bootstrap "Ansätze is updating. Please retry shortly." 503
        @release_pause {
            file {
                root /run/ansatze-deploy
                try_files /maintenance
            }
        }
        header @release_pause Retry-After "15"
        respond @release_pause "Ansätze is updating. Please retry shortly." 503
'''


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=120, **kwargs)


def render_caddy(text: str) -> str:
    old = f"{LEGACY}/docs/.vitepress/dist"
    new = f"{RELEASE_ROOT}/current/docs/.vitepress/dist"
    if old not in text and new not in text:
        raise RuntimeError("Caddy has no recognized Ansätze documentation root")
    result = text.replace(old, new)
    if "@release_pause" not in result:
        needle = "handle @console {"
        if result.count(needle) != 1:
            raise RuntimeError("Expected one existing console handler; review Caddy manually")
        result = result.replace(needle, needle + MAINTENANCE)
    return result


def render_dropin(unit: str, text: str, python: Path) -> str:
    commands = re.findall(r"^ExecStart=(.+)$", text, flags=re.M)
    if len(commands) != 1 or str(LEGACY) not in commands[0]:
        raise RuntimeError(f"Unexpected ExecStart in {unit}; review the service manually")
    current = str(RELEASE_ROOT / "current")
    lines = ["# Installed by deploy/ansatze/install_auto_deploy.py; preserves other drop-ins.", "[Service]", "ExecStart=", "ExecStart=" + commands[0].replace(str(LEGACY), current), "Environment=PYTHONPATH=" + current]
    if unit == "agent-monitor.service":
        lines.append("WorkingDirectory=" + current)
    if "ProtectHome=tmpfs" in text:
        # Keep existing bind paths and hardening. Add the release + interpreter;
        # the pinned legacy interpreter may differ from the new release's one.
        lines.append(f"BindReadOnlyPaths={RELEASE_ROOT} {python.parent.parent}")
    return "\n".join(lines) + "\n"


def install_file(path: Path, data: str, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"Refusing a symlink install destination: {path}")
    temporary = path.with_name(path.name + ".install-new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)
    os.chown(temporary, 0, 0)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Install the reviewed agent and enable its timer")
    parser.add_argument("--render-to", type=Path, help="Render all nonsecret configuration for review")
    parser.add_argument("--python", type=Path, default=LEGACY / ".venv/bin/python", help="Python 3.11–3.13 interpreter")
    parser.add_argument("--node-bin", type=Path, default=Path("/home/ubuntu/.local/node24/bin"))
    args = parser.parse_args()
    python = args.python.resolve(strict=True)
    node_bin = args.node_bin.resolve(strict=True)
    version = run([str(python), "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"]).stdout.strip()
    if version not in {"3.11", "3.12", "3.13"}:
        parser.error("The application needs Python 3.11, 3.12, or 3.13")
    if not (node_bin / "node").is_file() or not (node_bin / "npm").exists():
        parser.error("--node-bin must contain node and npm")
    for program in ("git", "curl", "caddy", "systemd-run", "useradd"):
        if not shutil.which(program):
            parser.error(f"Required host program is missing: {program}")
    files = {
        INSTALL / "auto_deploy.py": (HERE / "auto_deploy.py").read_text(),
        INSTALL / "build_release.sh": (HERE / "auto/build_release.sh").read_text(),
        CONFIG: json.dumps({"root": str(RELEASE_ROOT), "python": str(python), "node_bin": str(node_bin), "data_dir": "/var/lib/ansatze/console", "legacy_checkout": str(LEGACY)}, indent=2) + "\n",
    }
    for name in ("ansatze-auto-deploy.service", "ansatze-auto-deploy.timer"):
        files[UNITS / name] = (HERE / "auto" / name).read_text()
    for name in ("agent-monitor.service", "ansatze-kimi.service", "ansatze-news.service", "ansatze-problems.service"):
        path = UNITS / name
        if path.exists():
            files[UNITS / f"{name}.d/80-release.conf"] = render_dropin(name, path.read_text(), python)
    old_caddy = CADDY.read_text()
    files[CADDY] = render_caddy(old_caddy)
    if args.render_to:
        for target, content in files.items():
            output = args.render_to / target.relative_to("/")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content)
        print(f"Rendered {len(files)} nonsecret files under {args.render_to}")
    print("Plan: install root-owned pull agent; create ansatze-build; preserve the current checkout as the initial rollback target; redirect unit/Caddy code paths through /opt/ansatze/current; enable the two-minute timer.")
    print("No application restart during installation. The first legacy-to-release handoff also pauses reads and waits for all console/broker TCP connections and persisted background work to drain; subsequent releases pause admissions and check the request activity counter.")
    if not args.apply:
        print("Preview only. Pass --apply after reviewing the rendered configuration.")
        return 0
    if os.geteuid() != 0:
        parser.error("--apply requires root")
    backup = Path("/etc/ansatze/backup") / ("auto-deploy-" + time.strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(parents=True)
    for target in files:
        if target.exists():
            saved = backup / target.relative_to("/")
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
    found = subprocess.run(["getent", "passwd", "ansatze-build"], capture_output=True).returncode == 0
    if not found:
        run(["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", "ansatze-build"])
    for directory in (RELEASE_ROOT, RELEASE_ROOT / "releases", RELEASE_ROOT / "build", Path("/run/ansatze-deploy")):
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    current = RELEASE_ROOT / "current"
    if not current.exists() and not current.is_symlink():
        current.symlink_to(LEGACY, target_is_directory=True)
    if not current.is_symlink():
        raise RuntimeError("Current release must be a symlink")
    # Validate Caddy before publishing its new config. The symlink already
    # points to exactly the currently served documentation tree.
    candidate = backup / "candidate.Caddyfile"
    candidate.write_text(files[CADDY])
    run(["caddy", "validate", "--config", str(candidate), "--adapter", "caddyfile"])
    timer = "ansatze-auto-deploy.timer"
    timer_was_active = subprocess.run(["systemctl", "is-active", "--quiet", timer]).returncode == 0
    timer_was_enabled = subprocess.run(["systemctl", "is-enabled", "--quiet", timer]).returncode == 0
    try:
        for target, content in files.items():
            install_file(target, content)
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "reload", "caddy"])
        run(["systemctl", "enable", "--now", "ansatze-auto-deploy.timer"])
    except Exception:
        if not timer_was_active:
            subprocess.run(["systemctl", "stop", timer], capture_output=True)
        if not timer_was_enabled:
            subprocess.run(["systemctl", "disable", timer], capture_output=True)
        for target in files:
            saved = backup / target.relative_to("/")
            if saved.exists():
                shutil.copy2(saved, target)
            else:
                target.unlink(missing_ok=True)
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "reload", "caddy"])
        if timer_was_active:
            run(["systemctl", "start", timer])
        raise
    print(f"Installed; backups: {backup}. Inspect journalctl -u ansatze-auto-deploy.service.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
