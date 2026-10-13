"""Release trust boundary, rollback, and request-drain regressions (no host writes)."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.ansatze import auto_deploy as deploy
from deploy.ansatze import install_auto_deploy as installer


SHA = "a" * 40


@pytest.fixture(autouse=True)
def isolated_deploy_config(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "CONFIG", tmp_path / "no-host-config.json")


def successful_run(**overrides):
    return {"workflow_runs": [{"event": "push", "head_branch": "main", "status": "completed",
                              "conclusion": "success", "head_sha": SHA,
                              "repository": {"full_name": deploy.REPOSITORY},
                              "head_repository": {"full_name": deploy.REPOSITORY}, **overrides}]}


def test_only_successful_main_push_from_own_repository_is_eligible():
    assert deploy.eligible_sha(successful_run()) == SHA
    for change in ({"event": "pull_request"}, {"event": "pull_request_review"},
                   {"head_branch": "topic"}, {"status": "in_progress"}, {"conclusion": "failure"},
                   {"head_sha": "a; arbitrary-command"}, {"head_repository": {"full_name": "fork/repo"}}):
        assert deploy.eligible_sha(successful_run(**change)) is None
    assert deploy.eligible_sha({}) is None


def test_latest_failed_rerun_does_not_fall_back_to_older_success():
    document = successful_run(conclusion="failure")
    document["workflow_runs"] += successful_run()["workflow_runs"]
    assert deploy.eligible_sha(document) is None


@pytest.mark.parametrize("kind", ["receipt", "ancestor", "outside-link", "hardlink", "fifo"])
def test_untrusted_build_cannot_redirect_root_publication(tmp_path, kind):
    tree = tmp_path / "release"
    tree.mkdir()
    outside = tmp_path / "private"
    outside.write_text("do not change")
    if kind == "receipt":
        (tree / ".ansatze-release.json").symlink_to(outside)
    elif kind == "ancestor":
        (tree / "docs").symlink_to(tmp_path, target_is_directory=True)
    elif kind == "outside-link":
        (tree / "other").symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, tree / "other")
    else:
        os.mkfifo(tree / "pipe")
    with pytest.raises(RuntimeError):
        deploy.validate_tree(tree, Path("/usr/bin/python3"))
    assert outside.read_text() == "do not change"


def test_only_venv_python_may_link_to_the_pinned_interpreter(tmp_path):
    python = Path(os.path.realpath(os.sys.executable))
    tree = tmp_path / "release"
    (tree / ".venv/bin").mkdir(parents=True)
    (tree / ".venv/bin/python").symlink_to(python)
    (tree / "internal").symlink_to(".venv")
    deploy.validate_tree(tree, python)
    (tree / "pretend-python").symlink_to(python)
    with pytest.raises(RuntimeError):
        deploy.validate_tree(tree, python)


def test_receipt_open_never_follows_even_internal_ancestor_symlinks(tmp_path):
    tree = tmp_path / "tree"
    (tree / "real").mkdir(parents=True)
    (tree / "docs").symlink_to("real")
    with pytest.raises(OSError):
        deploy.write_receipt(tree, "docs/release.json", {"sha": SHA})
    assert not (tree / "real/release.json").exists()


def test_atomic_switch_preserves_the_previous_directory(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    current = tmp_path / "current"
    current.symlink_to(old)
    deploy.atomic_link(new, current)
    assert current.resolve() == new and old.is_dir()
    assert not (tmp_path / "current.next").exists()


def activation_fixture(tmp_path, monkeypatch, healthy=True):
    root = tmp_path / "releases-root"
    root.mkdir()
    previous = root / "old"
    release = root / SHA
    previous.mkdir()
    release.mkdir()
    (root / "current").symlink_to(previous)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(deploy, "RUNTIME", runtime)
    monkeypatch.setattr(deploy.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(deploy, "main_sha", lambda: SHA)
    monkeypatch.setattr(deploy, "latest_passed_sha", lambda: SHA)
    monkeypatch.setattr(deploy, "active_work", lambda _path: False)
    monkeypatch.setattr(deploy, "public_json", lambda _url, **_kwargs: {"ok": True, "deployment": {"protocol": 1, "active_requests": 0}})
    monkeypatch.setattr(deploy, "command", lambda _args, **_kw: "")
    monkeypatch.setattr(deploy, "healthy", lambda _sha: healthy)
    restarts = []
    monkeypatch.setattr(deploy, "restart_services", lambda: restarts.append((root / "current").resolve()))
    config = {"root": str(root), "data_dir": str(tmp_path / "data"), "legacy_checkout": str(previous)}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setattr(deploy, "CONFIG", config_path)
    return SimpleNamespace(root=root, old=previous, release=release, runtime=runtime,
                           config=config, restarts=restarts)


def test_success_switches_both_code_paths_and_records_previous(tmp_path, monkeypatch):
    fixture = activation_fixture(tmp_path, monkeypatch)
    deploy.activate(fixture.config, fixture.release, SHA)
    assert (fixture.root / "current").resolve() == fixture.release
    assert (fixture.root / "previous").resolve() == fixture.old
    assert fixture.restarts == [fixture.release]
    assert json.loads((fixture.root / "deployment-status.json").read_text())["state"] == "deployed"
    assert not list(fixture.runtime.iterdir())


def test_failed_health_restores_previous_code_and_restarts_it(tmp_path, monkeypatch):
    fixture = activation_fixture(tmp_path, monkeypatch, healthy=False)
    with pytest.raises(RuntimeError, match="restored the previous"):
        deploy.activate(fixture.config, fixture.release, SHA)
    assert (fixture.root / "current").resolve() == fixture.old
    assert fixture.restarts == [fixture.release, fixture.old]
    assert json.loads((fixture.root / "deployment-status.json").read_text())["rejected_sha"] == SHA
    assert not list(fixture.runtime.iterdir())


@pytest.mark.parametrize("busy", ["mutations", "background", "new-main"])
def test_busy_or_superseded_release_never_restarts_services(tmp_path, monkeypatch, busy):
    fixture = activation_fixture(tmp_path, monkeypatch)
    if busy == "mutations":
        monkeypatch.setattr(deploy, "public_json", lambda _url, **_kwargs: {"deployment": {"protocol": 1, "active_requests": 1}})
    elif busy == "background":
        monkeypatch.setattr(deploy, "active_work", lambda _path: True)
    else:
        monkeypatch.setattr(deploy, "main_sha", lambda: "b" * 40)
    deploy.activate(fixture.config, fixture.release, SHA)
    assert (fixture.root / "current").resolve() == fixture.old and not fixture.restarts
    assert not list(fixture.runtime.iterdir())


def test_legacy_handoff_requires_zero_connections_and_known_checkout(tmp_path, monkeypatch):
    fixture = activation_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy, "public_json", lambda _url, **_kwargs: {"ok": True})
    monkeypatch.setattr(deploy, "legacy_connections_busy", lambda: True)
    deploy.activate(fixture.config, fixture.release, SHA)
    assert not fixture.restarts
    fixture.config["legacy_checkout"] = str(tmp_path / "different")
    with pytest.raises(RuntimeError, match="protocol"):
        deploy.activate(fixture.config, fixture.release, SHA)
    assert not fixture.restarts and not list(fixture.runtime.iterdir())


def test_legacy_handoff_can_complete_only_after_connection_drain(tmp_path, monkeypatch):
    fixture = activation_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy, "public_json", lambda _url, **_kwargs: {"ok": True})
    monkeypatch.setattr(deploy, "legacy_connections_busy", lambda: False)
    deploy.activate(fixture.config, fixture.release, SHA)
    assert fixture.restarts == [fixture.release]


def test_tcp_scan_ignores_listeners_and_time_wait_but_includes_inflight(tmp_path):
    header = "sl local_address rem_address st\n"
    (tmp_path / "tcp").write_text(header + "0: 0100007F:11F8 00000000:0000 0A\n1: 0100007F:11F8 0100007F:9999 06\n")
    assert not deploy.legacy_connections_busy(tmp_path)
    with (tmp_path / "tcp").open("a") as stream:
        stream.write("2: 0100007F:1202 0100007F:9999 01\n")  # broker :4610
    assert deploy.legacy_connections_busy(tmp_path)


def test_busy_status_includes_analysis_rechecks_and_lean_harnesses():
    assert deploy.record_busy({"status": "completed", "recheck": {"status": "running"}})
    assert deploy.record_busy({"status": "completed", "harness_running": True})
    assert deploy.record_busy({"status": "queued"})
    assert not deploy.record_busy({"status": "completed", "proof": "status: running"})


def test_caddy_migration_preserves_existing_routes_and_reads():
    source = (Path(__file__).parents[1] / "deploy/ansatze/Caddyfile").read_text()
    rendered = installer.render_caddy(source)
    assert "method POST PUT PATCH DELETE" not in rendered
    assert "@release_bootstrap file" in rendered
    assert "reverse_proxy 127.0.0.1:4600" in rendered
    assert "root * /opt/ansatze/current/docs/.vitepress/dist" in rendered
    assert installer.render_caddy(rendered) == rendered


def test_unit_dropin_does_not_replace_existing_credentials_or_hardening():
    source = (Path(__file__).parents[1] / "deploy/ansatze/ansatze-kimi.service").read_text()
    rendered = installer.render_dropin("ansatze-kimi.service", source, Path("/python/bin/python3"))
    assert "ExecStart=\nExecStart=/opt/ansatze/current/.venv/bin/python" in rendered
    assert "BindReadOnlyPaths=/opt/ansatze /python" in rendered
    assert "LoadCredential" not in rendered and "ProtectSystem" not in rendered


def test_inflight_mutation_counter_is_exception_safe():
    from agent_monitor import console_server as server

    @server._track_request
    def action(fail=False):
        assert server._deployment_activity() == {"protocol": 1, "active_requests": 1}
        if fail:
            raise ValueError("request failed")
        return "complete"

    assert action() == "complete"
    with pytest.raises(ValueError):
        action(True)
    assert server._deployment_activity()["active_requests"] == 0


def test_failed_rollback_restart_still_blocks_the_rejected_sha(tmp_path, monkeypatch):
    fixture = activation_fixture(tmp_path, monkeypatch, healthy=False)
    def fails():
        raise RuntimeError("service failed")
    monkeypatch.setattr(deploy, "restart_services", fails)
    with pytest.raises(RuntimeError, match="operator"):
        deploy.activate(fixture.config, fixture.release, SHA)
    assert (fixture.root / "current").resolve() == fixture.old
    state = json.loads((fixture.root / "deployment-status.json").read_text())
    assert state["state"] == "rollback_restart_failed" and state["rejected_sha"] == SHA
    assert (fixture.root / "activation-in-progress.json").is_file()


def test_activity_scan_rejects_symlinked_runtime_roots(tmp_path):
    (tmp_path / "cache").symlink_to("/tmp")
    with pytest.raises(RuntimeError, match="activity directory"):
        deploy.active_work(tmp_path)


def test_cleanup_restores_only_recorded_timers_and_stops_owned_build(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "RUNTIME", tmp_path)
    monkeypatch.setattr(deploy, "CONFIG", tmp_path / "absent-test-config.json")
    for name in ("maintenance", "bootstrap-maintenance"):
        (tmp_path / name).touch()
    (tmp_path / "paused-timers.json").write_text(json.dumps({"timers": ["ansatze-news.timer"]}))
    (tmp_path / "build-unit.json").write_text(json.dumps({"unit": "ansatze-build-aaaaaaaaaaaa.service"}))
    calls = []
    def execute(args, **_kwargs):
        calls.append(args)
        return "loaded" if "LoadState" in args else ""
    monkeypatch.setattr(deploy, "command", execute)
    deploy.cleanup()
    assert ["systemctl", "start", "ansatze-news.timer"] in calls
    assert ["systemctl", "start", "ansatze-problems.timer"] not in calls
    assert ["systemctl", "stop", "ansatze-build-aaaaaaaaaaaa.service"] in calls
    assert not list(tmp_path.iterdir())


def test_health_counter_excludes_only_its_current_request():
    from agent_monitor import console_server as server
    @server._track_request
    def outer():
        @server._track_request
        def health():
            assert server._deployment_activity(exclude_current=True)["active_requests"] == 1
        health()
    outer()
    assert server._deployment_activity()["active_requests"] == 0


def test_interrupted_activation_cleanup_restores_previous_before_reopening(tmp_path, monkeypatch):
    fixture = activation_fixture(tmp_path, monkeypatch)
    (fixture.runtime / "maintenance").touch()
    deploy.write_receipt(fixture.root, "activation-in-progress.json", {"previous": str(fixture.old), "candidate": SHA})
    deploy.atomic_link(fixture.release, fixture.root / "current")
    deploy.cleanup()
    assert (fixture.root / "current").resolve() == fixture.old
    assert fixture.restarts == [fixture.old]
    assert not list(fixture.runtime.iterdir())
    state = json.loads((fixture.root / "deployment-status.json").read_text())
    assert state["state"] == "interrupted_rolled_back" and state["rejected_sha"] == SHA


def test_receipts_replace_existing_links_without_writing_their_target(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    outside = tmp_path / "private"
    outside.write_text("unchanged")
    (tree / "receipt.json").symlink_to(outside)
    deploy.write_receipt(tree, "receipt.json", {"sha": SHA})
    assert outside.read_text() == "unchanged"
    assert not (tree / "receipt.json").is_symlink()
    assert json.loads((tree / "receipt.json").read_text())["sha"] == SHA
