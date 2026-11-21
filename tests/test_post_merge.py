"""Exercise cleanup with real local, linked-worktree and bare-remote Git refs."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import syndicate_git


class PostMergeCleanupTests(unittest.TestCase):
    branch = "work/task-123"
    task_ref = f"refs/heads/{branch}"

    def git(self, path, *args):
        result = subprocess.run(("git", *args), cwd=path, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.repo = root / "main checkout"
        self.remote = root / "origin.git"
        self.integrator = root / "integrator"
        self.task = root / "task checkout"
        self.git(root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.git(root, "clone", str(self.remote), str(self.repo))
        self.git(self.repo, "config", "user.name", "Test User")
        self.git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / ".gitignore").write_text("*.cache\n")
        self.git(self.repo, "add", ".gitignore")
        self.git(self.repo, "commit", "-m", "initial commit")
        self.start = self.git(self.repo, "rev-parse", "HEAD")
        self.git(self.repo, "push", "origin", "main")
        self.git(self.repo, "worktree", "add", "-b", self.branch, str(self.task))
        (self.task / "feature.txt").write_text("task change\n")
        self.git(self.task, "add", "feature.txt")
        self.git(self.task, "commit", "-m", "task change")
        self.head = self.git(self.task, "rev-parse", "HEAD")
        self.git(self.task, "push", "origin", self.branch)
        self.git(root, "clone", str(self.remote), str(self.integrator))
        self.git(self.integrator, "config", "user.name", "Test Integrator")
        self.git(self.integrator, "config", "user.email", "integrator@example.com")
        self.pr = dict(state="OPEN", url="https://github.com/example/project/pull/123",
                       headRefName=self.branch, baseRefName="main", isCrossRepository=False,
                       headRefOid=self.head)
        self.messages = []

    def merge(self, *, squash=False, push=True):
        self.git(self.integrator, "merge", "--squash" if squash else "--no-ff",
                 "--no-edit", f"origin/{self.branch}")
        if squash:
            self.git(self.integrator, "commit", "-m", "squashed PR")
        merge = self.git(self.integrator, "rev-parse", "HEAD")
        if push:
            self.git(self.integrator, "push", "origin", "main")
        self.pr.update(state="MERGED", mergeCommit={"oid": merge},
                       mergedAt="2026-11-21T10:00:00Z")
        return merge

    def cleanup(self, *, move_remote=False, switch_main=False, worktree=None):
        run = syndicate_git._run

        def run_with_mock_pr(path, command, expected=(0,)):
            if command[0] == "gh":
                self.assertEqual(command[:6], ("gh", "pr", "view", "123", "--repo",
                                               "github.com/example/project"))
                return subprocess.CompletedProcess(command, 0, json.dumps(self.pr), "")
            if command[0] == "git" and "push" in command and move_remote:
                # The branch changed after ls-remote. A leased deletion must fail.
                self.git(self.task, "commit", "--allow-empty", "-m", "new remote work")
                self.git(self.task, "push", "origin", self.branch)
            if command[0] == "sh" and switch_main:
                self.git(self.repo, "switch", "-c", "different-task")
            return run(path, command, expected)

        with patch.object(syndicate_git, "_run", side_effect=run_with_mock_pr), patch.object(
            syndicate_git, "_github_repository", return_value="github.com/example/project"
        ):
            syndicate_git.cleanup_pr(self.repo, 123, self.branch, worktree or self.task,
                                     self.messages.append)

    def assert_artifacts(self, main=None):
        self.assertTrue(self.task.is_dir())
        self.assertEqual(self.git(self.repo, "rev-parse", self.task_ref), self.head)
        self.assertEqual(self.git(self.remote, "rev-parse", self.task_ref), self.head)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), main or self.start)

    def test_merged_pr_cleans_only_task_artifacts_and_fast_forwards_main(self):
        self.git(self.repo, "branch", "another-task")
        target = self.merge()
        self.cleanup()
        self.assertFalse(self.task.exists())
        self.assertEqual(self.git(self.repo, "branch", "--list", self.branch), "")
        self.assertEqual(self.git(self.remote, "branch", "--list", self.branch), "")
        self.assertEqual(self.git(self.repo, "rev-parse", "another-task"), self.start)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), target)
        self.assertEqual((self.repo / "feature.txt").read_text(), "task change\n")
        self.assertIn("Verified PR", self.messages[0])
        self.assertIn("Fast-forwarded local main", self.messages[-1])
        self.cleanup()  # Repeat safely after all task artifacts are gone.
        self.assertIn("already at", self.messages[-1])

    def test_open_or_closed_unmerged_pr_leaves_artifacts_intact(self):
        for state in ("OPEN", "CLOSED"):
            with self.subTest(state=state):
                self.pr["state"] = state
                self.cleanup()
                self.assert_artifacts()
                self.assertIn("cleanup skipped", self.messages[-1])

    def test_merged_pr_with_already_deleted_remote_branch(self):
        target = self.merge()
        self.git(self.integrator, "push", "origin", f":{self.task_ref}")
        self.cleanup()
        self.assertFalse(self.task.exists())
        self.assertEqual(self.git(self.repo, "branch", "--list", self.branch), "")
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), target)
        self.assertTrue(any("already absent" in message for message in self.messages))

    def test_cleanup_fetch_preserves_unrelated_tracking_refs_and_tags(self):
        self.merge()
        self.git(self.repo, "config", "remote.origin.prune", "true")
        self.git(self.repo, "config", "remote.origin.pruneTags", "true")
        unrelated = "refs/remotes/origin/unrelated"
        self.git(self.repo, "update-ref", unrelated, self.start)
        self.git(self.repo, "tag", "local-only")
        self.cleanup()
        self.assertEqual(self.git(self.repo, "rev-parse", unrelated), self.start)
        self.assertEqual(self.git(self.repo, "rev-parse", "refs/tags/local-only"), self.start)

    def test_remote_branch_deletion_does_not_follow_symbolic_tracking_ref(self):
        self.merge()
        unrelated = "refs/heads/unrelated"
        tracking = f"refs/remotes/origin/{self.branch}"
        self.git(self.repo, "branch", "unrelated", self.start)
        self.git(self.repo, "symbolic-ref", tracking, unrelated)
        self.cleanup()
        self.assertEqual(self.git(self.repo, "rev-parse", unrelated), self.start)
        self.assertEqual(self.git(self.repo, "symbolic-ref", tracking), unrelated)
        self.assertEqual(self.git(self.remote, "branch", "--list", self.branch), "")

    def test_remote_branch_deletion_ignores_origin_local_fetch_mapping(self):
        self.merge()
        unrelated = "refs/heads/unrelated"
        self.git(self.repo, "branch", "unrelated", self.start)
        self.git(self.repo, "config", "--replace-all", "remote.origin.fetch",
                 f"+{self.task_ref}:{unrelated}")
        self.cleanup()
        self.assertEqual(self.git(self.repo, "rev-parse", unrelated), self.start)
        self.assertEqual(self.git(self.remote, "branch", "--list", self.branch), "")

    def test_remote_branch_deletion_keeps_verified_push_url_despite_push_instead_of(self):
        self.merge()
        alternate = self.task.parent / "alternate.git"
        self.git(self.repo, "init", "--bare", str(alternate))
        self.git(self.task, "push", str(alternate), self.task_ref)
        self.git(self.repo, "config", "remote.origin.pushurl", str(self.remote))
        self.git(self.repo, "config", f"url.{alternate}.pushInsteadOf", str(self.remote))
        self.cleanup()
        self.assertEqual(self.git(self.remote, "branch", "--list", self.branch), "")
        self.assertEqual(self.git(alternate, "rev-parse", self.task_ref), self.head)

    def test_remote_branch_deletion_refuses_chained_instead_of_rewrite(self):
        self.merge()
        alternate = self.task.parent / "alternate.git"
        self.git(self.repo, "init", "--bare", str(alternate))
        self.git(self.task, "push", str(alternate), self.task_ref)
        alias = "https://example.test/project"
        self.git(self.repo, "config", "remote.origin.url", alias)
        self.git(self.repo, "config", "remote.origin.pushurl", alias)
        self.git(self.repo, "config", f"url.{self.remote}.insteadOf", alias)
        self.git(self.repo, "config", f"url.{alternate}.insteadOf", str(self.remote))
        with self.assertRaisesRegex(syndicate_git.CleanupError, "Cleanup push URL changed"):
            self.cleanup()
        self.assert_artifacts()
        self.assertEqual(self.git(alternate, "rev-parse", self.task_ref), self.head)

    def test_repository_overrides_stop_before_git_or_gh_commands(self):
        for name in ("GIT_DIR", "GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL", "GIT_SSH_COMMAND"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "private-value"}):
                with patch.object(syndicate_git, "_run") as run:
                    with self.assertRaisesRegex(syndicate_git.CleanupError, name) as error:
                        syndicate_git.cleanup_pr(self.repo, 123, self.branch, self.task,
                                                 self.messages.append)
                    run.assert_not_called()
                    self.assertNotIn("private-value", str(error.exception))
        self.assert_artifacts()

    def test_unverified_merge_commit_preserves_artifacts(self):
        self.merge(push=False)
        self.git(self.repo, "fetch", str(self.integrator), "HEAD")
        with self.assertRaisesRegex(syndicate_git.CleanupError, "absent from fetched origin/main"):
            self.cleanup()
        self.assert_artifacts()

    def test_dirty_main_leaves_artifacts_intact(self):
        self.merge()
        (self.repo / "local-draft").write_text("keep my draft")
        with self.assertRaisesRegex(syndicate_git.CleanupError, "Local main checkout is dirty"):
            self.cleanup()
        self.assert_artifacts()
        self.assertEqual((self.repo / "local-draft").read_text(), "keep my draft")

    def test_diverged_main_leaves_artifacts_intact(self):
        self.merge()
        self.git(self.repo, "commit", "--allow-empty", "-m", "unpublished main commit")
        newer = self.git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(syndicate_git.CleanupError, "cannot fast-forward"):
            self.cleanup()
        self.assert_artifacts(main=newer)

    def test_main_not_checked_out_leaves_artifacts_intact(self):
        self.merge()
        self.git(self.repo, "switch", "-c", "different-task")
        with self.assertRaisesRegex(syndicate_git.CleanupError, "No local main checkout"):
            self.cleanup()
        self.assert_artifacts()
        self.assertEqual(self.git(self.repo, "branch", "--show-current"), "different-task")

    def test_checkout_switch_at_fast_forward_boundary_preserves_both_branches(self):
        self.merge()
        with self.assertRaisesRegex(syndicate_git.CleanupError, "checkout changed before fast-forward"):
            self.cleanup(switch_main=True)
        self.assertEqual(self.git(self.repo, "branch", "--show-current"), "different-task")
        self.assertEqual(self.git(self.repo, "rev-parse", "refs/heads/different-task"), self.start)
        self.assertEqual(self.git(self.repo, "rev-parse", "refs/heads/main"), self.start)
        self.assertEqual(self.git(self.repo, "status", "--porcelain"), "")
        self.assertFalse((self.repo / "feature.txt").exists())

    def test_ignored_task_data_prevents_removal(self):
        self.merge()
        cache = self.task / "local.cache"
        cache.write_text("cannot discard")
        with self.assertRaisesRegex(syndicate_git.CleanupError, "Task worktree is dirty"):
            self.cleanup()
        self.assert_artifacts()
        self.assertEqual(cache.read_text(), "cannot discard")

    def test_mismatched_task_worktree_prevents_removal(self):
        self.merge()
        other = self.task.parent / "other worktree"
        self.git(self.repo, "worktree", "add", "-b", "other-task", str(other))
        with self.assertRaisesRegex(syndicate_git.CleanupError, "does not own"):
            self.cleanup(worktree=other)
        self.assert_artifacts()
        self.assertTrue(other.is_dir())

    def test_unproven_squash_ancestry_preserves_task_artifacts(self):
        self.merge(squash=True)
        with self.assertRaisesRegex(syndicate_git.CleanupError, "cannot be proven"):
            self.cleanup()
        self.assert_artifacts()

    def test_new_local_task_commits_prevent_cleanup(self):
        self.merge()
        self.git(self.task, "commit", "--allow-empty", "-m", "local task work")
        local_tip = self.git(self.task, "rev-parse", "HEAD")
        with self.assertRaisesRegex(syndicate_git.CleanupError, "Local task branch tip"):
            self.cleanup()
        self.assertTrue(self.task.is_dir())
        self.assertEqual(self.git(self.repo, "rev-parse", self.task_ref), local_tip)
        self.assertEqual(self.git(self.remote, "rev-parse", self.task_ref), self.head)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), self.start)

    def test_remote_branch_that_changes_during_cleanup_survives(self):
        self.merge()
        with self.assertRaisesRegex(syndicate_git.CleanupError, "stale info"):
            self.cleanup(move_remote=True)
        self.assertTrue(self.task.is_dir())
        current = self.git(self.repo, "rev-parse", self.task_ref)
        self.assertNotEqual(current, self.head)
        self.assertEqual(self.git(self.remote, "rev-parse", self.task_ref), current)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), self.start)


class GitHubEndpointTests(unittest.TestCase):
    def test_standard_https_and_ssh_urls_resolve_to_same_repository(self):
        for url in ("https://github.com/example/project.git",
                    "ssh://git@github.com/example/project.git",
                    "git@github.com:example/project.git"):
            with self.subTest(url=url):
                self.assertEqual(syndicate_git._github_repository(url),
                                 "github.com/example/project")

    def test_nonstandard_ports_are_rejected(self):
        for url in ("https://github.com:8443/example/project.git",
                    "ssh://git@github.com:2222/example/project.git"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(syndicate_git.CleanupError, "standard GitHub endpoint"):
                    syndicate_git._github_repository(url)

    def test_embedded_credentials_are_rejected_without_echoing_the_url(self):
        for url in ("https://private-token@github.com/example/project.git",
                    "https://user:private-token@github.com/example/project.git",
                    "ssh://git:private-token@github.com/example/project.git"):
            with self.assertRaisesRegex(syndicate_git.CleanupError, "embedded credentials") as error:
                syndicate_git._github_repository(url)
            self.assertNotIn("private-token", str(error.exception))
