"""agent-monitor CLI — serve dashboard, build cache, run engines, create the admin,
purge old guest runs."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _bootstrap() -> None:
    # Allow running as `python -m agent_monitor.cli` before install
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from agent_monitor.paths import ensure_data_dirs, ensure_import_paths

    ensure_import_paths()
    ensure_data_dirs()


def _default_cache() -> Path:
    from agent_monitor import CACHE_DIR

    return CACHE_DIR


def cmd_serve(args: argparse.Namespace) -> None:
    from agent_monitor import CACHE_DIR, ENGINES_DIR, ROOT
    from agent_monitor.paths import ensure_data_dirs

    ensure_data_dirs()
    cache = Path(args.cache_dir).resolve() if args.cache_dir else CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["LLM_DASHBOARD_CACHE"] = str(cache)
    os.environ["LLM_MONITOR_PORT"] = str(args.port)
    os.environ["AGENT_MONITOR_PORT"] = str(args.port)
    os.environ["HARNESS_OUTPUT_ROOT"] = str(
        Path(args.harness_output).resolve()
        if args.harness_output
        else ENGINES_DIR / "ucla"
    )
    calls = Path(args.calls).resolve() if args.calls else ROOT / "data" / "calls.jsonl"
    os.environ["LLM_MONITOR_LOG"] = str(calls)
    if not calls.exists():
        calls.parent.mkdir(parents=True, exist_ok=True)
        calls.touch()

    from agent_monitor.console_server import main as console_main

    print(f"Unified Math Proving Console -> http://localhost:{args.port}")
    print(f"  classic monitor           -> http://localhost:{args.port}/monitor")
    print(f"  cache: {cache / 'harness'}")
    print(f"  root:  {ROOT}")
    console_main(port=args.port)


def cmd_build(args: argparse.Namespace) -> None:
    from agent_monitor import CACHE_DIR, ENGINES_DIR, RUNS_DIR
    from agent_monitor.builders import hermes_builder
    from agent_monitor.schema import normalize_run

    cache = Path(args.cache_dir).resolve() if args.cache_dir else CACHE_DIR
    harness_cache = cache / "harness"
    harness_cache.mkdir(parents=True, exist_ok=True)
    os.environ["LLM_DASHBOARD_CACHE"] = str(cache)

    entries: list[dict] = []

    # Keep existing demo / prior runs unless --replace
    manifest_path = harness_cache / "manifest.json"
    if manifest_path.exists() and not args.replace:
        try:
            prev = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries.extend(prev.get("runs") or [])
        except (json.JSONDecodeError, OSError):
            pass

    # Hermes runner outputs
    hermes_entries = hermes_builder.build_all(harness_cache, RUNS_DIR)
    entries = _merge_entries(entries, hermes_entries)

    # IMProof WorkflowRuns
    improof_dir = Path(args.improof_dir).resolve() if args.improof_dir else (
        ENGINES_DIR / "improof" / "WorkflowRuns"
    )
    if improof_dir.exists():
        from harness_dashboard.build import merge_manifest_entries
        from harness_dashboard.improofbench_builder import rebuild_from_improof_artifacts

        improof_entries = rebuild_from_improof_artifacts(improof_dir, cache_dir=harness_cache)
        for e in improof_entries:
            e["engine"] = "improof"
            # stamp engine into run json
            run_path = harness_cache / f"{e['run_id']}.json"
            if run_path.exists():
                try:
                    data = json.loads(run_path.read_text(encoding="utf-8"))
                    data = normalize_run(data, engine="improof")
                    run_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                except (json.JSONDecodeError, OSError):
                    pass
        entries = _merge_entries(entries, improof_entries)
        print(f"[agent-monitor] IMProof runs: {len(improof_entries)}")

    # UCLA artifacts (optional path)
    if args.ucla_dir:
        ucla_dir = Path(args.ucla_dir).resolve()
        if ucla_dir.exists():
            from harness_dashboard.artifact_builder import rebuild_from_artifacts

            ucla_entries = rebuild_from_artifacts(ucla_dir, cache_dir=harness_cache)
            for e in ucla_entries:
                e["engine"] = "ucla"
                run_path = harness_cache / f"{e['run_id']}.json"
                if run_path.exists():
                    try:
                        data = json.loads(run_path.read_text(encoding="utf-8"))
                        data = normalize_run(data, engine="ucla")
                        run_path.write_text(
                            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                    except (json.JSONDecodeError, OSError):
                        pass
            entries = _merge_entries(entries, ucla_entries)
            print(f"[agent-monitor] UCLA runs: {len(ucla_entries)}")

    # Ensure demo improof sample stays tagged
    demo = harness_cache / "improof_prob_001.json"
    if demo.exists():
        try:
            data = normalize_run(json.loads(demo.read_text(encoding="utf-8")), engine="improof")
            demo.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            if not any(e.get("run_id") == "improof_prob_001" for e in entries):
                entries.append(
                    {
                        "run_id": "improof_prob_001",
                        "engine": "improof",
                        "trace_name": data.get("trace_name") or "improof_prob_001",
                        "source": "demo",
                        "agent_count": len(data.get("agents") or []),
                        "total_cost_usd": (data.get("totals") or {}).get("cost_usd"),
                    }
                )
        except (json.JSONDecodeError, OSError):
            pass

    manifest = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "runs": entries,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[agent-monitor] Wrote manifest ({len(entries)} runs) -> {manifest_path}")


def _merge_entries(existing: list[dict], new_entries: list[dict]) -> list[dict]:
    by_id = {e.get("run_id"): e for e in existing if e.get("run_id")}
    for e in new_entries:
        rid = e.get("run_id")
        if rid:
            by_id[rid] = {**(by_id.get(rid) or {}), **e}
    return list(by_id.values())


def cmd_problems(args: argparse.Namespace) -> None:
    from agent_monitor import PROBLEMS_DIR

    manifest = PROBLEMS_DIR / "manifest.json"
    if not manifest.exists():
        print("No problems/manifest.json — run repo setup first.")
        sys.exit(1)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    problems = data.get("problems") or []
    if args.source:
        problems = [p for p in problems if p.get("source") == args.source]
    print(f"{len(problems)} problems")
    for p in problems[: args.limit]:
        print(f"  [{p.get('source')}] {p.get('problem_id')}  {p.get('statement_path')}")


def cmd_run(args: argparse.Namespace) -> None:
    from agent_monitor import PROBLEMS_DIR

    problem_path = Path(args.problem)
    if not problem_path.exists():
        candidate = PROBLEMS_DIR / args.problem
        if candidate.exists():
            problem_path = candidate
        else:
            # try manifest lookup by id
            man = PROBLEMS_DIR / "manifest.json"
            if man.exists():
                for p in json.loads(man.read_text(encoding="utf-8")).get("problems") or []:
                    if p.get("problem_id") == args.problem:
                        problem_path = PROBLEMS_DIR / p["statement_path"]
                        break
    if not problem_path.exists():
        print(f"Problem not found: {args.problem}")
        sys.exit(1)

    text = problem_path.read_text(encoding="utf-8", errors="replace")
    engine = args.engine
    if engine == "hermes":
        from agent_monitor.runners import hermes as hermes_runner

        run = hermes_runner.run_problem(
            text,
            problem_id=args.problem_id or problem_path.stem,
            model=args.model,
            max_iterations=args.max_iterations,
        )
        print(json.dumps({"run_id": run.get("run_id"), "engine": "hermes", "status": "ok"}, indent=2))
    elif engine == "math_harness":
        from agent_monitor import jobs

        # The standalone CLI has no signed-in account identity, so it uses only
        # API credentials deliberately supplied to its process environment.
        # The web console remains the surface for per-user Codex/Claude login.
        prior_disable = os.environ.get("AGENT_MONITOR_DISABLE_AUTO_PIPELINE")
        os.environ["AGENT_MONITOR_DISABLE_AUTO_PIPELINE"] = "1"
        try:
            job = jobs.start_job(
                engine="math_harness",
                problem_id=args.problem_id or problem_path.stem,
                problem_text=text,
                model=args.model or None,
                max_iterations=args.max_iterations,
                user=None,
                use_subagents=False,
                auth_route="api_key",
            )
        finally:
            if prior_disable is None:
                os.environ.pop("AGENT_MONITOR_DISABLE_AUTO_PIPELINE", None)
            else:
                os.environ["AGENT_MONITOR_DISABLE_AUTO_PIPELINE"] = prior_disable
        job_id = str(job.get("job_id") or "")
        run_id = str(job.get("run_id") or "")
        try:
            while True:
                current = jobs.get_job(job_id)
                if not current:
                    raise RuntimeError("Math Harness job disappeared before completion")
                if current.get("status") in {"finished", "failed", "stopped"}:
                    break
                time.sleep(0.25)
        except KeyboardInterrupt:
            if run_id:
                jobs.stop_run(run_id)
            raise
        print(json.dumps(current, indent=2)[:4000])
        if current.get("status") != "finished":
            sys.exit(1)
    elif engine == "improof":
        from agent_monitor.runners import improof as improof_runner

        result = improof_runner.run_problem(problem_path, problem_id=args.problem_id)
        print(json.dumps(result, indent=2)[:4000])
    else:
        print(f"Unknown engine: {engine}")
        sys.exit(1)


# Exit status when an administrator already exists and --force-additional was
# not given. Deployment scripts treat it as "nothing to do", not as a failure.
ADMIN_EXISTS_EXIT = 3
_MAX_PASSWORD_CHARS = 1024


def _registered_admins() -> list[str]:
    """Return the emails of existing non-guest administrator accounts."""
    from contextlib import closing

    from agent_monitor import auth

    with closing(auth._conn()) as conn:
        rows = conn.execute(
            "SELECT email FROM users WHERE is_admin=1 AND is_guest=0"
            " AND (password_hash IS NOT NULL OR google_sub IS NOT NULL)"
            " ORDER BY id"
        ).fetchall()
    return [str(row["email"]) for row in rows]


def _read_admin_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        raw = sys.stdin.read(_MAX_PASSWORD_CHARS + 2)
        if raw.endswith("\n"):
            raw = raw[:-1]
        if raw.endswith("\r"):
            raw = raw[:-1]
        if "\n" in raw or "\r" in raw or len(raw) > _MAX_PASSWORD_CHARS:
            raise ValueError("--password-stdin expects exactly one password line")
        return raw
    password = getpass.getpass("Admin password (at least 8 characters): ")
    if getpass.getpass("Repeat the password: ") != password:
        raise ValueError("The passwords do not match")
    return password


def cmd_set_password(args: argparse.Namespace) -> int:
    """Set an account's password when emailed reset links are unavailable."""
    from agent_monitor import auth

    print(f"Account database: {auth.DB_PATH}")
    try:
        email = (args.email or input("Account email: ")).strip()
        if args.password_stdin:
            password = _read_admin_password(args)
        else:
            password = getpass.getpass("New password (at least 8 characters): ")
            if getpass.getpass("Repeat the password: ") != password:
                raise ValueError("The passwords do not match")
        account = auth.set_password(email, password)
    except (ValueError, EOFError) as exc:
        print(f"error: {exc or 'no input'}", file=sys.stderr)
        return 1
    print(f"Password updated for {account['email']} (id {account['id']}); its sessions were signed out.")
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    """Create the owner's administrator account without opening registration.

    HTTP self-registration stays closed (``require_open=False`` is a local,
    operator-only path).  The account database is the one selected by
    ``AGENT_MONITOR_DATA_DIR``; run this as the console's service user so the
    database keeps that owner.
    """
    from agent_monitor import DATA_DIR, ROOT, auth

    print(f"Account database: {auth.DB_PATH}")
    if DATA_DIR == (ROOT / "data").resolve():
        print(
            "Note: AGENT_MONITOR_DATA_DIR is unset, so this is the checkout's "
            "development data directory, not a production one."
        )
    if args.password_stdin and not args.email:
        print("error: --password-stdin requires --email", file=sys.stderr)
        return 2
    try:
        existing = _registered_admins()
    except Exception as exc:  # noqa: BLE001 - report without a traceback
        print(f"error: cannot open the account database: {exc}", file=sys.stderr)
        return 1
    if existing and not args.force_additional:
        print(
            f"An administrator already exists ({len(existing)} account"
            f"{'s' if len(existing) != 1 else ''}); nothing was changed. "
            "Pass --force-additional to create another administrator."
        )
        return ADMIN_EXISTS_EXIT
    try:
        email = (args.email or input("Admin email: ")).strip()
        password = _read_admin_password(args)
        account = auth.register(email, password, name=args.name, require_open=False)
    except (ValueError, EOFError) as exc:
        print(f"error: {exc or 'no input'}", file=sys.stderr)
        return 1
    if not account.get("is_admin"):
        # register() grants admin only to the first registered account.
        from contextlib import closing

        with closing(auth._conn()) as conn, conn:
            conn.execute(
                "UPDATE users SET is_admin=1 WHERE id=? AND is_guest=0",
                (int(account["id"]),),
            )
    print(f"Created administrator {account['email']} (id {account['id']}).")
    try:
        encrypted = auth._dedicated_fernet() is not None or auth._legacy_fernet() is not None
    except RuntimeError:
        encrypted = False
    if not encrypted:
        print(
            "No data key was available to this command, so the account's default "
            "settings (public engine, model and runtime defaults, no secrets) were "
            "stored unencrypted. A console that has the data key re-encrypts them "
            "the first time the account is used, so no restart is needed (a "
            "restart also re-encrypts them)."
        )
    return 0


