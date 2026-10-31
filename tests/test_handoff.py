import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import syndicate_runtime as rt
from test_syndicate import sy, ProjectCase


class Quotas(unittest.TestCase):
    def test_claude_windows_not_context_tokens(self):
        data = rt.normalize("claude", {"context_window": {"remaining_percentage": 2},
            "rate_limits": {"five_hour": {"used_percentage": 90, "resets_at": 200},
                            "seven_day": {"used_percentage": 30, "resets_at": 300}}}, now=100)
        self.assertEqual(rt.availability(data, 101), ("current", 10))
        self.assertEqual(rt.normalize("claude", {"context_window": {"remaining_percentage": 50}})["windows"], [])

    def test_codex_all_buckets_and_zero(self):
        data = rt.normalize("codex", {"rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 40, "resetsAt": 900}},
            "model": {"secondary": {"usedPercent": 100, "resetsAt": 1000}}}}, now=100)
        self.assertEqual(rt.availability(data, 101), ("current", 0))

    def test_invalid_values_unknown_not_available(self):
        for value in (None, True, "90", float("nan"), -1, 101):
            data = rt.normalize("claude", {"rate_limits": {"five_hour": {"used_percentage": value}}})
            self.assertEqual(rt.availability(data), ("unknown", None))

    def test_stale_and_due_are_not_available(self):
        data = rt.normalize("codex", {"rateLimits": {"primary": {"usedPercent": 90, "resetsAt": 200}}}, now=100)
        self.assertEqual(rt.availability(data, 200), ("awaiting reset confirmation", None))
        self.assertEqual(rt.availability(data, 500), ("stale", None))

    def test_reset_notices_require_evidence_and_are_deduplicated(self):
        old = rt.normalize("claude", {"rate_limits": {"five_hour": {"used_percentage": 95, "resets_at": 200}}}, now=100)
        notices = {}
        self.assertEqual(rt.reset_events(old, old, notices, 199), [])
        self.assertIn("expected", rt.reset_events(old, old, notices, 200)[0])
        self.assertEqual(rt.reset_events(old, old, notices, 201), [])
        new = rt.normalize("claude", {"rate_limits": {"five_hour": {"used_percentage": 3, "resets_at": 400}}}, now=202)
        self.assertIn("confirmed", rt.reset_events(old, new, notices, 202)[0])
        self.assertEqual(rt.reset_events(old, new, notices, 203), [])

    def test_missing_observation_preserves_last_known(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(rt, "STATE", Path(tmp)):
            rt.save_observation("claude", {"rate_limits": {"five_hour": {"used_percentage": 90}}})
            rt.save_observation("claude", {})
            self.assertEqual(rt.read_json(Path(tmp) / "claude.json")["windows"][0]["remaining_percent"], 10)

    def test_confirmation_explicit_and_noninteractive_safe(self):
        with patch("builtins.input", return_value="codex") as ask, patch("sys.stdout", new=io.StringIO()):
            self.assertIsNone(rt.choose_target("claude", False))
            ask.assert_not_called()
            self.assertEqual(rt.choose_target("claude", True), "codex")
        for answer in ("", "yes", "stop"):
            with patch("builtins.input", return_value=answer), patch("sys.stdout", new=io.StringIO()):
                self.assertIsNone(rt.choose_target("claude", True))

    def test_monitor_reports_empty_response_then_recovers(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(rt, "STATE", Path(tmp)), \
             patch.object(rt.shutil, "which", return_value="codex"), \
             patch.object(rt, "codex_limits", return_value={}) as limits:
            rt.monitor_tick()
            self.assertIn("returned no account limits", rt.describe_limits())
            self.assertIn("interactive Claude session", rt.describe_limits())
            limits.return_value = {"rateLimits": {"primary": {"usedPercent": 6}}}
            rt.monitor_tick()
            self.assertIn("94% remaining", rt.describe_limits())
            self.assertNotIn("codex_error", rt.read_json(Path(tmp) / "monitor.json"))


class Handoffs(ProjectCase):
    def setUp(self):
        super().setUp()
        self.state_patch = patch.object(rt, "STATE", self.project / "limits-state")
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def test_snapshot_preserves_current_and_semantic_checkpoint(self):
        local = self.project / ".syndicate"
        (local / "checkpoint.md").write_text("Goal: fix the bug. Review pending.")
        status = {"state": "running", "current": {"summary": "testing"}, "history": [{"outcome": "done"}]}
        (local / "status.json").write_text(json.dumps(status))
        path = rt.create_handoff(self.project, "claude", "Fix bug", "10% remaining", "test")
        self.assertEqual(rt.read_json(path / "status.json"), status)
        self.assertIn("Review pending", (path / "checkpoint.md").read_text())
        self.assertEqual(rt.read_json(local / "status.json")["state"], "waiting_on_user")
        self.assertEqual(rt.read_json(local / "status.json")["history"], status["history"])
        self.assertIn("Fix bug", (path / "handoff.md").read_text())

    def test_low_preflight_never_launches_cli(self):
        rt.save_observation("claude", {"rate_limits": {"five_hour": {"used_percentage": 90}}})
        with patch.object(rt.subprocess, "Popen") as launch, patch.object(rt, "create_handoff", return_value=Path("snapshot")):
            result, path = rt.supervise(["false"], self.project, {"claude"}, 10, "task", "claude")
        launch.assert_not_called()
        self.assertEqual(result, 75)
        self.assertEqual(path, Path("snapshot"))

    def test_reserve_validation_and_skill_injection(self):
        self.config("handoff-reserve = 20\n")
        team = sy.resolve_team(self.project)
        self.assertEqual(team.config["handoff-reserve"], "20")
        self.assertIn("checkpoint.md", sy.orchestrator_body(team))
        for invalid in ("NaN", "100", "0", "-1", "nope"):
            self.config(f"handoff-reserve = {invalid}\n")
            with self.assertRaises(sy.SyndicateError):
                sy.resolve_team(self.project)

    def test_claude_statusline_wired_without_global_edits(self):
        argv = sy.build_claude(sy.resolve_team(self.project), False, [])
        settings = rt.read_json(Path(argv[argv.index("--settings") + 1]))
        self.assertIn("_claude-usage", settings["statusLine"]["command"])
        self.assertIn("Stop", settings["hooks"])

    def test_switch_maps_every_role_and_loads_handoff(self):
        self.config("default = claude\n")
        path = rt.create_handoff(self.project, "claude", "goal", "low quota", "source")
        args = sy.build_parsers()[0].parse_args(["-x", "codex", "--all-runtime", "codex", "-C", str(self.project), "--handoff", str(path)])
        with patch.object(sy, "require_clis") as require, patch.object(sy, "register_project"), \
             patch.object(rt, "start_monitor"), patch.object(rt, "supervise", return_value=(0, None)) as run:
            self.assertEqual(sy.cmd_run(args), 0)
        require.assert_called_once_with({"codex"})
        self.assertEqual(run.call_args.args[2], {"codex"})
        self.assertIn(str(path / "handoff.md"), run.call_args.args[4])

    def test_existing_project_lock_blocks_second_session(self):
        import fcntl
        with (self.project / ".syndicate/session.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            args = sy.build_parsers()[0].parse_args(["-C", str(self.project), "task"])
            with patch.object(sy, "REGISTRY_PATH", self.project / "registry" / "projects.json"):
                with self.assertRaisesRegex(sy.SyndicateError, "already owns"):
                    sy.cmd_run(args)


class ProcessControl(unittest.TestCase):
    def test_stop_includes_worker_after_leader_exit(self):
        # Real child process group; no provider, network, credentials, or tokens.
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "worker"
            code = ("import subprocess,sys; "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                    "open(sys.argv[1],'w').write(str(p.pid))")
            leader = subprocess.Popen([sys.executable, "-c", code, str(marker)], start_new_session=True, stderr=subprocess.DEVNULL)
            try:
                leader.wait(timeout=5)
                self.assertTrue(marker.exists())
                rt.stop_group(leader)
                pid = int(marker.read_text())
                # Linux may retain a reparented zombie until init reaps it.
                stat = Path(f"/proc/{pid}/stat")
                if stat.exists():
                    self.assertEqual(stat.read_text().split()[2], "Z")
            finally:
                rt.signal_group(leader.pid, signal.SIGKILL)


class RuntimeIntegration(ProjectCase):
    def setUp(self):
        super().setUp()
        self.state = self.project / "limit-cache"
        self.patcher = patch.object(rt, "STATE", self.state)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        self.err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        self.addCleanup(self.out.close)
        self.addCleanup(self.err.close)

    def run_cli(self, code):
        with patch("sys.stdout", self.out), patch("sys.stderr", self.err):
            return rt.supervise([sys.executable, "-c", code], self.project,
                                {"claude"}, 10, "Finish feature", "claude", headless=True)

    def test_headless_output_and_exit_status_preserved(self):
        rt.save_observation("claude", {"rate_limits": {"five_hour": {"used_percentage": 20}}})
        code, handoff = self.run_cli("import sys; print('result'); print('diagnostic', file=sys.stderr)")
        self.assertEqual(code, 0)
        self.assertIsNone(handoff)
        self.assertEqual(self.out.buffer.getvalue(), b"result\n")
        self.assertEqual(self.err.buffer.getvalue(), b"diagnostic\n")
        logs = list((self.project / ".syndicate/runs").glob("*.log"))
        self.assertIn(b"result", logs[0].read_bytes())

    def test_live_threshold_crossing_stops_and_snapshots(self):
        rt.save_observation("claude", {"rate_limits": {"five_hour": {"used_percentage": 20}}})
        observation = rt.normalize("claude", {"rate_limits": {"five_hour": {"used_percentage": 90}}})
        code = ("import time,pathlib; time.sleep(.1); "
                f"pathlib.Path({str(self.state / 'claude.json')!r}).write_text({json.dumps(observation)!r}); "
                "time.sleep(30)")
        result, handoff = self.run_cli(code)
        self.assertIsNotNone(handoff)
        self.assertIn("10%", (handoff / "handoff.md").read_text())
        self.assertNotEqual(result, 0)

    def test_unknown_usage_interrupts_without_claiming_exhaustion(self):
        with patch.object(rt, "UNKNOWN_GRACE_SECONDS", 0):
            _, handoff = self.run_cli("import time; time.sleep(30)")
        self.assertIn("usage unknown", (handoff / "handoff.md").read_text())

    def test_unexpected_failure_gets_recoverable_snapshot(self):
        _, handoff = self.run_cli("raise SystemExit(4)")
        self.assertIn("status 4", (handoff / "handoff.md").read_text())

class Protocol(unittest.TestCase):
    def test_codex_handshake_with_fake_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "codex"
            binary.write_text(f"#!{sys.executable}\n" + '''import json, sys
request = json.loads(sys.stdin.readline())
assert request['method'] == 'initialize'
print(json.dumps({'id': request['id'], 'result': {}}), flush=True)
assert json.loads(sys.stdin.readline())['method'] == 'initialized'
request = json.loads(sys.stdin.readline())
assert request['method'] == 'account/rateLimits/read'
print(json.dumps({'method': 'account/updated', 'params': {}}), flush=True)
print(json.dumps({'id': request['id'], 'result': {'rateLimits': {'primary': {'usedPercent': 91}}}}), flush=True)
sys.stdin.read()
''')
            binary.chmod(0o700)
            with patch.dict(os.environ, {"PATH": tmp + os.pathsep + os.environ["PATH"]}):
                result = rt.codex_limits(timeout=2)
            self.assertEqual(rt.normalize("codex", result)["windows"][0]["remaining_percent"], 9)

    def test_codex_reader_timeout_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "codex"
            binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
            binary.chmod(0o700)
            with patch.dict(os.environ, {"PATH": tmp + os.pathsep + os.environ["PATH"]}):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    rt.codex_limits(timeout=.05)

    def test_monitor_reset_deduplication_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(rt, "STATE", Path(tmp)), \
                patch.object(rt.shutil, "which", return_value=None):
            old = rt.normalize("claude", {"rate_limits": {"five_hour": {"used_percentage": 95, "resets_at": 200}}}, now=100)
            rt.atomic_json(Path(tmp) / "claude.json", old)
            with patch.object(rt.time, "time", return_value=201):
                self.assertEqual(len(rt.monitor_tick()), 1)
                self.assertEqual(rt.monitor_tick(), [])
                new = rt.normalize("claude", {"rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 400}}})
                rt.atomic_json(Path(tmp) / "claude.json", new)
                self.assertIn("confirmed", rt.monitor_tick()[0])
                self.assertEqual(rt.monitor_tick(), [])
            self.assertEqual(len((Path(tmp) / "notifications.log").read_text().splitlines()), 2)

@unittest.skipUnless(Path('/proc').is_dir(), 'Linux process identity tracking')
class DetachedWorkers(unittest.TestCase):
    def test_tracked_detached_worker_is_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'worker'
            code = ("import subprocess,sys,time; "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True); "
                    "open(sys.argv[1],'w').write(str(p.pid)); time.sleep(30)")
            leader = subprocess.Popen([sys.executable, '-c', code, str(marker)],
                                      start_new_session=True, stderr=subprocess.DEVNULL)
            known = {}
            try:
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(marker.exists())
                pid = int(marker.read_text())
                known = rt.descendants(leader.pid)
                self.assertIn(pid, known)
                rt.stop_group(leader, known)
                stat = Path(f'/proc/{pid}/stat')
                if stat.exists():
                    self.assertEqual(stat.read_text().split()[2], 'Z')
            finally:
                rt.signal_group(leader.pid, signal.SIGKILL)
                leader.wait()
                for pid, start in known.items():
                    identity = rt.process_identity(pid)
                    if identity and identity[1] == start:
                        os.kill(pid, signal.SIGKILL)

class TerminalIntegration(unittest.TestCase):
    def test_interactive_cli_receives_terminal_and_input(self):
        import pty
        import select
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            pid, master = pty.fork()
            if pid == 0:
                try:
                    rt.STATE = project / 'limits'
                    rt.save_observation('claude', {'rate_limits': {'five_hour': {'used_percentage': 20}}})
                    cli = "import sys; assert sys.stdin.isatty(); print('CLI_READY', flush=True); print('ANSWER='+input(), flush=True)"
                    result, handoff = rt.supervise([sys.executable, '-c', cli], project,
                                                  {'claude'}, 10, 'task', 'claude')
                    os._exit(0 if result == 0 and handoff is None else 1)
                except BaseException:
                    os._exit(2)
            output = b''
            sent = False
            status = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    readable, _, _ = select.select([master], [], [], .1)
                    if readable:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError:
                            chunk = b''
                        output += chunk
                        if b'CLI_READY' in output and not sent:
                            os.write(master, b'hello\n')
                            sent = True
                    waited, child_status = os.waitpid(pid, os.WNOHANG)
                    if waited:
                        status = child_status
                        break
                self.assertIsNotNone(status, 'supervisor did not exit')
                self.assertEqual(os.waitstatus_to_exitcode(status), 0, output)
                self.assertIn(b'ANSWER=hello', output)
            finally:
                if status is None:
                    os.kill(pid, signal.SIGTERM)
                    os.waitpid(pid, 0)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
