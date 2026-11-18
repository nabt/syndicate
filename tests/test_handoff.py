import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
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

    def test_chief_uses_recorded_usage_without_claiming_fresh_capacity(self):
        for provider, payload in (
            ("claude", {"rate_limits": {"five_hour": {"used_percentage": 90, "resets_at": 200}}}),
            ("codex", {"rateLimits": {"primary": {"usedPercent": 90, "resetsAt": 200}}}),
        ):
            observation = rt.normalize(provider, payload, now=100)
            for now in (101, 200, 500):
                with self.subTest(provider=provider, now=now):
                    self.assertEqual(rt.supervision_usage(observation, chief=True, now=now), ("last observed", 10))
                    # Project sessions now enforce the same latest real reading;
                    # the freshness state is still reported for warnings.
                    self.assertEqual(rt.supervision_usage(observation, now=now),
                                     (rt.availability(observation, now)[0], 10))
        self.assertEqual(rt.supervision_usage({}, chief=True), ("last observed", None))
        self.assertEqual(rt.supervision_usage({}), ("unknown", None))

    def test_usage_warning_names_state_and_last_reading(self):
        observation = rt.normalize("claude", {"rate_limits": {"five_hour": {"used_percentage": 60}}}, now=100)
        self.assertIsNone(rt.usage_warning("claude", "current", observation))
        message = rt.usage_warning("claude", "stale", observation)
        self.assertIn("claude usage telemetry is stale", message)
        self.assertIn("40% remaining", message)
        self.assertIn("enforcing the reserve with the last real reading", message)
        self.assertIn("no codex usage reading yet", rt.usage_warning("codex", "unknown", {}))

    def test_malformed_telemetry_reads_as_unknown(self):
        bad = [{"remaining_percent": value} for value in (True, "50", float("nan"), float("inf"), -1, 101, None)]
        for observation in ({"windows": bad}, {"windows": "x"}, {"windows": [1, None, "w"]}, {"windows": None},
                            {"observed_at": float("nan"), "windows": bad}, []):
            with self.subTest(observation=str(observation)[:50]):
                self.assertIsNone(rt.latest_reading(observation))
                self.assertEqual(rt.availability(observation if isinstance(observation, dict) else {}, 100),
                                 ("unknown", None))
        mixed = {"observed_at": 100, "windows": bad + [{"name": "ok", "remaining_percent": 42}]}
        self.assertEqual(rt.latest_reading(mixed), 42)
        self.assertEqual(rt.availability(mixed, 101), ("current", 42))
        for observed in (float("nan"), float("inf"), 1e300, -5, "noon", True, None):
            with self.subTest(observed=observed):
                observation = {"observed_at": observed, "windows": [{"name": "w", "remaining_percent": 42,
                                                                     "resets_at": observed}]}
                self.assertEqual(rt.availability(observation, 101), ("stale", None))
                self.assertIn("unknown time", rt.usage_warning("claude", "stale", observation))
                self.assertIsNone(rt.observed_time(observation))
                self.assertEqual(rt.reset_events(observation, observation, {}, 10**9), [])
        with tempfile.TemporaryDirectory() as tmp, patch.object(rt, "STATE", Path(tmp)):
            rt.atomic_json(Path(tmp) / "claude.json", {"observed_at": 1e300, "windows": [
                {"name": "w", "remaining_percent": 42, "resets_at": 1e300}, "junk"]})
            self.assertIn("42% remaining; reset unknown", rt.describe_limits())

    def test_huge_integers_and_deep_nesting_read_as_malformed(self):
        # Regression (security LOW-1): math.isfinite raises OverflowError for
        # ints beyond float range; the range check must come first.
        huge = 10**400
        for value in (huge, -huge):
            with self.subTest(sign=value > 0):
                self.assertIsNone(rt.window("w", value, value))
                self.assertEqual(rt.window("w", 40, value)["resets_at"], None)
                observation = {"observed_at": value, "windows": [
                    {"name": "w", "remaining_percent": value, "resets_at": value}]}
                self.assertIsNone(rt.latest_reading(observation))
                self.assertEqual(rt.supervision_usage(observation, now=100), ("unknown", None))
                self.assertEqual(rt.supervision_usage(observation, chief=True), ("last observed", None))
                self.assertEqual(rt.reset_events(observation, observation, {}, 10**9), [])
                good = {"observed_at": value, "windows": [{"name": "w", "remaining_percent": 42,
                                                           "resets_at": value}]}
                self.assertEqual(rt.supervision_usage(good, now=100), ("stale", 42))
                self.assertIsNone(rt.observed_time(good))
                self.assertIn("unknown time", rt.usage_warning("claude", "stale", good))
        self.assertIsNone(rt.normalize("claude", {"rate_limits": {"five_hour": {"used_percentage": huge}}})
                          ["windows"] or None)
        # Windows need string names; reset_events never keys on other types.
        named = {"observed_at": 100, "windows": [{"name": ["x"], "remaining_percent": 5, "resets_at": 50},
                                                 {"name": 7, "remaining_percent": 6, "resets_at": 50}]}
        self.assertIsNone(rt.latest_reading(named))
        self.assertEqual(rt.reset_events(named, named, {}, 10**9), [])
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "nested.json"
            nested.write_text("[" * 100000 + "]" * 100000)
            self.assertEqual(rt.read_json(nested), {})
            raw = Path(tmp) / "huge.json"
            raw.write_text('{"observed_at": %d, "windows": [{"name": "w", "remaining_percent": %d}]}'
                           % (huge, huge))
            self.assertEqual(rt.supervision_usage(rt.read_json(raw), now=100), ("unknown", None))

    def test_delegated_trigger_does_not_offer_the_exhausted_provider(self):
        def ask(answer, interactive=True, **options):
            out = io.StringIO()
            prompt = patch("builtins.input", side_effect=answer) if isinstance(answer, type) else \
                patch("builtins.input", return_value=answer)
            with prompt as asked, patch("sys.stdout", out):
                return rt.choose_target("codex", interactive, **options), out.getvalue(), asked
        target, text, _ = ask("claude", limited="claude")
        self.assertIsNone(target)  # the exhausted delegated provider is not a choice
        self.assertIn("claude reached the usage reserve while a delegated specialist was running", text)
        self.assertIn("continue with codex for every role", text)
        self.assertNotIn("continue with claude", text)
        self.assertEqual(ask("codex", limited="claude")[0], "codex")
        for answer in ("", "yes", EOFError, KeyboardInterrupt):
            with self.subTest(answer=answer):
                self.assertIsNone(ask(answer, limited="claude")[0])
        target, text, asked = ask("codex", interactive=False, limited="claude")
        self.assertIsNone(target)
        asked.assert_not_called()
        self.assertIn("leaving work stopped", text)
        # The host itself (or an unknown trigger) keeps the original offer.
        for limited in ("codex", None, "gemini"):
            with self.subTest(limited=limited):
                target, text, _ = ask("claude", limited=limited)
                self.assertEqual(target, "claude")
                self.assertIn("continue with claude (or use only codex)", text)

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

    def test_chief_stale_low_preflight_never_launches_even_after_reset(self):
        observation = rt.normalize("claude", {"rate_limits": {
            "five_hour": {"used_percentage": 91, "resets_at": 200}}}, now=100)
        rt.atomic_json(rt.STATE / "claude.json", observation)
        with patch.object(rt.subprocess, "Popen") as launch, patch.object(rt, "create_handoff", return_value=Path("snapshot")):
            result, path = rt.supervise(["false"], self.project, {"claude"}, 10,
                                        "task", "claude", headless=True, chief=True)
        launch.assert_not_called()
        self.assertEqual((result, path), (75, Path("snapshot")))

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
        args = sy.build_parsers()[0].parse_args(["-x", "codex", "--all-runtime", "codex", "--project", str(self.project), "--handoff", str(path)])
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
            args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "task"])
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

    def test_usage_policy_in_running_loop(self):
        # Advance the supervisor clock beyond the real grace interval without
        # making the child wait minutes. The child exits successfully if kept alive.
        for chief in (True, False):
            for condition in ("stale", "unknown", "awaiting reset confirmation", "low"):
                with self.subTest(chief=chief, condition=condition):
                    observed = 100 if condition == "stale" else 1000
                    observation = {} if condition == "unknown" else rt.normalize("claude", {
                        "rate_limits": {"five_hour": {"used_percentage": 20,
                            "resets_at": 900 if condition == "awaiting reset confirmation" else 999999}}}, now=observed)
                    rt.atomic_json(self.state / "claude.json", observation)
                    def started(pid):
                        if condition == "low":
                            # The new low reading must stop the running CLI,
                            # even though it is already stale and reset is due.
                            rt.atomic_json(self.state / "claude.json", rt.normalize("claude", {
                                "rate_limits": {"five_hour": {"used_percentage": 90,
                                    "resets_at": 200}}}, now=100) if chief else rt.normalize("claude", {
                                "rate_limits": {"five_hour": {"used_percentage": 90}}}, now=1000))
                    clock = iter(range(1000, 10000, 61))
                    with patch.object(rt, "clock", lambda: next(clock)), \
                         patch("sys.stdout", self.out), patch("sys.stderr", self.err):
                        result, handoff = rt.supervise([sys.executable, "-c", "import time; time.sleep(0.8)"],
                            self.project, {"claude"}, 10, "test policy", "claude",
                            headless=True, chief=chief, on_start=started)
                    # Project sessions no longer stop on stale, unknown, or
                    # awaiting-reset telemetry (previously they did after 60s);
                    # like the Chief, they enforce the latest real reading.
                    if condition != "low":
                        self.assertEqual(result, 0)
                        self.assertIsNone(handoff)
                    else:
                        self.assertIsNotNone(handoff)
                        reason = (handoff / "handoff.md").read_text()
                        self.assertIn("reserve", reason)

    def test_supervised_cli_inherits_chief_lock(self):
        rt.save_observation("claude", {"rate_limits": {"five_hour": {"used_percentage": 20}}})
        with sy.chief_session_lock() as lock:
            inode = os.fstat(lock.fileno()).st_ino
            code = f"import os; assert os.fstat({lock.fileno()}).st_ino == {inode}"
            with patch("sys.stdout", self.out), patch("sys.stderr", self.err):
                result, handoff = rt.supervise([sys.executable, '-c', code], self.project,
                    {'claude'}, 10, 'Check inherited lock', 'claude', headless=True,
                    pass_fds=(lock.fileno(),))
        self.assertEqual(result, 0)
        self.assertIsNone(handoff)

    def test_on_start_gets_child_pid_and_cannot_orphan_the_cli(self):
        rt.save_observation("claude", {"rate_limits": {"five_hour": {"used_percentage": 20}}})
        marker = self.project / "child-pid"

        def supervise(code, on_start):
            with patch("sys.stdout", self.out), patch("sys.stderr", self.err):
                return rt.supervise([sys.executable, "-c", code], self.project, {"claude"}, 10,
                                    "Finish feature", "claude", headless=True, on_start=on_start)

        def gone(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False

        def kill_leftover(pid):
            if not gone(pid):
                os.kill(pid, signal.SIGKILL)
        seen = []
        code, handoff = supervise(f"import os,pathlib; pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))",
                                  seen.append)
        self.assertEqual((code, handoff), (0, None))
        self.assertEqual(seen, [int(marker.read_text())])
        # A failure to record the owner is reported; the CLI keeps running to completion.
        code, handoff = supervise("pass", lambda pid: (_ for _ in ()).throw(RuntimeError("store down")))
        self.assertEqual((code, handoff), (0, None))
        self.err.flush()
        self.assertIn(b"could not record the CLI owner", self.err.buffer.getvalue())
        for error in (KeyboardInterrupt, SystemExit):
            pids = []

            def interrupted(pid):
                pids.append(pid)
                self.addCleanup(kill_leftover, pid)
                raise error()
            started = time.monotonic()
            if error is KeyboardInterrupt:
                _, handoff = supervise("import time; time.sleep(30)", interrupted)
                self.assertIn("Interrupted by user", (handoff / "handoff.md").read_text())
            else:
                with self.assertRaises(SystemExit):
                    supervise("import time; time.sleep(30)", interrupted)
            self.assertLess(time.monotonic() - started, 15)
            self.assertEqual(len(pids), 1)
            self.assertTrue(gone(pids[0]), f"{error.__name__} in on_start left the CLI running")

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

    def test_unknown_host_usage_warns_without_stopping(self):
        # Replaces the former 60-second unknown-usage interruption: a Claude
        # host is its own telemetry source, so stopping would prevent bootstrap.
        result, handoff = self.run_cli("import time; time.sleep(1.5)")
        self.assertEqual((result, handoff), (0, None))
        self.err.flush()
        errors = self.err.buffer.getvalue().decode()
        self.assertEqual(errors.count("no claude usage reading yet"), 1)

    def test_unexpected_failure_gets_recoverable_snapshot(self):
        _, handoff = self.run_cli("raise SystemExit(4)")
        self.assertIn("status 4", (handoff / "handoff.md").read_text())

BIN = str(Path(rt.__file__).parent)
OTHER_RUN = "20000101T000000-deadbeef"


def ticking(start=10**6, step=61):
    """A module-local supervisor clock advancing in large steps."""
    ticks = iter(range(start, start + 10**8, step))
    return lambda: next(ticks)


def namespace_prefix():
    """A command prefix running a process in a new PID namespace, or None."""
    candidates = []
    if shutil.which("unshare"):
        candidates.append(["unshare", "-Upf", "--map-root-user"])
    if shutil.which("codex"):
        candidates.append(["codex", "sandbox", "-c", 'sandbox_mode="workspace-write"', "--"])
    for prefix in candidates:
        try:
            probe = subprocess.run([*prefix, sys.executable, "-c", "import os; print(os.getpid())"],
                                   capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0 and probe.stdout.strip().isdigit() and int(probe.stdout) != os.getpid():
            if int(probe.stdout) < 10:  # a fresh namespace numbers its processes from 1
                return prefix
    return None


class ProjectUsagePolicy(ProjectCase):
    """Host enforcement with the latest real reading; delegated providers via gate and lease."""

    def setUp(self):
        super().setUp()
        self.state = rt.STATE  # ProjectCase patches it into the temporary project
        self.err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        self.out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        self.addCleanup(self.err.close)
        self.addCleanup(self.out.close)

    def reading(self, provider, used, observed_at):
        self.usage(provider, used, observed_at=observed_at)

    def supervise(self, code, providers, host, on_start=None, clock=True):
        patches = [patch("sys.stdout", self.out), patch("sys.stderr", self.err)]
        if clock:
            patches.append(patch.object(rt, "clock", clock if callable(clock) else ticking()))
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            return rt.supervise([sys.executable, "-c", code], self.project, providers, 10,
                                "task", host, headless=True, on_start=on_start)

    def errors(self):
        self.err.flush()
        return self.err.buffer.getvalue().decode()

    def test_incident_codex_host_run_session_ignores_stale_or_unknown_claude(self):
        # The incident: a Codex host whose specialists default to Claude was
        # stopped because Claude telemetry was stale or unknown. Drive the real
        # run_session -> supervise path with a stand-in Codex CLI. The former
        # unknown-usage grace (UNKNOWN_GRACE_SECONDS, removed) is forced to 0,
        # so the old policy would hand off on the first tick.
        self.config("orchestrator = codex\n")
        self.reading("codex", 20, observed_at=None)
        argv = [sys.executable, "-c", "import time; time.sleep(1.5)"]
        for claude in ("stale", "unknown"):
            with self.subTest(claude=claude):
                (self.state / "claude.json").unlink(missing_ok=True)
                if claude == "stale":
                    self.reading("claude", 50, observed_at=100)
                args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "-p", "task"])
                with patch.object(sy, "require_clis"), patch.object(sy, "register_project"), \
                     patch.object(rt, "start_monitor"), patch.object(sy, "build_codex", return_value=argv), \
                     patch.object(rt, "UNKNOWN_GRACE_SECONDS", 0, create=True), \
                     patch.object(rt, "choose_target", return_value=None), \
                     patch("sys.stderr", self.err), patch("sys.stdout", self.out):
                    self.assertEqual(sy.cmd_run(args), 0)
                self.assertFalse((self.project / ".syndicate" / "handoffs").exists())
        self.assertIn("monitoring codex account limits", self.errors())

    def test_host_stale_reading_above_reserve_continues_with_one_warning(self):
        self.reading("claude", 50, observed_at=100)
        result, handoff = self.supervise("import time; time.sleep(1.5)", {"claude"}, "claude")
        self.assertEqual((result, handoff), (0, None))
        self.assertEqual(self.errors().count("claude usage telemetry is stale"), 1)

    def test_warning_once_per_state_change(self):
        # stale -> current -> stale: two warnings, none while current.
        self.reading("claude", 50, observed_at=10**6)
        def times():
            yield 10**6 + 1000  # preflight: stale
            for step in range(4):
                yield 10**6 + 10 + step * 10  # current
            value = 10**6 + 5000  # stale again
            while True:
                value += 10
                yield value
        sequence = times()
        result, handoff = self.supervise("import time; time.sleep(2.5)", {"claude"}, "claude",
                                         clock=lambda: next(sequence))
        self.assertEqual((result, handoff), (0, None))
        self.assertEqual(self.errors().count("claude usage telemetry is stale"), 2)

    def test_host_stale_reading_at_reserve_still_hands_off(self):
        self.reading("claude", 90, observed_at=100)
        marker = self.project / "launched"
        result, handoff = self.supervise(f"open({str(marker)!r}, 'w')", {"claude"}, "claude", clock=False)
        self.assertFalse(marker.exists())
        self.assertEqual(result, 75)
        self.assertIn("10% account usage remaining (reserve 10%); no CLI launched",
                      (handoff / "handoff.md").read_text())
        self.assertIn("syndicate limits --forget claude", self.errors())
        # Mid-run: a stale low reading recorded after launch also stops the run.
        self.reading("claude", 50, observed_at=100)
        result, handoff = self.supervise("import time; time.sleep(30)", {"claude"}, "claude",
                                         on_start=lambda pid: self.reading("claude", 95, observed_at=100))
        self.assertIn("5% account usage remaining (reserve 10%)", (handoff / "handoff.md").read_text())

    def test_host_fresh_reading_dropping_below_reserve_hands_off(self):
        self.reading("codex", 50, observed_at=None)
        observation = rt.normalize("codex", {"rateLimits": {"primary": {"usedPercent": 92}}})
        code = ("import time,pathlib; time.sleep(.2); "
                f"pathlib.Path({str(self.state / 'codex.json')!r}).write_text({json.dumps(observation)!r}); "
                "time.sleep(30)")
        result, handoff = self.supervise(code, {"codex"}, "codex", clock=False)
        self.assertNotEqual(result, 0)
        self.assertIn("codex: 8% account usage remaining (reserve 10%)", (handoff / "handoff.md").read_text())

    def test_malformed_telemetry_never_crashes_supervision(self):
        self.reading("codex", 50, observed_at=None)
        rt.atomic_json(self.state / "claude.json", {"observed_at": "soon", "windows": [
            {"remaining_percent": True}, {"remaining_percent": 150}, "x", None]})
        result, handoff = self.supervise("import time; time.sleep(1.2)", {"codex", "claude"}, "codex")
        self.assertEqual((result, handoff), (0, None))
        self.assertIn("no claude usage reading yet", self.errors())

    # Leases -------------------------------------------------------------

    def specialist(self, provider="claude", role="backend-engineer", run_id=None, seconds=30):
        """Python code for a delegated specialist holding a lease while it sleeps.

        run_id None uses the supervised run's id from SYNDICATE_RUN_ID."""
        owner = f"{run_id!r}" if run_id else f"os.environ[{rt.RUN_ENV!r}]"
        return (f"import os,sys,time; sys.path.insert(0, {BIN!r}); import syndicate_runtime as rt\n"
                f"with rt.specialist_lease({str(self.project)!r}, {provider!r}, {role!r}, {owner}):\n"
                f"    time.sleep({seconds})")

    def cli_with_specialist(self, prefix=(), **options):
        """A supervised CLI whose child is a delegated specialist holding a lease."""
        command = [*prefix, sys.executable, "-c", self.specialist(**options)]
        return f"import subprocess; subprocess.Popen({command!r}).wait()"

    def test_held_lease_enforces_delegated_provider(self):
        self.reading("codex", 50, observed_at=None)
        self.reading("claude", 90, observed_at=100)  # stale but real and at the reserve
        started = time.monotonic()
        result, handoff = self.supervise(self.cli_with_specialist(), {"codex"}, "codex", clock=False)
        self.assertLess(time.monotonic() - started, 20)
        self.assertNotEqual(result, 0)
        self.assertIn("claude: 10% account usage remaining (reserve 10%) while the backend-engineer "
                      "specialist was running", (handoff / "handoff.md").read_text())

    def test_locally_enforced_specialist_reserve_does_not_stop_host(self):
        self.reading("codex", 50, observed_at=None)
        self.reading("claude", 50, observed_at=None)
        specialist = self.project / "claude"
        specialist.write_text(
            f"#!{sys.executable}\n"
            f"import sys,time; sys.path.insert(0, {BIN!r}); import syndicate_runtime as rt\n"
            f"rt.STATE = rt.Path({str(self.state)!r})\n"
            "rt.save_observation('claude', {'rate_limits': {'five_hour': {'used_percentage': 95}}})\n"
            "time.sleep(30)\n"
        )
        specialist.chmod(0o700)
        code = (
            f"import os,sys,time; sys.path.insert(0, {BIN!r}); import syndicate_runtime as rt\n"
            f"project = rt.Path({str(self.project)!r}); rt.STATE = rt.Path({str(self.state)!r})\n"
            "os.environ['PATH'] = str(project) + ':' + os.environ['PATH']\n"
            f"with rt.specialist_lease(project, 'claude', 'qa', os.environ[{rt.RUN_ENV!r}], local_quota=True):\n"
            "    try: rt.run_specialist_process(['claude'], cwd=project, reserve=10)\n"
            "    except rt.RoleUsageLimit as error: print(error, flush=True); time.sleep(1.2)\n"
            "print('host kept running', flush=True)\n"
        )
        result, handoff = self.supervise(code, {"codex"}, "codex", clock=False)
        self.assertEqual((result, handoff), (0, None))
        self.out.flush()
        output = self.out.buffer.getvalue().decode()
        self.assertIn("claude reached the role usage reserve (5% remaining, 10% reserve)", output)
        self.assertIn("host kept running", output)

    def test_lease_from_another_pid_namespace_is_enforced(self):
        # Regression: a Codex host runs run-role inside its sandbox, which has
        # its own PID namespace (getpid() is 2 there). PID-based leases never
        # matched; lock-based leases must.
        prefix = namespace_prefix()
        if prefix is None:
            self.skipTest("cannot start a process in a new PID namespace here")
        self.reading("codex", 50, observed_at=None)
        self.reading("claude", 95, observed_at=None)
        result, handoff = self.supervise(self.cli_with_specialist(prefix=prefix), {"codex"}, "codex", clock=False)
        self.assertNotEqual(result, 0)
        self.assertIsNotNone(handoff, f"{prefix[0]}: {self.errors()}")
        self.assertIn("claude: 5% account usage remaining (reserve 10%) while the backend-engineer "
                      "specialist was running", (handoff / "handoff.md").read_text())

    def test_delegated_provider_above_reserve_keeps_running(self):
        self.reading("codex", 50, observed_at=None)
        self.reading("claude", 50, observed_at=100)
        result, handoff = self.supervise(self.cli_with_specialist(seconds=1.5), {"codex"}, "codex", clock=False)
        self.assertEqual((result, handoff), (0, None))
        self.assertEqual(self.errors().count("claude usage telemetry is stale"), 1)

    def test_lease_of_another_run_is_ignored(self):
        self.reading("codex", 50, observed_at=None)
        self.reading("claude", 5, observed_at=None)
        code = self.cli_with_specialist(run_id=OTHER_RUN, seconds=1.5)
        result, handoff = self.supervise(code, {"codex"}, "codex", clock=False)
        self.assertEqual((result, handoff), (0, None))

    def test_supervised_cli_receives_run_id(self):
        self.reading("claude", 50, observed_at=None)
        with patch.dict(os.environ, {rt.RUN_ENV: OTHER_RUN}):
            self.supervise(f"import os; print(os.environ.get({rt.RUN_ENV!r}))", {"claude"}, "claude")
        record = json.loads((self.project / ".syndicate" / "run.json").read_text())
        self.out.flush()
        self.assertEqual(self.out.buffer.getvalue().decode().strip(), record["id"])
        self.assertTrue(rt.valid_run_id(record["id"]))
        # The Chief does not use leases and passes no run id on.
        with patch.dict(os.environ, {rt.RUN_ENV: OTHER_RUN}), patch("sys.stdout", self.out), \
             patch("sys.stderr", self.err):
            rt.supervise([sys.executable, "-c", f"import os; print('chief', os.environ.get({rt.RUN_ENV!r}))"],
                         self.project, {"claude"}, 10, "task", "claude", headless=True, chief=True)
        self.out.flush()
        self.assertIn("chief None", self.out.buffer.getvalue().decode())

    def test_lease_evaluation_failure_keeps_enforcing_host(self):
        self.reading("codex", 50, observed_at=None)
        with patch.object(rt, "read_leases", side_effect=RuntimeError("boom")):
            result, handoff = self.supervise("import time; time.sleep(1.5)", {"codex"}, "codex", clock=False)
        self.assertEqual((result, handoff), (0, None))
        self.assertEqual(self.errors().count("could not evaluate specialist leases"), 1)
        # ...and the host is still enforced while lease evaluation fails.
        observation = rt.normalize("codex", {"rateLimits": {"primary": {"usedPercent": 95}}})
        code = ("import time,pathlib; time.sleep(.2); "
                f"pathlib.Path({str(self.state / 'codex.json')!r}).write_text({json.dumps(observation)!r}); "
                "time.sleep(30)")
        with patch.object(rt, "read_leases", side_effect=RuntimeError("boom")):
            result, handoff = self.supervise(code, {"codex"}, "codex", clock=False)
        self.assertIn("codex: 5% account usage remaining", (handoff / "handoff.md").read_text())

    def test_held_lease_is_read_and_released_on_exit(self):
        run = "20260101T000000-0123abcd"
        with rt.specialist_lease(self.project, "codex", "code-reviewer", run) as path:
            self.assertRegex(path.name, r"^[0-9a-f]{32}\.json$")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            expected = [{"provider": "codex", "role": "code-reviewer", "run_id": run}]
            self.assertEqual(rt.read_leases(self.project, run), expected)
            self.assertEqual(rt.read_leases(self.project, OTHER_RUN), [])
        self.assertFalse(path.exists())
        self.assertEqual(os.listdir(path.parent), [])

    def test_two_concurrent_leases_do_not_clobber(self):
        run = "20260101T000000-0123abcd"
        with rt.specialist_lease(self.project, "claude", "qa-engineer", run) as first:
            with rt.specialist_lease(self.project, "codex", "code-reviewer", run) as second:
                self.assertNotEqual(first, second)
                self.assertEqual(sorted(l["role"] for l in rt.read_leases(self.project, run)),
                                 ["code-reviewer", "qa-engineer"])
            self.assertEqual([l["role"] for l in rt.read_leases(self.project, run)], ["qa-engineer"])
            self.assertTrue(first.exists())

    def test_lease_removed_when_specialist_raises(self):
        run = "20260101T000000-0123abcd"
        with self.assertRaises(RuntimeError):
            with rt.specialist_lease(self.project, "claude", "qa-engineer", run) as path:
                raise RuntimeError("specialist failed")
        self.assertFalse(path.exists())

    def test_unlocked_lease_of_killed_specialist_is_cleaned_up(self):
        run = "20260101T000000-0123abcd"
        code = (f"import os,sys,time; sys.path.insert(0, {BIN!r}); import syndicate_runtime as rt\n"
                f"with rt.specialist_lease({str(self.project)!r}, 'claude', 'qa', {run!r}) as path:\n"
                "    print(path, flush=True); time.sleep(30)")
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        path = Path(child.stdout.readline().strip())
        self.assertEqual(len(rt.read_leases(self.project, run)), 1)
        child.kill()  # no cleanup runs; the kernel releases the lock
        child.wait()
        child.stdout.close()
        self.assertTrue(path.exists())
        self.assertEqual(rt.read_leases(self.project, run), [])
        self.assertFalse(path.exists())

    def held(self, name, content):
        """A lease-named file whose lock is held, as by a running specialist."""
        directory = rt.lease_directory(self.project)
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / name
        path.write_bytes(content if isinstance(content, bytes) else
                         (content if isinstance(content, str) else json.dumps(content)).encode())
        fd = os.open(path, os.O_RDONLY)
        rt.fcntl.flock(fd, rt.fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        return path

    def test_lease_json_recursion_error_skips_only_that_lease(self):
        # Python 3.11's C decoder stops near 1000 levels, which a 4096-byte
        # lease can exceed; the bad lease is skipped, others still count.
        run = "20260101T000000-0123abcd"
        good = {"provider": "claude", "role": "qa", "run_id": run}
        self.held(uuid.uuid4().hex + ".json", good)
        self.held(uuid.uuid4().hex + ".json", "[" * 2000 + "]" * 2000)
        real = json.loads
        def loads(raw, *a, **k):
            if raw.startswith(b"[["):
                raise RecursionError("maximum recursion depth exceeded")
            return real(raw, *a, **k)
        with patch.object(rt.json, "loads", side_effect=loads):
            self.assertEqual([l["role"] for l in rt.read_leases(self.project, run)], ["qa"])

    def test_unsafe_or_malformed_leases_are_ignored(self):
        run = "20260101T000000-0123abcd"
        good = {"provider": "claude", "role": "qa", "run_id": run}
        name = lambda: uuid.uuid4().hex + ".json"
        self.held(name(), good)
        self.assertEqual(len(rt.read_leases(self.project, run)), 1)  # the control is accepted
        for label, data in (("not json", "{not json"), ("list", "[]"),
                            ("provider", {**good, "provider": "gemini"}),
                            ("role", {**good, "role": "Bad Role\n# injected"}),
                            ("role type", {**good, "role": None}),
                            ("long role", {**good, "role": "a" * 65}),
                            ("run id", {**good, "run_id": "x" * 20}),
                            ("run id type", {**good, "run_id": 7}),
                            ("oversized", {**good, "pad": "x" * rt.LEASE_MAX_BYTES})):
            with self.subTest(label):
                path = self.held(name(), data)
                self.assertEqual(len(rt.read_leases(self.project, run)), 1)
                path.unlink()
        directory = rt.lease_directory(self.project)
        # A name outside the pattern is never read.
        self.held("notes.json", good)
        self.held("0.json", good)
        self.assertEqual(len(rt.read_leases(self.project, run)), 1)
        # A symlinked lease file is not followed.
        outside = self.project / "outside.json"
        outside.write_text(json.dumps(good))
        (directory / name()).symlink_to(outside)
        # A FIFO is skipped without blocking.
        os.mkfifo(directory / name())
        self.assertEqual(len(rt.read_leases(self.project, run)), 1)

    def test_unsafe_lease_directory_is_ignored(self):
        run = "20260101T000000-0123abcd"
        good = {"provider": "claude", "role": "qa", "run_id": run}
        self.held(uuid.uuid4().hex + ".json", good)
        directory = rt.lease_directory(self.project)
        directory.chmod(0o770)  # group-writable
        self.assertEqual(rt.read_leases(self.project, run), [])
        directory.chmod(0o700)
        with patch.object(rt.os, "getuid", return_value=os.getuid() + 1):  # wrong owner
            self.assertEqual(rt.read_leases(self.project, run), [])
        self.assertEqual(len(rt.read_leases(self.project, run)), 1)
        # A symlinked lease directory is neither read nor written through.
        moved = self.project / "elsewhere"
        directory.rename(moved)
        directory.symlink_to(moved)
        self.assertEqual(rt.read_leases(self.project, run), [])
        with self.assertRaises(OSError):
            with rt.specialist_lease(self.project, "claude", "qa", run):
                pass
        self.assertEqual(len(os.listdir(moved)), 1)

    def test_writer_tightens_directory_and_rejects_bad_fields(self):
        run = "20260101T000000-0123abcd"
        directory = rt.lease_directory(self.project)
        directory.mkdir(mode=0o777)
        directory.chmod(0o777)
        with rt.specialist_lease(self.project, "claude", "Odd_Global", run):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual([l["role"] for l in rt.read_leases(self.project, run)], ["unnamed-role"])
        for provider, owner in (("gemini", run), ("claude", "not-a-run"), ("claude", None)):
            with self.subTest(provider=provider, owner=owner), self.assertRaises(ValueError):
                with rt.specialist_lease(self.project, provider, "qa", owner):
                    pass

    def test_huge_integer_telemetry_never_crashes_preflight_or_loop(self):
        # Regression (security LOW-1): a 400-digit integer used to raise
        # OverflowError out of math.isfinite and kill the supervisor.
        huge = 10**400
        self.reading("codex", 50, observed_at=None)
        path = self.state / "claude.json"
        path.write_text('{"observed_at": %d, "windows": [{"name": "five_hour", "remaining_percent": %d, '
                        '"resets_at": %d}]}' % (huge, huge, huge))
        result, handoff = self.supervise("import time; time.sleep(1.5)", {"codex", "claude"}, "codex")
        self.assertEqual((result, handoff), (0, None))
        self.assertIn("no claude usage reading yet", self.errors())
        # A real reading with a huge observed_at is still enforced (stale, unknown time).
        path.write_text('{"observed_at": %d, "windows": [{"name": "five_hour", "remaining_percent": 5}]}' % huge)
        result, handoff = self.supervise("import time; time.sleep(30)", {"codex", "claude"}, "codex", clock=False)
        self.assertEqual(result, 75)
        self.assertIn("claude: 5% account usage remaining", (handoff / "handoff.md").read_text())
        # ...and mid-run, after the CLI has started.
        path.write_text('{"observed_at": 1, "windows": [{"name": "five_hour", "remaining_percent": 50}]}')
        result, handoff = self.supervise(
            "import time; time.sleep(30)", {"codex", "claude"}, "codex",
            on_start=lambda pid: path.write_text('{"observed_at": %d, "windows": [{"name": "five_hour", '
                                                 '"remaining_percent": %d}, {"name": "seven_day", '
                                                 '"remaining_percent": 4}]}' % (huge, huge)))
        self.assertIn("claude: 4% account usage remaining", (handoff / "handoff.md").read_text())

    def test_lease_evaluation_recovery_is_reported_and_rearms_the_warning(self):
        self.reading("codex", 50, observed_at=None)
        calls = iter([RuntimeError, RuntimeError, None, RuntimeError] + [None] * 1000)
        def flaky(*args, **kwargs):
            error = next(calls)
            if error:
                raise error("boom")
            return []
        with patch.object(rt, "read_leases", side_effect=flaky):
            result, handoff = self.supervise("import time; time.sleep(1.5)", {"codex"}, "codex")
        self.assertEqual((result, handoff), (0, None))
        self.assertEqual(self.errors().count("could not evaluate specialist leases"), 2)
        self.assertEqual(self.errors().count("specialist lease evaluation recovered"), 2)

    def test_lease_scan_is_bounded_and_reports_the_cap_once(self):
        run = "20260101T000000-0123abcd"
        directory = rt.lease_directory(self.project)
        good = {"provider": "claude", "role": "qa", "run_id": run}
        with patch.object(rt, "LEASE_SCAN_LIMIT", 2), patch.object(rt, "LEASE_SCAN_ENTRIES", 8):
            for index in range(3):
                self.held(uuid.uuid4().hex + ".json", good)
            caps = []
            self.assertEqual(len(rt.read_leases(self.project, run, on_cap=caps.append)), 2)
            self.assertEqual(len(caps), 1)
            self.assertIn("at least 2 lease files and further entries", caps[0])
            for path in directory.iterdir():
                path.unlink()
            caps.clear()
            self.held(uuid.uuid4().hex + ".json", good)
            self.assertEqual(len(rt.read_leases(self.project, run, on_cap=caps.append)), 1)
            self.assertEqual(caps, [])  # exactly at neither cap
            for index in range(10):
                (directory / f"junk-{index}").write_text("")
            self.assertLessEqual(len(rt.read_leases(self.project, run, on_cap=caps.append)), 1)
            self.assertEqual(len(caps), 1)
            self.assertIn("more than 8 entries", caps[0])
            with patch.object(rt.os, "listdir", side_effect=AssertionError("unbounded listdir")):
                rt.read_leases(self.project, run)
            # The supervisor warns once per run, not once per tick.
            self.reading("codex", 50, observed_at=None)
            result, handoff = self.supervise("import time; time.sleep(1.5)", {"codex"}, "codex")
        self.assertEqual((result, handoff), (0, None))
        self.assertEqual(self.errors().count("only part of it was examined"), 1)

    def test_handoff_records_which_provider_hit_the_reserve(self):
        self.reading("codex", 50, observed_at=None)
        self.reading("claude", 95, observed_at=None)
        result, handoff = self.supervise(self.cli_with_specialist(), {"codex"}, "codex", clock=False)
        self.assertEqual(rt.limited_provider(self.project, handoff), "claude")
        self.reading("codex", 95, observed_at=None)
        result, handoff = self.supervise("pass", {"codex"}, "codex", clock=False)  # preflight
        self.assertEqual(rt.limited_provider(self.project, handoff), "codex")
        self.reading("codex", 50, observed_at=None)
        result, handoff = self.supervise("raise SystemExit(3)", {"codex"}, "codex", clock=False)
        self.assertIsNone(rt.limited_provider(self.project, handoff))
        self.assertIsNone(rt.limited_provider(self.project, self.project / "another-handoff"))

    def test_run_session_passes_the_delegated_trigger_to_the_prompt(self):
        self.config("orchestrator = codex\n")
        self.reading("codex", 50, observed_at=None)
        snapshot = self.project / "snapshot"
        snapshot.mkdir()
        def stopped(*args, **kwargs):
            rt.atomic_json(self.project / ".syndicate" / "handoff.json",
                           {"path": str(snapshot), "limited_provider": "claude",
                            "reason": "claude: 5% remaining while the qa specialist was running"})
            return 75, snapshot
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "task"])
        with patch.object(sy, "require_clis"), patch.object(sy, "register_project"), \
             patch.object(rt, "start_monitor"), patch.object(rt, "supervise", side_effect=stopped), \
             patch.object(rt, "choose_target", return_value=None) as choose, \
             patch("sys.stderr", self.err), patch("sys.stdout", self.out):
            self.assertEqual(sy.cmd_run(args), 75)
        self.assertEqual(choose.call_args.args[0], "codex")
        self.assertEqual(choose.call_args.kwargs, {"limited": "claude"})
        self.assertIn("claude: 5% remaining while the qa specialist was running", self.errors())

    def test_post_handoff_target_with_stale_low_reading_stays_stopped(self):
        self.reading("codex", 95, observed_at=100)  # stale and long past any reset
        snapshot = self.project / "snapshot"
        snapshot.mkdir()
        args = sy.build_parsers()[0].parse_args(["--project", str(self.project), "task"])
        with patch.object(sy, "require_clis"), patch.object(sy, "register_project"), \
             patch.object(rt, "start_monitor"), patch.object(rt, "supervise", return_value=(75, snapshot)) as run, \
             patch.object(rt, "choose_target", return_value="codex"), \
             patch("sys.stderr", self.err), patch("sys.stdout", self.out):
            self.assertEqual(sy.cmd_run(args), 75)
        self.assertEqual(run.call_count, 1)
        self.assertIn("codex last recorded only 5% remaining", self.errors())
        self.assertIn("syndicate limits --forget codex", self.errors())


