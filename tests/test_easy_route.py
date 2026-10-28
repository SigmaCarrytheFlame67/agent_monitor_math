"""Easy requests on Plain: the judge, the start, each follow-up, and the key's isolation."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from agent_monitor import easy_route, jobs, subprocess_env

KEY = "sk-or-v1-" + "0" * 64  # a placeholder, never a real key


def _reply(content: str):
    body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return Resp(body)


class _Opener:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class JudgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {easy_route.KEY_ENV: KEY}, clear=False))
        os.environ.pop(easy_route.SWITCH_ENV, None)
        os.environ.pop(easy_route.MODEL_ENV, None)

    def judge(self, result, text="Prove that 2 + 2 = 4.", **kw):
        opener = _Opener(result)
        with patch("urllib.request.build_opener", return_value=opener):
            return easy_route.judge(text, **kw), opener

    def test_on_only_with_the_key_and_not_switched_off(self) -> None:
        self.assertTrue(easy_route.enabled())
        with patch.dict(os.environ, {easy_route.SWITCH_ENV: "0"}):
            self.assertFalse(easy_route.enabled())
        with patch.dict(os.environ, {easy_route.KEY_ENV: "  "}):
            self.assertFalse(easy_route.enabled())
        self.assertEqual(easy_route.model(), "typesafe/jev-1.13")
        with patch.dict(os.environ, {easy_route.MODEL_ENV: "vendor/other"}):
            self.assertEqual(easy_route.model(), "vendor/other")

    def test_an_easy_verdict_and_the_request_sent(self) -> None:
        verdict, opener = self.judge(_reply('{"difficulty": "easy", "reason": "A one-line computation."}'))
        self.assertEqual((verdict["verdict"], verdict["reason"], verdict["model"], verdict["error"]),
                         ("easy", "A one-line computation.", "typesafe/jev-1.13", ""))
        req, timeout = opener.requests[0]
        self.assertEqual(req.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {KEY}")
        sent = json.loads(req.data)
        self.assertEqual(sent["model"], "typesafe/jev-1.13")
        self.assertEqual(sent["temperature"], 0)
        self.assertIn("Prove that 2 + 2 = 4.", sent["messages"][1]["content"])
        self.assertLessEqual(timeout, 15)

    def test_a_follow_up_is_rated_with_its_problem(self) -> None:
        _, opener = self.judge(_reply('{"difficulty":"hard"}'), text="keep trying!", problem="Prove the Riemann hypothesis.")
        content = json.loads(opener.requests[0][0].data)["messages"][1]["content"]
        self.assertIn("ORIGINAL PROBLEM OF THE SESSION:\nProve the Riemann hypothesis.", content)
        self.assertIn("REQUEST (this turn):\nkeep trying!", content)

    def test_anything_but_a_clear_easy_is_hard(self) -> None:
        for content, want in [
            ('{"difficulty": "hard", "reason": "Research level."}', "hard"),
            ('```json\n{"difficulty": "EASY", "reason": "x"}\n```', "easy"),
            ("easy", "easy"),
            ("Easy.", "easy"),
            ("I think it is easy but maybe hard", "hard"),
            ('{"difficulty": "medium"}', "hard"),
            ("", "hard"),
            ("{not json", "hard"),
        ]:
            verdict, _ = self.judge(_reply(content))
            self.assertEqual(verdict["verdict"], want, content)

    def test_failures_are_hard_with_an_error_and_never_the_key_or_the_text(self) -> None:
        for result, error in [
            (urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"")), "judge HTTP 401"),
            (TimeoutError("slow"), "judge unavailable (TimeoutError)"),
            (urllib.error.URLError("down"), "judge unavailable (URLError)"),
            (_reply_garbage := io.BytesIO(b"not json"), None),
        ]:
            if error is None:
                class R(io.BytesIO):
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False
                result = R(b"not json")
                error = "judge unavailable (JSONDecodeError)"
            verdict, _ = self.judge(result, text="secret problem text")
            self.assertEqual((verdict["verdict"], verdict["error"]), ("hard", error))
            self.assertNotIn(KEY, json.dumps(verdict))
            self.assertNotIn("secret problem text", json.dumps(verdict))

    def test_no_key_never_calls_out(self) -> None:
        with patch.dict(os.environ, {easy_route.KEY_ENV: ""}):
            verdict, opener = self.judge(_reply('{"difficulty":"easy"}'))
        self.assertEqual((verdict["verdict"], verdict["error"], opener.requests), ("hard", "no judge key", []))

    def test_long_text_is_cut(self) -> None:
        _, opener = self.judge(_reply('{"difficulty":"hard"}'), text="x" * 50_000)
        self.assertLess(len(json.loads(opener.requests[0][0].data)["messages"][1]["content"]), 9_000)

    def test_the_judges_address(self) -> None:
        self.assertEqual(easy_route.url(), easy_route.URL)
        for value, want in [("https://proxy.example/v1/chat/completions", "https://proxy.example/v1/chat/completions"),
                            ("http://127.0.0.1:8123/judge", "http://127.0.0.1:8123/judge"),
                            ("http://evil.example/judge", easy_route.URL), ("file:///etc/passwd", easy_route.URL)]:
            with patch.dict(os.environ, {easy_route.URL_ENV: value}):
                self.assertEqual(easy_route.url(), want, value)

    def test_asking_for_cgm(self) -> None:
        for text in ["use Ansatz CGM", "Please run it with CGM.", "use the full harness", "ANSATZ CGM please"]:
            self.assertTrue(easy_route.asks_for_cgm(text), text)
        for text in ["Prove that 1+1=2", "the cgmx lemma", "keep trying!"]:
            self.assertFalse(easy_route.asks_for_cgm(text), text)


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_turns_are_recorded_with_their_home(self) -> None:
        easy_route.note_turn(self.ws, home="math_harness", engine="plain",
                             verdict={"verdict": "easy", "reason": "short", "model": "m", "error": ""}, why="judged")
        easy_route.note_turn(self.ws, home="ignored", engine="math_harness", verdict=None, why="asked")
        state = easy_route.load(self.ws)
        self.assertEqual(easy_route.home(self.ws), "math_harness")
        self.assertEqual([(t["engine"], t["why"], t.get("verdict")) for t in state["turns"]],
                         [("plain", "judged", "easy"), ("math_harness", "asked", None)])

    def test_no_workspace_a_bad_file_or_a_symlink_records_nothing(self) -> None:
        easy_route.note_turn(self.ws / "missing", home="math_harness", engine="plain", verdict=None, why="x")
        (self.ws / easy_route.STATE_FILE).write_text("{bad", encoding="utf-8")
        self.assertEqual(easy_route.load(self.ws), {})
        self.assertEqual(easy_route.home(None), "")
        other = Path(self.enterContext(tempfile.TemporaryDirectory())) / "target.json"
        other.write_text("{}", encoding="utf-8")
        (self.ws / easy_route.STATE_FILE).unlink()
        (self.ws / easy_route.STATE_FILE).symlink_to(other)
        easy_route.note_turn(self.ws, home="math_harness", engine="plain", verdict=None, why="x")
        self.assertEqual(other.read_text(encoding="utf-8"), "{}")

    def test_the_notes(self) -> None:
        note = easy_route.plain_note({"model": "typesafe/jev-1.13", "reason": "A routine identity."})
        self.assertIn("this turn runs on Plain", note)
        self.assertIn("typesafe/jev-1.13: A routine identity.", note)
        self.assertIn("use Ansatz CGM", note)
        self.assertEqual(easy_route.cgm_note("asked"), "This turn runs on Ansatz CGM again, as asked.")
        self.assertIn("rated not easy by m", easy_route.cgm_note("judged", {"model": "m"}))


class StartTest(unittest.TestCase):
    """start_job_routed: which engine a start gets."""

    ADMIN = {"id": 1, "is_admin": True}
    MEMBER = {"id": 7, "is_admin": False}

    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.dict(os.environ, {easy_route.KEY_ENV: KEY}))
        os.environ.pop(easy_route.SWITCH_ENV, None)
        self.calls = []
        self.chat = []
        self.fail_plain = None
        self.serial = 0

        def start_job(**kwargs):
            self.calls.append(kwargs)
            if kwargs["engine"] == "plain" and self.fail_plain:
                raise self.fail_plain
            self.serial += 1
            rid = f"{kwargs['engine']}_adhoc_{self.serial}"
            (root / "runs" / "workspaces" / rid).mkdir(parents=True)
            return {"run_id": rid, "engine": kwargs["engine"]}

        self.enterContext(patch.object(jobs, "start_job", side_effect=start_job))
        self.enterContext(patch.object(jobs, "_append_chat", side_effect=lambda rid, role, text: self.chat.append((rid, role, text))))
        self.root = root

    def start(self, verdict="easy", user=None, **kw):
        judge = patch.object(easy_route, "judge", return_value={"verdict": verdict, "reason": "Short.", "model": "typesafe/jev-1.13", "error": ""})
        with judge as judged:
            job = jobs.start_job_routed(engine=kw.pop("engine", "math_harness"), problem_text=kw.pop("problem_text", "Show 1+1=2."),
                                        user=user or self.ADMIN, model="gpt-6.1-sol", auth_route="codex_subscription", **kw)
        return job, judged

    def state(self, job):
        return easy_route.load(self.root / "runs" / "workspaces" / job["run_id"])

    def test_easy_starts_plain_with_the_same_request_and_says_so(self) -> None:
        job, judged = self.start("easy")
        self.assertEqual(job["engine"], "plain")
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual((call["engine"], call["easy_route"], call["model"], call["auth_route"], call["problem_text"]),
                         ("plain", True, "gpt-6.1-sol", "codex_subscription", "Show 1+1=2."))
        judged.assert_called_once_with("Show 1+1=2.")
        self.assertEqual(self.state(job)["home"], "math_harness")
        self.assertEqual(self.state(job)["turns"][0]["engine"], "plain")
        self.assertEqual(len(self.chat), 1)
        self.assertIn("this turn runs on Plain", self.chat[0][2])

    def test_hard_starts_cgm_and_remembers_its_home(self) -> None:
        job, _ = self.start("hard")
        self.assertEqual(job["engine"], "math_harness")
        self.assertEqual([c["engine"] for c in self.calls], ["math_harness"])
        self.assertNotIn("easy_route", self.calls[0])
        self.assertEqual(self.state(job)["turns"][0]["engine"], "math_harness")
        self.assertEqual(self.chat, [])

    def test_a_plain_start_refused_falls_back_to_cgm(self) -> None:
        for error in (ValueError("Plain would run on the sponsored Kimi service"),
                      jobs.account_policy.ProjectPolicyError("tool-free")):
            self.calls.clear()
            self.chat.clear()
            self.fail_plain = error
            job, _ = self.start("easy")
            self.assertEqual([c["engine"] for c in self.calls], ["plain", "math_harness"])
            self.assertEqual(job["engine"], "math_harness")
            self.assertEqual(self.chat, [])

    def test_other_errors_are_not_swallowed(self) -> None:
        self.fail_plain = RuntimeError("disk full")
        with self.assertRaises(RuntimeError):
            self.start("easy")

    def test_asking_for_cgm_or_no_text_skips_the_judge(self) -> None:
        job, judged = self.start("easy", problem_text="Use Ansatz CGM: prove 1+1=2.")
        judged.assert_not_called()
        self.assertEqual(job["engine"], "math_harness")
        self.assertEqual(self.state(job)["turns"][0]["why"], "asked")
        job, judged = self.start("easy", problem_text="", problem_id="erdos_224")
        judged.assert_not_called()
        self.assertEqual(job["engine"], "math_harness")

    def test_unrouted_starts_are_start_job_unchanged(self) -> None:
        for kw in ({"engine": "plain"}, {"engine": "claude"}, {"engine": "math_harness", "run_kind": "analyze"},
                   {"engine": "math_harness", "user": {"id": 9, "guest": True}}):
            self.calls.clear()
            user = kw.pop("user", None)
            job, judged = self.start("easy", user=user, **kw)
            judged.assert_not_called()
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.state(job), {})
        with patch.dict(os.environ, {easy_route.SWITCH_ENV: "0"}):
            self.calls.clear()
            job, judged = self.start("easy")
            judged.assert_not_called()
            self.assertEqual(job["engine"], "math_harness")
        from agent_monitor import cgm_max

        for kw in ({"auth_route": cgm_max.ROUTE}, {"model": cgm_max.MODEL_ID}, {"model": cgm_max.API_MODEL_ID}):
            self.calls.clear()
            judge = patch.object(easy_route, "judge")
            with judge as judged:
                job = jobs.start_job_routed(engine="math_harness", problem_text="Show 1+1=2.", user=self.ADMIN,
                                            **{"model": "gpt-6.1-sol", "auth_route": "codex_subscription", **kw})
            judged.assert_not_called()
            self.assertEqual([c["engine"] for c in self.calls], ["math_harness"], kw)

    def test_a_member_not_allowed_plain_or_another_model_stays_on_cgm(self) -> None:
        with patch.object(jobs.account_policy, "member_run_error", return_value="Plain is not available"):
            job, _ = self.start("easy", user=self.MEMBER)
        self.assertEqual([c["engine"] for c in self.calls], ["math_harness"])
        self.calls.clear()
        with patch.object(jobs.account_policy, "member_run_error", return_value=None), \
                patch.object(jobs.account_policy, "member_run_model", return_value="kimi-k3"):
            job, _ = self.start("easy", user=self.MEMBER)
        self.assertEqual([c["engine"] for c in self.calls], ["math_harness"])
        self.calls.clear()
        with patch.object(jobs.account_policy, "member_run_error", return_value=None), \
                patch.object(jobs.account_policy, "member_run_model", side_effect=lambda u, e, m: m):
            job, _ = self.start("easy", user=self.MEMBER)
        self.assertEqual([c["engine"] for c in self.calls], ["plain"])


class FollowUpTest(unittest.TestCase):
    """_easy_route_follow_up: the engine of each follow-up turn."""

    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        self.enterContext(patch.dict(os.environ, {easy_route.KEY_ENV: KEY}))
        os.environ.pop(easy_route.SWITCH_ENV, None)
        self.run_id = "math_harness_adhoc_fu"
        self.ws = root / "runs" / "workspaces" / self.run_id
        self.ws.mkdir(parents=True)
        jobs._write_run({"run_id": self.run_id, "engine": "math_harness", "status": "finished",
                         "problem_text": "Prove the theorem.", "problems": [{"text": "Prove the theorem.", "source": "initial"}],
                         "agents": [], "edges": []})
        easy_route.note_turn(self.ws, home="math_harness", engine="math_harness", verdict=None, why="judged")
        self.chat = []
        self.enterContext(patch.object(jobs, "_append_chat", side_effect=lambda rid, role, text: self.chat.append(text)))
        self.plain_ok_patch = patch.object(jobs, "_easy_plain_follow_up_ok", return_value=True)
        self.plain_ok = self.plain_ok_patch.start()
        self.addCleanup(patch.stopall)

    def turn(self, message, verdict="easy", **kw):
        with patch.object(easy_route, "judge", return_value={"verdict": verdict, "reason": "Short.", "model": "typesafe/jev-1.13", "error": ""}) as judged:
            data = jobs._easy_route_follow_up(self.run_id, jobs._load_run_record(self.run_id), self.ws,
                                              message=message, user={"id": 1, "is_admin": True}, model="gpt-6.1-sol", **kw)
        return data, judged

    def engine(self):
        return jobs._load_run_record(self.run_id)["engine"]

    def test_easy_hard_and_asked_turns_switch_the_engine_and_say_so(self) -> None:
        data, judged = self.turn("What is 2+2?", "easy")
        self.assertEqual((data["engine"], self.engine()), ("plain", "plain"))
        judged.assert_called_once_with("What is 2+2?", problem="Prove the theorem.")
        self.assertIn("this turn runs on Plain", self.chat[-1])
        data, _ = self.turn("keep trying!", "hard")
        self.assertEqual((data["engine"], self.engine()), ("math_harness", "math_harness"))
        self.assertIn("Ansatz CGM again, rated not easy", self.chat[-1])
        self.turn("2+3?", "easy")
        data, judged = self.turn("Now use Ansatz CGM on it", "easy")
        judged.assert_not_called()
        self.assertEqual(self.engine(), "math_harness")
        self.assertEqual(self.chat[-1], "This turn runs on Ansatz CGM again, as asked.")
        self.assertEqual([t["engine"] for t in easy_route.load(self.ws)["turns"]],
                         ["math_harness", "plain", "math_harness", "plain", "math_harness"])

    def test_the_same_engine_again_says_nothing(self) -> None:
        self.turn("keep trying!", "hard")
        self.assertEqual(self.chat, [])
        self.assertEqual(self.engine(), "math_harness")

    def test_continue_files_and_unrouted_runs_keep_their_engine(self) -> None:
        for message, kw in [("", {}), (None, {}), (jobs._BARE_CONTINUE_PREFIX + " please", {}), ("What is 2+2?", {"has_files": True})]:
            data, judged = self.turn(message, "easy", **kw)
            judged.assert_not_called()
            self.assertEqual(data["engine"], "math_harness")
        (self.ws / easy_route.STATE_FILE).unlink()
        data, judged = self.turn("What is 2+2?", "easy")
        judged.assert_not_called()
        self.assertEqual(self.engine(), "math_harness")
        self.assertEqual(self.chat, [])

    def test_plain_not_possible_keeps_cgm(self) -> None:
        self.plain_ok.return_value = False
        data, _ = self.turn("What is 2+2?", "easy")
        self.assertEqual((data["engine"], self.engine()), ("math_harness", "math_harness"))
        self.assertEqual(self.chat, [])

    def test_switched_off_or_failing_never_stops_the_turn(self) -> None:
        with patch.dict(os.environ, {easy_route.SWITCH_ENV: "off"}):
            data, judged = self.turn("What is 2+2?", "easy")
        judged.assert_not_called()
        with patch.object(easy_route, "judge", side_effect=RuntimeError("boom")):
            data = jobs._easy_route_follow_up(self.run_id, jobs._load_run_record(self.run_id), self.ws,
                                              message="What is 2+2?", user=None, model=None)
        self.assertEqual(data["engine"], "math_harness")

    def test_plain_on_the_runs_own_route_only(self) -> None:
        self.plain_ok_patch.stop()
        record = jobs._load_run_record(self.run_id)
        from agent_monitor import cgm_max

        for extra in ({"route_plan": {"advisor": {}, "worker": {}}}, {"auth_route": cgm_max.ROUTE},
                      {"live": {"auth_mode": cgm_max.ROUTE}}, {"model": cgm_max.API_MODEL_ID}):
            self.assertFalse(jobs._easy_plain_follow_up_ok(self.run_id, {**record, **extra}, None, "gpt-6.1-sol"), extra)
        for max_model in (cgm_max.MODEL_ID, cgm_max.API_MODEL_ID):
            self.assertFalse(jobs._easy_plain_follow_up_ok(self.run_id, record, None, max_model), max_model)
        with patch.object(jobs, "_continuation_auth_environment", side_effect=ValueError("route")):
            self.assertFalse(jobs._easy_plain_follow_up_ok(self.run_id, record, None, "gpt-6.1-sol"))
        with patch.object(jobs, "_continuation_auth_environment", return_value=({}, "codex_subscription")), \
                patch.object(jobs, "_tool_free_follow_up", return_value=False):
            self.assertTrue(jobs._easy_plain_follow_up_ok(self.run_id, record, {"id": 1, "is_admin": True}, "gpt-6.1-sol"))
            with patch.object(jobs, "_member_continuation_model", side_effect=lambda u, e, r, m: "kimi-k3" if e == "plain" else m):
                self.assertFalse(jobs._easy_plain_follow_up_ok(self.run_id, record, None, "gpt-6.1-sol"))


class KeyIsolationTest(unittest.TestCase):
    def test_the_judges_key_never_reaches_a_child_process(self) -> None:
        with patch.dict(os.environ, {easy_route.KEY_ENV: KEY}):
            self.assertNotIn(easy_route.KEY_ENV, subprocess_env.child_process_env())
            self.assertNotIn(easy_route.KEY_ENV, subprocess_env.child_process_env(extra={easy_route.KEY_ENV: KEY}))
        env = {easy_route.KEY_ENV: KEY, easy_route.URL_ENV: "http://127.0.0.1:1/x", easy_route.MODEL_ENV: "m",
               easy_route.SWITCH_ENV: "1", "PATH": "/bin"}
        subprocess_env.scrub_control_plane_env(env)
        self.assertEqual(env, {"PATH": "/bin"})

    def test_it_is_not_a_provider_key_anything_else_reads(self) -> None:
        import subprocess

        root = Path(__file__).resolve().parents[1] / "agent_monitor"
        hits = subprocess.run(["grep", "-rl", "--include=*.py", "--include=*.js", "--include=*.html", easy_route.KEY_ENV, str(root)],
                              capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(Path(h).name for h in hits), ["easy_route.py", "subprocess_env.py"])

    def test_latex_compiles_without_server_secrets(self) -> None:
        import inspect

        from agent_monitor import console_server

        source = inspect.getsource(console_server._compile_workspace_pdf)
        self.assertEqual(source.count("env=tex_env"), 2)
        self.assertIn("tex_env = child_process_env()", source)


class WiringTest(unittest.TestCase):
    def test_the_proof_start_and_both_follow_up_paths_route(self) -> None:
        import inspect

        from agent_monitor import console_server

        self.assertIn("job_manager.start_job_routed(", inspect.getsource(console_server))
        claimed = inspect.getsource(jobs._continue_claimed_run)
        with_message = inspect.getsource(jobs._continue_with_message)
        for source in (claimed, with_message):
            self.assertLess(source.index("_easy_route_follow_up("), source.index('engine = run_data.get("engine")'))
        self.assertIn("has_files=bool(user_entry.get(\"attachments\"))", with_message)
        start = inspect.getsource(jobs.start_job)
        self.assertLess(start.index("if easy_route and sponsored_kimi:"), start.index("_launch_run(") if "_launch_run(" in start else len(start))


if __name__ == "__main__":
    unittest.main()


class SponsoredRefusalTest(unittest.TestCase):
    """The real start_job: an easy Plain start never lands on the sponsored service."""

    def test_refused_before_anything_is_written(self) -> None:
        from agent_monitor import sponsored_kimi_client

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        self.enterContext(patch.object(sponsored_kimi_client, "configured", return_value=True))
        self.enterContext(patch.object(sponsored_kimi_client, "supports_engine", return_value=True))
        self.enterContext(patch.object(sponsored_kimi_client, "endpoint", return_value="http://127.0.0.1:9/v1"))
        for name in ("KIMI_API_KEY", "KIMI_API_BASE", "KIMI_BASE_URL"):
            self.enterContext(patch.dict(os.environ, {name: ""}))
        admin = {"id": 1, "is_admin": True, "email": "admin@example.test"}
        with patch.object(jobs.threading, "Thread") as thread:
            with self.assertRaisesRegex(ValueError, "sponsored Kimi service"):
                jobs.start_job(engine="plain", problem_text="Show 1+1=2.", model="kimi-k3", user=admin,
                               auth_route="api_key", easy_route=True)
            thread.assert_not_called()
        self.assertFalse((root / "runs" / "workspaces").exists() and any((root / "runs" / "workspaces").iterdir()))
