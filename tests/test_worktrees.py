"""Durable, locked task worktrees: creation, handoff snapshots and post-merge cleanup."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import syndicate_git
import syndicate_runtime as rt
import syndicate_worktrees as wt
from test_syndicate import sy

TASK = "task-a"


class WorktreeCase(unittest.TestCase):
    def git(self, path, *args, check=True):
        result = subprocess.run(("git", *args), cwd=path, text=True, capture_output=True)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip() if check else result

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.root = self.tmp / "durable"
        # Test repositories live under the system temp directory. Exempt only this
        # test's directory, explicitly, from the volatile-location refusal.
        for target, value in ((wt, "ROOT"), (wt, "REGISTRY"), (wt, "VOLATILE_EXEMPT")):
            replacement = {"ROOT": self.root, "REGISTRY": self.root / "registry.json",
                           "VOLATILE_EXEMPT": [self.tmp]}[value]
            patcher = patch.object(target, value, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(wt.ROOT_ENV, None)
        self.remote = self.tmp / "origin.git"
        self.repo = self.tmp / "repo"
        self.source = self.tmp / "source"
        (self.source / ".syndicate").mkdir(parents=True)
        self.git(self.tmp, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(self.tmp, "clone", str(self.remote), str(self.repo))
        self.git(self.repo, "config", "user.name", "Test User")
        self.git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / ".gitignore").write_text("*.cache\n")
        (self.repo / "app.txt").write_text("one\n")
        (self.repo / "lib.txt").write_text("lib\n")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-m", "initial commit")
        self.git(self.repo, "push", "origin", "main")
        self.start = self.git(self.repo, "rev-parse", "HEAD")

    def ensure(self, task=TASK, branch="work/feature-1", **kwargs):
        return wt.ensure_worktree(self.source, self.repo, task, branch, **kwargs)

    def records(self):
        return wt.list_worktrees(self.repo)

    def record(self, path):
        return next(r for r in self.records() if Path(r["worktree"]).resolve() == Path(path))


class Creation(WorktreeCase):
    def test_durable_path_layout(self):
        self.assertEqual(wt.repo_key(Path("/home/nick/workspace/nabt/syndicate/.git")), "syndicate-be1a471b")
        entry = self.ensure()
        common = os.path.realpath(self.repo / ".git")
        expected = self.root / f"repo-{hashlib.sha1(common.encode()).hexdigest()[:8]}" / "work-feature-1"
        self.assertEqual(entry["path"], str(expected))
        self.assertTrue(expected.is_dir())
        self.assertEqual(entry["git_common_dir"], common)
        self.assertEqual(entry["repo"], str(self.repo))
        saved = json.loads((self.root / "registry.json").read_text())["worktrees"][str(expected)]
        for key in ("path", "repo", "git_common_dir", "branch", "series", "base", "task_ids",
                    "created_at", "lock_reason"):
            self.assertIn(key, saved)

    def test_environment_override_root(self):
        os.environ[wt.ROOT_ENV] = str(self.tmp / "elsewhere")
        entry = self.ensure()
        self.assertTrue(entry["path"].startswith(str(self.tmp / "elsewhere") + "/"))

    def test_volatile_roots_are_refused(self):
        os.environ["TMPDIR"] = str(self.tmp / "private-tmp")
        os.environ["XDG_RUNTIME_DIR"] = str(self.tmp / "runtime")
        with patch.object(wt, "VOLATILE_EXEMPT", []):
            for path in ("/tmp/x/work", "/var/tmp/x", "/dev/shm/x", "/run/user/1000/x",
                         str(Path(tempfile.gettempdir()) / "x"), str(self.tmp / "private-tmp" / "w"),
                         str(self.tmp / "runtime" / "w")):
                with self.subTest(path=path):
                    self.assertIsNotNone(wt.volatile_reason(path))
            os.environ[wt.ROOT_ENV] = str(self.tmp / "durable-looking")
            with self.assertRaisesRegex(wt.WorktreeError, "Refusing to use .*temporary directory"):
                self.ensure()
        self.assertEqual(len(self.records()), 1)
        self.assertFalse((self.tmp / "durable-looking").exists())

    def test_tmpfs_mount_is_refused_by_longest_prefix(self):
        mounts = self.tmp / "mounts"
        mounts.write_text("/dev/sda1 / ext4 rw 0 0\n"
                          "tmpfs /mnt/ram tmpfs rw 0 0\n"
                          "/dev/sdb1 /mnt/ram/disk ext4 rw 0 0\n"
                          "none /mnt/with\\040space ramfs rw 0 0\n")
        with patch.object(wt, "MOUNTS", mounts), patch.object(wt, "VOLATILE_EXEMPT", []):
            self.assertIn("tmpfs", wt.volatile_reason("/mnt/ram/worktrees/x"))
            self.assertIsNone(wt.volatile_reason("/mnt/ram/disk/worktrees/x"))
            self.assertIsNone(wt.volatile_reason("/mnt/ramdisk/x"))
            self.assertIn("ramfs", wt.volatile_reason("/mnt/with space/x"))
            self.assertIsNone(wt.volatile_reason("/home/someone/.syndicate/worktrees/x"))

    def test_creation_locks_with_reason_and_sets_no_upstream(self):
        entry = self.ensure(series="feature")
        path = Path(entry["path"])
        reason = "syndicate: task series feature (task task-a); do not prune until series complete"
        self.assertEqual(self.record(path)["locked"], reason)
        self.assertEqual(entry["lock_reason"], reason)
        self.assertEqual(entry["base"], "origin/main")
        self.assertEqual(self.git(path, "rev-parse", "HEAD"), self.start)
        self.assertEqual(self.git(path, "branch", "--show-current"), "work/feature-1")
        self.assertNotEqual(self.git(path, "rev-parse", "--abbrev-ref", "@{upstream}", check=False).returncode, 0)
        self.assertEqual(self.git(self.repo, "config", "--get", "branch.work/feature-1.merge", check=False).returncode, 1)

    def test_series_defaults_to_branch(self):
        entry = self.ensure()
        self.assertEqual(entry["series"], "work/feature-1")
        self.assertIn("task series work/feature-1 (task task-a)", self.record(entry["path"])["locked"])

    def test_idempotent_readoption_appends_series_task_and_relocks(self):
        first = self.ensure()
        again = self.ensure()
        self.assertFalse(again["created"])
        self.assertEqual(again["path"], first["path"])
        self.assertEqual(again["task_ids"], [TASK])
        self.git(self.repo, "worktree", "unlock", first["path"])
        later = self.ensure(task="task-b")
        self.assertEqual(later["task_ids"], [TASK, "task-b"])
        self.assertIn("task series work/feature-1", self.record(first["path"])["locked"])
        self.assertEqual(len(self.records()), 2)
        with self.assertRaisesRegex(wt.WorktreeError, "already belongs to task series"):
            self.ensure(task="task-c", series="other")

    def test_existing_branch_is_checked_out(self):
        self.git(self.repo, "switch", "-c", "work/existing")
        (self.repo / "app.txt").write_text("two\n")
        self.git(self.repo, "commit", "-am", "existing work")
        tip = self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.repo, "switch", "main")
        entry = self.ensure(branch="work/existing")
        self.assertEqual(self.git(entry["path"], "rev-parse", "HEAD"), tip)
        self.assertEqual(self.git(entry["path"], "branch", "--show-current"), "work/existing")

    def test_branch_checked_out_elsewhere_fails_with_guidance(self):
        elsewhere = self.tmp / "ad-hoc"
        self.git(self.repo, "worktree", "add", "-b", "work/feature-1", str(elsewhere))
        with self.assertRaisesRegex(wt.WorktreeError, "worktree move"):
            self.ensure()
        with patch.object(wt, "VOLATILE_EXEMPT", [self.root]):
            with self.assertRaisesRegex(wt.WorktreeError, "volatile path"):
                self.ensure()
        self.assertTrue(elsewhere.is_dir())
        self.assertFalse((self.root / "registry.json").exists())

    def test_older_git_without_add_reason_locks_after_adding(self):
        real = wt.git

        def old_git(path, *args, **kwargs):
            if args[:3] == ("worktree", "add", "--lock"):
                return subprocess.CompletedProcess(args, 129, "", "error: unknown option `reason'\nusage: --reason")
            return real(path, *args, **kwargs)

        with patch.object(wt, "git", side_effect=old_git):
            entry = self.ensure()
        self.assertEqual(self.record(entry["path"])["locked"], entry["lock_reason"])

    def test_main_branch_is_refused(self):
        with self.assertRaisesRegex(wt.WorktreeError, "other than main"):
            self.ensure(branch="main")
        self.git(self.repo, "switch", "-c", "work/feature-1")
        with self.assertRaisesRegex(wt.WorktreeError, "checked out in the main checkout"):
            self.ensure()

    def test_cli_prints_absolute_path(self):
        args = sy.build_parsers()[1].parse_args(
            ["tasks", "worktree", TASK, "-C", str(self.source), "--repo", str(self.repo),
             "--branch", "work/cli", "--series", "s1"])
        with patch("sys.stdout") as out:
            self.assertEqual(args.fn(args), 0)
        printed = "".join(call.args[0] for call in out.write.call_args_list).strip()
        self.assertTrue(Path(printed).is_absolute() and Path(printed).is_dir())


class HandoffCapture(WorktreeCase):
    def setUp(self):
        super().setUp()
        state = patch.object(rt, "STATE", self.tmp / "limits")
        state.start()
        self.addCleanup(state.stop)
        (self.source / ".syndicate" / "status.json").write_text(json.dumps(
            {"state": "running", "current": {"task_id": TASK, "summary": "work"}}))

    def handoff(self, run_id="20261122T100000-abcdef01"):
        return rt.create_handoff(self.source, "claude", "Build it", "10% remaining", run_id)

    def test_handoff_snapshots_task_worktree_without_touching_it(self):
        path = Path(self.ensure()["path"])
        (path / "app.txt").write_text("modified\n")
        (path / "lib.txt").write_text("staged\n")
        self.git(path, "add", "lib.txt")
        (path / "new.txt").write_text("untracked\n")
        (path / "build.cache").write_text("ignored\n")
        before = [self.git(path, *args) for args in (("rev-parse", "HEAD"), ("ls-files", "-s"),
                                                     ("status", "--porcelain"), ("diff", "--cached"))]
        index = (Path(self.git(path, "rev-parse", "--git-dir")) / "index").read_bytes()
        target = self.handoff()
        self.assertEqual((Path(self.git(path, "rev-parse", "--git-dir")) / "index").read_bytes(), index)
        after = [self.git(path, *args) for args in (("rev-parse", "HEAD"), ("ls-files", "-s"),
                                                    ("status", "--porcelain"), ("diff", "--cached"))]
        self.assertEqual(before, after)
        self.assertEqual((path / "app.txt").read_text(), "modified\n")
        evidence = target / "worktrees" / "work-feature-1"
        info = json.loads((evidence / "info.json").read_text())
        self.assertEqual(info["path"], str(path))
        self.assertEqual(info["branch"], "work/feature-1")
        self.assertEqual(info["head"], self.start)
        self.assertTrue(info["exists"] and info["locked"])
        ref = "refs/syndicate/snapshots/work-feature-1/20261122T100000-abcdef01"
        self.assertEqual(info["snapshot_ref"], ref)
        self.assertEqual(self.git(self.repo, "rev-parse", ref), info["snapshot_commit"])
        self.assertEqual(self.git(self.repo, "rev-parse", f"{ref}^"), self.start)
        self.assertEqual(self.git(self.repo, "show", f"{ref}:new.txt"), "untracked")
        self.assertEqual(self.git(self.repo, "show", f"{ref}:app.txt"), "modified")
        self.assertEqual(self.git(self.repo, "show", f"{ref}:lib.txt"), "staged")
        self.assertNotEqual(self.git(self.repo, "cat-file", "-e", f"{ref}:build.cache", check=False).returncode, 0)
        status = (evidence / "git-status.txt").read_text()
        self.assertIn("## work/feature-1", status)
        self.assertIn("?? new.txt", status)
        self.assertIn("M  lib.txt", status)
        self.assertIn("+modified", (evidence / "worktree.diff").read_text())
        self.assertIn("+staged", (evidence / "worktree.diff").read_text())
        self.assertIn("+staged", (evidence / "staged.diff").read_text())
        self.assertNotIn("modified", (evidence / "staged.diff").read_text())
        self.assertEqual((evidence / "untracked.txt").read_text(), "new.txt\n")
        self.assertTrue((evidence / "unpushed.log").is_file())
        self.assertFalse(list(evidence.glob("snapshot.index*")))
        handoff = (target / "handoff.md").read_text()
        self.assertIn("## Task worktrees", handoff)
        self.assertIn(str(path), handoff)
        self.assertIn(ref, handoff)
        self.assertIn("restore --source", handoff)
        # The launch-directory evidence is still written for compatibility.
        for name in ("git-status.txt", "worktree.diff", "staged.diff"):
            self.assertTrue((target / name).is_file())

    def test_unpushed_log_lists_commits_ahead_of_base(self):
        path = Path(self.ensure()["path"])
        (path / "app.txt").write_text("committed\n")
        self.git(path, "commit", "-am", "local work")
        log = (self.handoff() / "worktrees" / "work-feature-1" / "unpushed.log").read_text()
        self.assertIn("local work", log)
        self.assertIn("origin/main", log)

    def test_handoff_reports_missing_worktree(self):
        path = Path(self.ensure()["path"])
        shutil.rmtree(path)
        target = self.handoff()
        info = json.loads((target / "worktrees" / "work-feature-1" / "info.json").read_text())
        self.assertFalse(info["exists"])
        self.assertIsNone(info["snapshot_ref"])
        self.assertIn("missing", (target / "worktrees" / "work-feature-1" / "git-status.txt").read_text())
        handoff = (target / "handoff.md").read_text()
        self.assertIn("MISSING", handoff)
        self.assertIn(f"worktree add {path} work/feature-1", handoff)

    def test_handoff_without_registered_worktree_says_so(self):
        self.ensure(task="another-task")
        target = self.handoff()
        self.assertIn(f"No task worktree is registered for task {TASK}", (target / "handoff.md").read_text())
        self.assertFalse((target / "worktrees").exists())

    def test_git_failure_does_not_abort_handoff(self):
        self.ensure()
        with patch.object(wt, "_capture", return_value=None), patch.object(wt, "_text", return_value=None):
            target = self.handoff()
        info = json.loads((target / "worktrees" / "work-feature-1" / "info.json").read_text())
        self.assertIsNone(info["snapshot_ref"])
        self.assertIn("unavailable", (target / "worktrees" / "work-feature-1" / "worktree.diff").read_text())
        self.assertIn("Snapshot: unavailable", (target / "handoff.md").read_text())


class LockedCleanup(WorktreeCase):
    branch = "work/feature-1"

    def setUp(self):
        super().setUp()
        registry = patch.object(sy, "REGISTRY_PATH", self.tmp / "state" / "projects.json")
        registry.start()
        self.addCleanup(registry.stop)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        self.entry = self.ensure()
        self.path = Path(self.entry["path"])
        (self.path / "feature.txt").write_text("task change\n")
        self.git(self.path, "add", "feature.txt")
        self.git(self.path, "commit", "-m", "task change")
        self.head = self.git(self.path, "rev-parse", "HEAD")
        self.git(self.path, "push", "origin", self.branch)
        integrator = self.tmp / "integrator"
        self.git(self.tmp, "clone", str(self.remote), str(integrator))
        self.git(integrator, "config", "user.name", "Integrator")
        self.git(integrator, "config", "user.email", "integrator@example.com")
        self.git(integrator, "merge", "--no-ff", "--no-edit", f"origin/{self.branch}")
        self.git(integrator, "push", "origin", "main")
        merge = self.git(integrator, "rev-parse", "HEAD")
        self.pr = dict(state="MERGED", url="https://github.com/example/project/pull/7",
                       headRefName=self.branch, baseRefName="main", isCrossRepository=False,
                       headRefOid=self.head, mergeCommit={"oid": merge}, mergedAt="2026-11-22T10:00:00Z")
        self.tasks = []

    def cleanup(self, task=TASK):
        run = syndicate_git._run

        def fake(path, command, expected=(0,)):
            if command[0] == "gh":
                return subprocess.CompletedProcess(command, 0, json.dumps(self.pr), "")
            return run(path, command, expected)

        argv = ["tasks", "cleanup-pr", "7", "-C", str(self.repo), "--branch", self.branch,
                "--worktree", str(self.path)] + (["--task", task] if task else [])
        args = sy.build_parsers()[1].parse_args(argv)
        rows = [{"tasks": self.tasks, "completed_tasks": [], "ended_tasks": []}]
        with patch.object(syndicate_git, "_run", side_effect=fake), \
             patch.object(syndicate_git, "_github_repository", return_value="github.com/example/project"), \
             patch.object(sy, "gather_statuses", return_value=rows), patch("builtins.print"):
            return args.fn(args)

    def assert_intact(self):
        self.assertTrue(self.path.is_dir())
        self.assertIn("locked", self.record(self.path))
        self.assertEqual(self.git(self.remote, "rev-parse", f"refs/heads/{self.branch}"), self.head)
        self.assertIsNotNone(wt.entry_for_path(self.path))

    def test_accepts_syndicate_lock_and_unlocks_then_removes(self):
        self.assertEqual(self.cleanup(), 0)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.git(self.repo, "branch", "--list", self.branch), "")
        self.assertEqual(self.git(self.remote, "branch", "--list", self.branch), "")
        self.assertIsNone(wt.entry_for_path(self.path))

    def test_rejects_foreign_lock(self):
        self.git(self.repo, "worktree", "unlock", str(self.path))
        self.git(self.repo, "worktree", "lock", "--reason", "someone else's", str(self.path))
        with self.assertRaisesRegex(sy.SyndicateError, "locked by another tool"):
            self.cleanup()
        self.assert_intact()
        self.git(self.repo, "worktree", "unlock", str(self.path))
        self.git(self.repo, "worktree", "lock", str(self.path))  # no reason is foreign too
        with self.assertRaisesRegex(sy.SyndicateError, "locked by another tool"):
            self.cleanup()
        self.assert_intact()

    def test_refuses_while_another_series_task_is_unfinished(self):
        self.ensure(task="task-b")
        self.tasks = [{"id": "task-b", "source_path": str(self.source), "state": "running"},
                      {"id": TASK, "source_path": str(self.source), "state": "running"}]
        with self.assertRaisesRegex(sy.SyndicateError, "series work/feature-1 is still active: task-b"):
            self.cleanup()
        self.assert_intact()
        self.tasks[0]["state"] = "done"
        self.assertEqual(self.cleanup(), 0)
        self.assertFalse(self.path.exists())

    def test_current_task_defaults_from_source_status(self):
        self.ensure(task="task-b")
        (self.source / ".syndicate" / "status.json").write_text(
            json.dumps({"current": {"task_id": "task-b"}}))
        self.tasks = [{"id": TASK, "source_path": str(self.source), "state": "ended"}]
        self.assertEqual(self.cleanup(task=None), 0)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