class SpecialistProcess(ProjectCase):
    def test_reports_structured_quota_only_on_provider_error_events(self):
        codex = '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit."}}'
        claude = '{"type":"result","is_error":true,"result":"You\'ve hit your session limit · resets at 3:51pm"}'
        self.assertTrue(rt.reported_role_quota("codex", codex, ""))
        self.assertTrue(rt.reported_role_quota("claude", claude, ""))
        self.assertTrue(rt.reported_role_quota("claude", '{"type":"result","is_error":true,'
                                               '"errors":["You\'ve hit your usage limit."]}', ""))
        self.assertFalse(rt.reported_role_quota("codex", codex.replace("turn.failed", "item.completed"), ""))
        self.assertFalse(rt.reported_role_quota("claude", claude.replace("true", "false"), ""))
        self.assertFalse(rt.reported_role_quota("claude", "You've hit your limit.", ""))
        self.assertFalse(rt.reported_role_quota("claude", "You've hit your weekly limit · resets 3:51pm", ""))
        self.assertTrue(rt.reported_role_quota("claude", "", "You've hit your weekly limit · resets 3:51pm"))

    def test_terminating_role_wrapper_stops_child_session(self):
        marker = self.project / "child.pid"
        specialist = self.project / "claude"
        specialist.write_text(
            f"#!{sys.executable}\nimport os,time,pathlib\n"
            f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(30)\n"
        )
        specialist.chmod(0o700)
        child_env = dict(os.environ, PATH=f"{self.project}:{os.environ.get('PATH', '')}", PYTHONPATH=BIN)
        code = "import syndicate_runtime as rt; rt.run_specialist_process(['claude'], cwd='.')"
        with subprocess.Popen([sys.executable, "-c", code], cwd=self.project, env=child_env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as wrapper:
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists(), "stand-in specialist did not start")
                pid = int(marker.read_text())
                wrapper.terminate()
                self.assertEqual(wrapper.wait(timeout=6), 128 + signal.SIGTERM)
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()

    @unittest.skipUnless(Path("/proc").is_dir(), "requires process identity tracking")
    def test_quota_failure_stops_detached_worker_before_retry(self):
        ready = self.project / "worker-ready"
        stopped = self.project / "worker-stopped"
        worker = (
            "import pathlib, signal, time\n"
            f"def stop(*_): pathlib.Path({str(stopped)!r}).touch(); raise SystemExit(0)\n"
            "signal.signal(signal.SIGINT, stop)\n"
            f"pathlib.Path({str(ready)!r}).touch()\n"
            "time.sleep(5)\n"
        )
        specialist = self.project / "codex"
        specialist.write_text(
            f"#!{sys.executable}\nimport pathlib, subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {worker!r}], start_new_session=True, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"ready = pathlib.Path({str(ready)!r}); deadline = time.monotonic() + 2\n"
            "while not ready.exists() and time.monotonic() < deadline: time.sleep(.01)\n"
            "time.sleep(.6)\n"
            'print("ERROR: You\'ve hit your usage limit.", file=sys.stderr); sys.exit(1)\n'
        )
        specialist.chmod(0o700)
        env = dict(os.environ, PATH=f"{self.project}:{os.environ.get('PATH', '')}")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(rt.RoleUsageLimit):
            rt.run_specialist_process(["codex"], cwd=self.project, env=env)
        self.assertTrue(stopped.exists(), "detached specialist worker survived the failed attempt")