def _retention_days(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number of days") from None
    if not value >= 1 or value > 36_500:
        raise argparse.ArgumentTypeError("must be between 1 and 36500 days")
    return value


def cmd_purge_guest_runs(args: argparse.Namespace) -> int:
    """List, or with ``--apply`` delete, finished guest runs older than N days.

    Wraps :func:`agent_monitor.jobs.purge_guest_runs`: only terminal runs
    recorded as anonymous-guest runs are selected, active runs are never
    touched, and usage history is kept.  Without ``--apply`` nothing changes.
    """
    from agent_monitor import DATA_DIR, ROOT, jobs

    print(f"Run data directory: {DATA_DIR}")
    if DATA_DIR == (ROOT / "data").resolve():
        print(
            "Note: AGENT_MONITOR_DATA_DIR is unset, so this is the checkout's "
            "development data directory, not a production one."
        )
    days = args.older_than_days
    try:
        selected = jobs.purge_guest_runs(older_than_days=days, dry_run=not args.apply)
    except Exception as exc:  # noqa: BLE001 - report without a traceback
        print(f"error: cannot purge guest runs: {exc}", file=sys.stderr)
        return 1
    plural = "s" if len(selected) != 1 else ""
    age = f"{days:g} day{'s' if days != 1 else ''}"
    if args.apply:
        print(
            f"Deleted {len(selected)} finished guest run{plural} older than "
            f"{age} (usage history is kept)."
        )
    else:
        print(
            f"{len(selected)} finished guest run{plural} older than {age} "
            "would be deleted. Nothing was changed; pass --apply to delete them."
        )
    for run_id in selected:
        print(f"  {run_id}")
    if args.apply and selected:
        print(
            "Run this while the console is idle or stopped: a running console "
            "keeps its in-memory index of runs until it restarts."
        )
    return 0


def main(argv: list[str] | None = None) -> None:
    _bootstrap()
    from agent_monitor import CACHE_DIR, ENGINES_DIR

    parser = argparse.ArgumentParser(
        prog="agent-monitor",
        description="Unified informal math proving console (Math Harness / IMProof / Hermes)",
    )
    sub = parser.add_subparsers(dest="command")

    serve_p = sub.add_parser("serve", help="Start pipeline dashboard")
    serve_p.add_argument("--port", type=int, default=int(os.environ.get("AGENT_MONITOR_PORT", "4600")))
    serve_p.add_argument("--cache-dir", type=str, default=str(CACHE_DIR))
    serve_p.add_argument("--calls", type=str, default="")
    serve_p.add_argument("--harness-output", type=str, default="")

    build_p = sub.add_parser("build", help="Build unified run cache / manifest")
    build_p.add_argument("--cache-dir", type=str, default=str(CACHE_DIR))
    build_p.add_argument(
        "--improof-dir",
        type=str,
        default=str(ENGINES_DIR / "improof" / "WorkflowRuns"),
    )
    build_p.add_argument("--ucla-dir", type=str, default="", help="Optional UCLA _harness_runs root")
    build_p.add_argument("--replace", action="store_true", help="Replace manifest instead of merge")

    probs = sub.add_parser("problems", help="List bundled problems")
    probs.add_argument("--source", type=str, default="")
    probs.add_argument("--limit", type=int, default=50)

    run_p = sub.add_parser("run", help="Run a problem with selected engine")
    run_p.add_argument("engine", choices=["hermes", "math_harness", "improof"])
    run_p.add_argument("problem", help="Problem id or path to statement file")
    run_p.add_argument("--problem-id", type=str, default="")
    run_p.add_argument("--model", type=str, default="")
    run_p.add_argument("--max-iterations", type=int, default=60)

    admin_p = sub.add_parser(
        "create-admin",
        help="Create the owner/administrator account (registration stays closed)",
        description=(
            "Create an administrator account in the database selected by "
            "AGENT_MONITOR_DATA_DIR without enabling HTTP self-registration. "
            "Refuses when an administrator already exists unless "
            "--force-additional is given (exit status 3)."
        ),
    )
    admin_p.add_argument("--email", type=str, default="", help="Account email (prompted when omitted)")
    admin_p.add_argument("--name", type=str, default=None, help="Optional display name")
    admin_p.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password as one line from standard input instead of prompting",
    )
    admin_p.add_argument(
        "--force-additional",
        action="store_true",
        help="Create another administrator even if one already exists",
    )

    setpw_p = sub.add_parser(
        "set-password",
        help="Set an account's password (when emailed reset links are not configured)",
        description=(
            "Set the password of an existing account in the database selected by "
            "AGENT_MONITOR_DATA_DIR and sign that account out everywhere. Run it as "
            "the console's service user."
        ),
    )
    setpw_p.add_argument("--email", type=str, default="", help="Account email (prompted when omitted)")
    setpw_p.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the password as one line from standard input instead of prompting",
    )

    purge_p = sub.add_parser(
        "purge-guest-runs",
        help="List (or with --apply delete) finished guest runs older than N days",
        description=(
            "Guest identities expire after 24 hours, but their run records and "
            "workspaces stay in the data directory selected by "
            "AGENT_MONITOR_DATA_DIR until removed. Lists finished anonymous-guest "
            "runs older than --older-than-days; --apply deletes them (usage "
            "history is kept). Run it as the console's service user while the "
            "console is idle or stopped."
        ),
    )
    purge_p.add_argument(
        "--older-than-days",
        type=_retention_days,
        default=30.0,
        help="Minimum age in days since the run's last update (default 30, at least 1)",
    )
    purge_p.add_argument(
        "--apply",
        action="store_true",
        help="Delete the listed runs (default: only list them)",
    )

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "serve":
        cmd_serve(args)
    elif args.command == "build":
        cmd_build(args)
    elif args.command == "problems":
        cmd_problems(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "create-admin":
        sys.exit(cmd_create_admin(args))
    elif args.command == "set-password":
        sys.exit(cmd_set_password(args))
    elif args.command == "purge-guest-runs":
        sys.exit(cmd_purge_guest_runs(args))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