class SpecialistGate(ProjectCase):
    RUN = "20260101T000000-0123abcd"

    def run_role(self, provider="claude", role="backend-engineer", run_id=RUN, launch_error=None, env_run_id=None):
        args = argparse.Namespace(dir=str(self.project), role=role, runtime=provider, brief="work")
        seen = []
        def launched(argv, **kwargs):
            seen.append(rt.read_leases(self.project, run_id))
            if launch_error:
                raise launch_error
            return 0
        err = io.StringIO()
        environment = {key: value for key, value in os.environ.items() if key != rt.RUN_ENV}
        if env_run_id is not None or run_id:
            environment[rt.RUN_ENV] = env_run_id if env_run_id is not None else run_id
        with patch.object(sy, "require_clis"), patch.object(rt, "run_specialist_process", side_effect=launched), \
             patch.dict(os.environ, environment, clear=True), contextlib.redirect_stderr(err):
            try:
                result = sy.cmd_run_role(args)
            except sy.SyndicateError as error:
                return str(error), seen, err.getvalue()
        return result, seen, err.getvalue()

    def test_refuses_at_or_below_reserve_without_launching(self):
        for used in (90, 97):
            self.usage("claude", used, observed_at=100)
            result, seen, _ = self.run_role("claude")
            self.assertEqual(seen, [])
            for part in ("refusing to launch the backend-engineer specialist on claude",
                         f"{100 - used}% account usage remaining", "10% reserve", "observed 19",
                         "no claude CLI was launched", "syndicate limits --forget claude"):
                self.assertIn(part, result)
        self.assertFalse(rt.lease_directory(self.project).exists())

    def test_refuses_unknown_with_guidance(self):
        result, seen, _ = self.run_role("claude")
        self.assertEqual(seen, [])
        self.assertIn("No claude usage reading has been recorded", result)
        self.assertIn("interactive Claude session", result)
        self.assertIn("syndicate limits", result)
        self.assertIn("--all-runtime", result)
        rt.atomic_json(rt.STATE / "monitor.json", {"codex_error": "Codex account limits unavailable for this login"})
        result, seen, _ = self.run_role("codex", "code-reviewer")
        self.assertEqual(seen, [])
        self.assertIn("No codex usage reading has been recorded", result)
        self.assertIn("Codex account limits unavailable for this login", result)

    def test_malformed_telemetry_reads_as_unknown(self):
        for observation in ({"observed_at": float("inf"), "windows": [{"remaining_percent": float("nan")}]},
                            {"observed_at": 1e300, "windows": "all"},
                            {"observed_at": True, "windows": [{"remaining_percent": "50"}]}):
            with self.subTest(observation=str(observation)[:40]):
                (rt.STATE / "claude.json").parent.mkdir(parents=True, exist_ok=True)
                (rt.STATE / "claude.json").write_text(json.dumps(observation, allow_nan=True))
                result, seen, _ = self.run_role("claude")
                self.assertIn("No claude usage reading has been recorded", result)
        rt.atomic_json(rt.STATE / "claude.json", {"observed_at": 1e300, "windows": [{"name": "five_hour", "remaining_percent": 50}]})
        result, seen, err = self.run_role("claude")
        self.assertEqual(result, 0)
        self.assertIn("observed at an unknown time", err)

    def test_main_reports_refusal_with_nonzero_exit(self):
        err = io.StringIO()
        with patch.object(sy, "require_clis"), patch.object(sy.subprocess, "run") as run, \
             contextlib.redirect_stderr(err):
            code = sy.main(["team", "run-role", "backend-engineer", "--project", str(self.project),
                            "--runtime", "claude", "work"])
        self.assertEqual(code, 1)
        self.assertFalse(any(call.args[0][0] == "claude" for call in run.call_args_list))
        self.assertIn("no claude CLI was launched", err.getvalue())

    def test_allows_stale_reading_above_reserve_and_holds_lease(self):
        self.usage("claude", 40, observed_at=100)
        result, seen, err = self.run_role("claude")
        self.assertEqual(result, 0)
        self.assertEqual(seen, [[{"provider": "claude", "role": "backend-engineer", "run_id": self.RUN,
                                  "local_quota": True}]])
        self.assertEqual(os.listdir(rt.lease_directory(self.project)), [])  # released after exit
        self.assertIn("claude usage telemetry is stale", err)
        self.assertNotIn("in-run enforcement", err)

    def test_lease_removed_when_launch_raises(self):
        self.usage("claude", 40, observed_at=None)
        with self.assertRaises(OSError):
            self.run_role("claude", launch_error=OSError("exec failed"))
        self.assertEqual(os.listdir(rt.lease_directory(self.project)), [])

    def test_outside_supervised_run_gates_without_lease_and_warns(self):
        self.usage("claude", 95, observed_at=None)
        result, seen, _ = self.run_role("claude", run_id=None)
        self.assertIn("no claude CLI was launched", result)  # the gate still applies
        self.usage("claude", 40, observed_at=None)
        result, seen, err = self.run_role("claude", run_id=None)
        self.assertEqual((result, seen), (0, [[]]))
        self.assertFalse(rt.lease_directory(self.project).exists())
        self.assertIn("SYNDICATE_RUN_ID is not set", err)
        self.assertIn("enforce the claude reserve locally without a lease", err)

    def test_lease_write_failure_in_supervised_run_refuses_launch(self):
        self.usage("claude", 40, observed_at=None)
        elsewhere = self.project / "elsewhere"
        elsewhere.mkdir()
        rt.lease_directory(self.project).symlink_to(elsewhere)
        result, seen, _ = self.run_role("claude")
        self.assertEqual(seen, [])
        for part in ("cannot record the backend-engineer specialist lease", "no claude CLI was launched",
                     "rerun this exact run-role command outside the sandbox with approval"):
            self.assertIn(part, result)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_no_gate_or_lease_when_handoff_off(self):
        self.config("handoff = off\n")
        result, seen, err = self.run_role("claude")
        self.assertEqual((result, seen), (0, [[]]))
        self.assertNotIn("in-run enforcement", err)

    def test_reserve_comes_from_project_config(self):
        self.config("handoff-reserve = 30\n")
        self.usage("claude", 75, observed_at=None)
        result, seen, _ = self.run_role("claude")
        self.assertIn("30% reserve", result)
        self.assertEqual(seen, [])

    def test_active_run_reserve_applies_to_attempt_and_fallback(self):
        self.run_record(reserve=30)
        self.hold_session_lock()
        self.usage("claude", 75)
        self.usage("codex", 72)
        result, seen, _ = self.run_role("claude")
        self.assertIn("30% reserve", result)
        self.assertEqual(seen, [])  # Both providers started below the active run reserve.

    def test_tmux_hosted_role_manages_backoff_and_attempt_leases(self):
        self.usage("codex", 20, observed_at=None)
        self.usage("claude", 40, observed_at=None)
        leases, commands = [], []

        def tmux(argv, directory, name, headless=False):
            commands.append(argv)
            self.assertEqual(rt.read_leases(directory, self.RUN), [])
            return sy.main(["_in-tmux", *argv])

        def launched(argv, **kwargs):
            leases.extend(rt.read_leases(self.project, self.RUN))
            if argv[0] == "codex":
                raise rt.RoleUsageLimit("codex reported a usage limit", 17)
            return 0

        with patch.object(sy, "tmux_enabled", return_value=True), \
             patch.dict(os.environ, {"TMUX": "", rt.RUN_ENV: self.RUN}), \
             patch.object(rt, "run_specialist_process", side_effect=launched), \
             patch.object(sy, "launch_tmux", side_effect=tmux):
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(sy.main(["team", "run-role", "backend-engineer", "--project", str(self.project),
                                          "--runtime", "codex", "work"]), 0)
        self.assertEqual(leases, [{"provider": provider, "role": "backend-engineer", "run_id": self.RUN,
                                   "local_quota": True} for provider in ("codex", "claude")])
        self.assertEqual(len(commands), 1)
        self.assertIn("Syndicate specialist completed: backend-engineer claude ", out.getvalue())
        self.assertEqual(os.listdir(rt.lease_directory(self.project)), [])


    # SYNDICATE_RUN_ID stripped (security LOW-2) ---------------------------

    LIVE = "20260202T020202-89abcdef"

    def run_record(self, run_id=LIVE, **extra):
        rt.atomic_json(self.project / ".syndicate" / "run.json", {"id": run_id, "task": "t", **extra})

    def hold_session_lock(self):
        path = sy.project_lock_path(self.project)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        rt.fcntl.flock(fd, rt.fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        return path

    def test_stripped_run_id_uses_live_run_from_run_json(self):
        self.usage("claude", 40, observed_at=None)
        self.run_record()
        lock = self.hold_session_lock()
        before = lock.stat()
        for label, env in (("absent", None), ("malformed", "not-a-run-id")):
            with self.subTest(label):
                result, seen, err = self.run_role("claude", run_id=None, env_run_id=env)
                self.assertEqual(result, 0)
                self.assertEqual(seen, [[{"provider": "claude", "role": "backend-engineer", "run_id": self.LIVE,
                                          "local_quota": True}]])
                self.assertIn(f"using run id {self.LIVE} from", err)
                self.assertIn("run.json", err)
                self.assertNotIn("in-run enforcement", err)
        after = lock.stat()
        self.assertEqual((before.st_ino, before.st_size), (after.st_ino, after.st_size))  # never replaced

    def test_stripped_run_id_requires_the_lock_held_before_and_after_reading(self):
        # A supervisor replaced between reading run.json and the probe must not
        # lend this lease the old run id (security LOW-B).
        from types import SimpleNamespace
        role = SimpleNamespace(name="qa", cli="claude")
        self.run_record()
        for states, expected in ((["held", "held"], self.LIVE), (["held", "free"], None),
                                 (["free"], None), (["missing"], None)):
            with self.subTest(states):
                probes = iter(states)
                with patch.object(sy, "session_lock_state", side_effect=lambda p: next(probes)):
                    self.assertEqual(sy.supervised_run_id(self.project, role, "is not set"), expected)
        probes = iter(["held", "unknown"])
        with patch.object(sy, "session_lock_state", side_effect=lambda p: next(probes)):
            with self.assertRaises(sy.SyndicateError):
                sy.supervised_run_id(self.project, role, "is not set")

    def test_stripped_run_id_lease_is_enforced_by_the_supervisor(self):
        # End to end: a specialist whose environment lost SYNDICATE_RUN_ID
        # still stops a supervised run whose session.lock is held.
        self.usage("codex", 50, observed_at=None)
        self.usage("claude", 95, observed_at=None)
        self.hold_session_lock()  # as cmd_run's launcher does around supervise()
        script = str(Path(BIN) / "syndicate")
        code = (f"import os,sys,time; sys.path.insert(0, {BIN!r})\n"
                "import importlib.machinery, importlib.util, syndicate_runtime as rt\n"
                "os.environ.pop(rt.RUN_ENV)  # stripped, as by a shell_environment_policy\n"
                f"loader = importlib.machinery.SourceFileLoader('s', {script!r})\n"
                "s = importlib.util.module_from_spec(importlib.util.spec_from_loader('s', loader))\n"
                "loader.exec_module(s)\n"
                f"found = s.supervised_run_id(s.Path({str(self.project)!r}), "
                "type('R', (), {'name': 'qa', 'cli': 'claude'})(), 'is not set')\n"
                f"with rt.specialist_lease({str(self.project)!r}, 'claude', 'qa', found):\n"
                "    time.sleep(30)")
        out, err = io.TextIOWrapper(io.BytesIO()), io.TextIOWrapper(io.BytesIO())
        with patch("sys.stdout", out), patch("sys.stderr", err):
            result, handoff = rt.supervise([sys.executable, "-c", code], self.project, {"codex"}, 10, "task",
                                           "codex", headless=True)
        err.flush()
        self.assertIsNotNone(handoff, err.buffer.getvalue().decode())
        self.assertIn("claude: 5% account usage remaining (reserve 10%) while the qa specialist was running",
                      (handoff / "handoff.md").read_text())

    def test_no_live_run_keeps_the_warning(self):
        self.usage("claude", 40, observed_at=None)
        lock = sy.project_lock_path(self.project)
        for label in ("no run.json", "no session.lock", "lock free"):
            with self.subTest(label):
                if label == "no session.lock":
                    self.run_record()
                if label == "lock free":
                    lock.write_text("")
                result, seen, err = self.run_role("claude", run_id=None)
                self.assertEqual((result, seen), (0, [[]]))
                self.assertIn("no Syndicate-supervised run is live", err)
                self.assertIn("enforce the claude reserve locally without a lease", err)
        # An unusable run.json with a free lock is not a live run either.
        (self.project / ".syndicate" / "run.json").write_text("{broken")
        result, seen, err = self.run_role("claude", run_id=None)
        self.assertEqual((result, seen), (0, [[]]))

    def test_undeterminable_lock_state_fails_closed(self):
        self.usage("claude", 40, observed_at=None)
        self.run_record()
        lock = sy.project_lock_path(self.project)
        target = self.project / "elsewhere.lock"
        target.write_text("")
        for label in ("symlink", "fifo", "hard link"):
            with self.subTest(label):
                lock.unlink(missing_ok=True)
                if label == "symlink":
                    lock.symlink_to(target)
                elif label == "fifo":
                    os.mkfifo(lock)
                else:
                    os.link(target, lock)
                result, seen, _ = self.run_role("claude", run_id=None)
                self.assertEqual(seen, [])
                for part in ("refusing to launch the backend-engineer specialist", "no claude CLI was launched",
                             "SYNDICATE_RUN_ID is not set", "cannot be determined"):
                    self.assertIn(part, result)
        self.assertEqual(target.read_text(), "")  # never written through

    def test_held_lock_without_a_valid_run_record_fails_closed(self):
        self.usage("claude", 40, observed_at=None)
        self.hold_session_lock()
        record = self.project / ".syndicate" / "run.json"
        outside = self.project / "outside.json"
        outside.write_text(json.dumps({"id": self.LIVE}))
        cases = (("bad id", lambda: self.run_record(run_id="../../etc")),
                 ("not json", lambda: record.write_text("{")),
                 ("list", lambda: record.write_text("[]")),
                 ("oversized", lambda: self.run_record(pad="x" * sy.RUN_RECORD_MAX_BYTES)),
                 ("symlink", lambda: record.symlink_to(outside)),
                 ("fifo", lambda: os.mkfifo(record)))
        for label, make in cases:
            with self.subTest(label):
                record.unlink(missing_ok=True)
                make()
                result, seen, _ = self.run_role("claude", run_id=None)
                self.assertEqual(seen, [])
                self.assertIn("does not name a valid run id", result)
                self.assertIn("no claude CLI was launched", result)
        self.assertFalse(rt.lease_directory(self.project).exists())

    def test_inner_tmux_run_checks_usage(self):
        self.usage("claude", 40, observed_at=100)  # stale but above the reserve
        def main(*argv):
            err = io.StringIO()
            with patch.object(sy, "require_clis"), contextlib.redirect_stderr(err), \
                 patch.object(rt, "run_specialist_process", return_value=0), \
                 patch.dict(os.environ, {rt.RUN_ENV: self.RUN}):
                return sy.main([*argv, "team", "run-role", "backend-engineer", "--project", str(self.project),
                                "--runtime", "claude", "work"]), err.getvalue()
        code, err = main("_in-tmux")
        self.assertEqual(code, 0)
        self.assertEqual(err.count("telemetry is stale"), 1)
        self.assertIn("claude usage telemetry is stale", err)
        self.usage("claude", 95, observed_at=100)  # the inner refusal still applies
        code, err = main("_in-tmux")
        self.assertEqual(code, 1)
        self.assertIn("no claude CLI was launched", err)


class ForgetReading(ProjectCase):
    def forget(self, provider):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             patch.object(rt, "start_monitor") as monitor:
            code = sy.main(["limits", "--forget", provider])
        self.assertFalse(monitor.called)
        return code, out.getvalue(), err.getvalue()

    def test_removes_only_that_providers_reading(self):
        self.usage("claude", 95, observed_at=100)
        self.usage("codex", 95, observed_at=100)
        rt.atomic_json(rt.STATE / "monitor.json", {"observations": {}})
        code, out, _ = self.forget("claude")
        self.assertEqual(code, 0)
        self.assertIn(f"Removed {rt.STATE / 'claude.json'}", out)
        self.assertIn("reserve cannot be enforced until a new claude usage reading is recorded", out)
        self.assertEqual(sorted(p.name for p in rt.STATE.iterdir()), ["codex.json", "monitor.json"])
        self.assertIsNone(rt.latest_reading(rt.read_json(rt.STATE / "claude.json")))
        code, out, _ = self.forget("claude")
        self.assertEqual(code, 0)
        self.assertIn("nothing removed", out)

    def test_refuses_symlink_and_unknown_provider(self):
        target = self.project / "target.json"
        target.write_text("{}")
        rt.STATE.mkdir(parents=True, exist_ok=True)
        (rt.STATE / "codex.json").symlink_to(target)
        code, _, err = self.forget("codex")
        self.assertEqual(code, 1)
        self.assertIn("not a regular file", err)
        self.assertTrue(target.exists())
        self.assertTrue((rt.STATE / "codex.json").is_symlink())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            sy.main(["limits", "--forget", "gemini"])



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
