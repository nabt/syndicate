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

    def squash(self, *, edit=None, push=True):
        """Squash-merge the PR on main; edit(path) may change the work tree before committing."""
        self.git(self.integrator, "fetch", "origin")
        self.git(self.integrator, "merge", "--squash", f"origin/{self.branch}")
        if edit:
            edit(self.integrator)
            self.git(self.integrator, "add", "-A")
        self.git(self.integrator, "commit", "-m", "squashed PR")
        merge = self.git(self.integrator, "rev-parse", "HEAD")
        if push:
            self.git(self.integrator, "push", "origin", "main")
        self.pr.update(state="MERGED", mergeCommit={"oid": merge}, mergedAt="2026-11-21T10:00:00Z")
        return merge

    def task_commit(self, name, text, message):
        (self.task / name).write_text(text)
        self.git(self.task, "add", name)
        self.git(self.task, "commit", "-m", message)
        self.head = self.git(self.task, "rev-parse", "HEAD")
        self.git(self.task, "push", "origin", self.branch)
        self.pr["headRefOid"] = self.head
        return self.head

    def main_commit(self, name, text):
        (self.integrator / name).write_text(text)
        self.git(self.integrator, "add", name)
        self.git(self.integrator, "commit", "-m", f"main change to {name}")
        self.git(self.integrator, "push", "origin", "main")

    def assert_cleaned(self, target, repo=None):
        repo = repo or self.repo
        self.assertFalse(self.task.exists())
        self.assertEqual(self.git(repo, "branch", "--list", self.branch), "")
        self.assertEqual(self.git(self.remote, "branch", "--list", self.branch), "")
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), target)

    def cleanup(self, *, move_remote=False, switch_main=False, worktree=None, repo=None,
                series_guard=None):
        run = syndicate_git._run

        def run_with_mock_pr(path, command, expected=(0,), label=None):
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
            return run(path, command, expected, label)

        with patch.object(syndicate_git, "_run", side_effect=run_with_mock_pr), patch.object(
            syndicate_git, "_github_repository", return_value="github.com/example/project"
        ):
            syndicate_git.cleanup_pr(repo or self.repo, 123, self.branch, worktree or self.task,
                                     self.messages.append, series_guard=series_guard)

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
        with self.assertRaisesRegex(syndicate_git.CleanupError, "No local main checkout") as caught:
            self.cleanup()
        self.assertIn(f"registered in {self.repo}", str(caught.exception))
        self.assertIn("clean checkout of main", str(caught.exception))
        self.assertIn("before changing any task branch or worktree", str(caught.exception))
        self.assertIn("worktree add PATH main", str(caught.exception))
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

    def test_squash_merge_with_edited_content_preserves_task_artifacts(self):
        # Conflict resolution (or a concurrent main change) altered a PR file in the merge commit.
        self.squash(edit=lambda path: (path / "feature.txt").write_text("resolved differently\n"))
        with self.assertRaisesRegex(syndicate_git.CleanupError,
                                    "content could not be proven.*feature.txt.*were preserved"):
            self.cleanup()
        self.assert_artifacts()

    def test_squash_merge_after_concurrent_main_change_to_pr_file_preserves(self):
        self.task_commit("shared.txt", "task version\n", "task edits shared file")
        self.main_commit("shared.txt", "main version\n")
        # Squash the PR's other file only; main keeps its own shared.txt.
        self.git(self.integrator, "checkout", f"origin/{self.branch}", "--", "feature.txt")
        self.git(self.integrator, "commit", "-m", "partial squash")
        self.git(self.integrator, "push", "origin", "main")
        self.pr.update(state="MERGED", mergeCommit={"oid": self.git(self.integrator, "rev-parse", "HEAD")})
        with self.assertRaisesRegex(syndicate_git.CleanupError, "content could not be proven.*shared.txt"):
            self.cleanup()
        self.assert_artifacts()

    def test_merge_commit_lacking_pr_changes_preserves_task_artifacts(self):
        # GitHub reports a main commit that does not carry the PR's change.
        self.main_commit("unrelated.txt", "main only\n")
        self.pr.update(state="MERGED", mergeCommit={"oid": self.git(self.integrator, "rev-parse", "HEAD")})
        with self.assertRaisesRegex(syndicate_git.CleanupError, "content could not be proven"):
            self.cleanup()
        self.assert_artifacts()

    def test_squash_merge_missing_pr_deletion_preserves_task_artifacts(self):
        self.git(self.task, "rm", "-q", ".gitignore")
        self.git(self.task, "commit", "-m", "drop ignore file")
        self.head = self.git(self.task, "rev-parse", "HEAD")
        self.git(self.task, "push", "origin", self.branch)
        self.pr["headRefOid"] = self.head
        self.squash(edit=lambda path: (path / ".gitignore").write_text("*.cache\n"))
        with self.assertRaisesRegex(syndicate_git.CleanupError, "content could not be proven.*gitignore"):
            self.cleanup()
        self.assert_artifacts()

    def test_squash_merge_cleans_task_artifacts(self):
        target = self.squash()
        self.cleanup()
        self.assert_cleaned(target)
        self.assertEqual((self.repo / "feature.txt").read_text(), "task change\n")
        self.assertTrue(any(f"Verified squash/rebase merge: task branch tip {self.head} equals PR head and "
                            f"its 1 changed paths match merge commit {target}" in message
                            for message in self.messages))

    def test_squash_merge_with_renames_deletions_and_modes_cleans_up(self):
        self.git(self.task, "mv", "feature.txt", "renamed.txt")
        self.git(self.task, "rm", "-q", ".gitignore")
        (self.task / "tool.sh").write_text("#!/bin/sh\n")
        (self.task / "tool.sh").chmod(0o755)
        (self.task / "blob.bin").write_bytes(bytes(range(256)))
        self.git(self.task, "add", "-A")
        self.git(self.task, "commit", "-m", "rename, delete, executable and binary")
        self.head = self.git(self.task, "rev-parse", "HEAD")
        self.git(self.task, "push", "origin", self.branch)
        self.pr["headRefOid"] = self.head
        self.main_commit("unrelated.txt", "concurrent main change\n")
        target = self.squash()
        self.cleanup()
        self.assert_cleaned(target)
        self.assertTrue(any("its 4 changed paths match" in message for message in self.messages))

    def test_squash_merge_with_changed_mode_in_merge_commit_preserves(self):
        (self.task / "tool.sh").write_text("#!/bin/sh\n")
        (self.task / "tool.sh").chmod(0o755)
        self.git(self.task, "add", "tool.sh")
        self.git(self.task, "commit", "-m", "add tool")
        self.head = self.git(self.task, "rev-parse", "HEAD")
        self.git(self.task, "push", "origin", self.branch)
        self.pr["headRefOid"] = self.head
        self.squash(edit=lambda path: (path / "tool.sh").chmod(0o644))
        with self.assertRaisesRegex(syndicate_git.CleanupError, "content could not be proven.*tool.sh"):
            self.cleanup()
        self.assert_artifacts()

    def test_squash_merge_checks_gitlink_entries(self):
        (self.task / "vendored").mkdir()  # An unpopulated submodule keeps a clean status.
        self.git(self.task, "update-index", "--add", "--cacheinfo", f"160000,{self.head},vendored")
        self.git(self.task, "commit", "-m", "add gitlink")
        self.head = self.git(self.task, "rev-parse", "HEAD")
        self.git(self.task, "push", "origin", self.branch)
        self.pr["headRefOid"] = self.head
        self.squash(edit=lambda path: self.git(path, "update-index", "--cacheinfo",
                                               f"160000,{self.start},vendored"))
        with self.assertRaisesRegex(syndicate_git.CleanupError, "content could not be proven.*vendored"):
            self.cleanup()
        self.assert_artifacts()

    def test_rebase_merge_cleans_task_artifacts(self):
        first = self.head
        self.task_commit("second.txt", "second\n", "second task change")
        self.main_commit("unrelated.txt", "concurrent main change\n")
        self.git(self.integrator, "fetch", "origin")
        for commit in (first, self.head):
            self.git(self.integrator, "cherry-pick", commit)
        target = self.git(self.integrator, "rev-parse", "HEAD")
        self.git(self.integrator, "push", "origin", "main")
        self.pr.update(state="MERGED", mergeCommit={"oid": target}, mergedAt="2026-11-21T10:00:00Z")
        self.cleanup()
        self.assert_cleaned(target)
        self.assertTrue(any("its 2 changed paths match" in message for message in self.messages))

    def test_squash_merge_with_extra_local_commit_preserves(self):
        self.squash()
        self.git(self.task, "commit", "--allow-empty", "-m", "local task work")
        local_tip = self.git(self.task, "rev-parse", "HEAD")
        with self.assertRaisesRegex(syndicate_git.CleanupError,
                                    f"Local task branch tip {local_tip} .*differs from PR head"):
            self.cleanup()
        self.assertTrue(self.task.is_dir())
        self.assertEqual(self.git(self.repo, "rev-parse", self.task_ref), local_tip)
        self.assertEqual(self.git(self.remote, "rev-parse", self.task_ref), self.head)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), self.start)

    def test_squash_merge_with_local_tip_behind_pr_head_preserves(self):
        first = self.head
        self.task_commit("second.txt", "second\n", "second task change")
        self.squash()
        self.git(self.task, "reset", "-q", "--hard", first)
        with self.assertRaisesRegex(syndicate_git.CleanupError, f"Local task branch tip {first}"):
            self.cleanup()
        self.assertTrue(self.task.is_dir())
        self.assertEqual(self.git(self.repo, "rev-parse", self.task_ref), first)
        self.assertEqual(self.git(self.remote, "rev-parse", self.task_ref), self.head)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), self.start)

    def test_squash_merge_with_different_remote_tip_preserves(self):
        self.squash()
        self.git(self.task, "commit", "--allow-empty", "-m", "pushed after merge")
        moved = self.git(self.task, "rev-parse", "HEAD")
        self.git(self.task, "push", "origin", self.branch)
        self.git(self.task, "reset", "-q", "--hard", self.head)
        with self.assertRaisesRegex(syndicate_git.CleanupError, f"Remote task branch tip {moved}"):
            self.cleanup()
        self.assertTrue(self.task.is_dir())
        self.assertEqual(self.git(self.repo, "rev-parse", self.task_ref), self.head)
        self.assertEqual(self.git(self.remote, "rev-parse", self.task_ref), moved)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), self.start)

    def test_squash_merge_fetches_missing_pr_head_without_creating_refs(self):
        target = self.squash()
        fresh = self.task.parent / "fresh"
        self.git(self.task.parent, "clone", "-q", "--no-local", "--single-branch", "--branch", "main", str(self.remote),
                 str(fresh))
        self.git(fresh, "reset", "-q", "--hard", self.start)
        refs_before = self.git(fresh, "for-each-ref", "--format=%(refname)")
        self.assertNotEqual(subprocess.run(("git", "cat-file", "-e", self.head), cwd=fresh).returncode, 0)
        self.cleanup(repo=fresh, worktree=self.task.parent / "absent task")
        self.assertEqual(self.git(self.remote, "branch", "--list", self.branch), "")
        self.assertEqual(self.git(fresh, "rev-parse", "HEAD"), target)
        self.assertEqual(self.git(fresh, "for-each-ref", "--format=%(refname)"), refs_before)
        self.assertTrue(any("Verified squash/rebase merge" in message for message in self.messages))

    def test_squash_merge_error_escapes_untrusted_path_names(self):
        name = "evil\n\x1b[32mVerified.txt"
        self.task_commit(name, "task\n", "add hostile path name")
        self.squash(edit=lambda path: (path / name).write_text("altered\n"))
        with self.assertRaises(syndicate_git.CleanupError) as raised:
            self.cleanup()
        message = str(raised.exception)
        self.assertNotIn("\n", message)
        self.assertNotIn("\x1b", message)
        self.assertIn(repr(name), message)
        self.assert_artifacts()

    def shallow_clone(self, depth):
        clone = self.task.parent / "shallow"
        self.git(self.task.parent, "clone", "-q", f"--depth={depth}", "--single-branch", "--branch", "main",
                 f"file://{self.remote}", str(clone))
        self.assertEqual(self.git(clone, "rev-parse", "--is-shallow-repository"), "true")
        return clone

    def test_squash_merge_in_shallow_clone_preserves(self):
        self.squash()
        clone = self.shallow_clone(1)
        with self.assertRaisesRegex(syndicate_git.CleanupError, "shallow clone.*preserved.*--unshallow"):
            self.cleanup(repo=clone, worktree=self.task.parent / "absent task")
        self.assertEqual(self.git(self.remote, "rev-parse", self.task_ref), self.head)
        self.assertEqual(self.git(self.repo, "rev-parse", self.task_ref), self.head)

    def test_squash_merge_with_grafts_preserves(self):
        self.squash()
        grafts = Path(self.git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        (grafts / "info").mkdir(exist_ok=True)
        (grafts / "info" / "grafts").write_text("")
        with self.assertRaisesRegex(syndicate_git.CleanupError, "grafts.*preserved"):
            self.cleanup()
        self.assert_artifacts()

    def test_merge_commit_in_shallow_clone_cleans_up(self):
        # Ancestry needs no content proof; a shallow history can only hide ancestry, never invent it.
        target = self.merge()
        clone = self.shallow_clone(2)
        self.cleanup(repo=clone, worktree=self.task.parent / "absent task")
        self.assertEqual(self.git(self.remote, "branch", "--list", self.branch), "")
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), target)

    def remove_task_branches(self):
        """The remote auto-deleted the task branch; cleanup then runs where none was ever checked out."""
        self.git(self.remote, "update-ref", "-d", self.task_ref)

    def test_squash_merge_without_task_branches_in_fresh_clone_cleans_up(self):
        # No tip is left to prove or delete, so no fetch of the deleted remote branch is attempted.
        target = self.squash()
        self.remove_task_branches()
        fresh = self.task.parent / "fresh"
        self.git(self.task.parent, "clone", "-q", f"file://{self.remote}", str(fresh))
        self.cleanup(repo=fresh, worktree=self.task.parent / "absent task")
        self.assertEqual(self.git(fresh, "rev-parse", "HEAD"), target)
        self.assertIn(f"Remote task branch {self.branch} was already absent.", self.messages)
        self.assertFalse(any("Verified squash/rebase merge" in message for message in self.messages))

    def test_merge_commit_without_task_branches_in_depth_one_clone_cleans_up(self):
        target = self.merge()
        self.remove_task_branches()
        clone = self.shallow_clone(1)
        self.cleanup(repo=clone, worktree=self.task.parent / "absent task")
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), target)
        self.assertIn(f"Remote task branch {self.branch} was already absent.", self.messages)

    def test_git_errors_name_the_subcommand(self):
        with self.assertRaisesRegex(syndicate_git.CleanupError, r"^git check-ref-format failed: "):
            syndicate_git._git(self.repo, "check-ref-format", "refs/heads/a..b")
        with self.assertRaisesRegex(syndicate_git.CleanupError, r"^git rev-parse failed: "):
            syndicate_git._git(self.repo, "-c", "core.quotePath=false", "rev-parse", "--verify", "missing")

    def test_oid_of_other_hash_format_is_rejected_before_deletion(self):
        self.merge()
        self.pr["headRefOid"] = "a" * 64
        with self.assertRaisesRegex(syndicate_git.CleanupError, "missing a valid merge commit or head commit"):
            self.cleanup()
        self.assert_artifacts()

    def test_unknown_object_format_is_rejected(self):
        self.merge()
        run = syndicate_git._git

        def fake_format(path, *args, expected=(0,)):
            if args == ("rev-parse", "--show-object-format"):
                return subprocess.CompletedProcess(args, 0, "sha3\n", "")
            return run(path, *args, expected=expected)

        with patch.object(syndicate_git, "_git", side_effect=fake_format), \
                self.assertRaisesRegex(syndicate_git.CleanupError, "Unsupported Git object format 'sha3'"):
            self.cleanup()
        self.assert_artifacts()

    def test_squash_merge_honors_syndicate_lock_and_series_guard(self):
        self.git(self.repo, "worktree", "lock", "--reason", "syndicate: task-123", str(self.task))
        target = self.squash()
        active = [True]
        calls = []

        def guard(path):
            calls.append(path)
            if active[0]:
                raise syndicate_git.CleanupError("series work/task-123 is still active: task-b")

        with self.assertRaisesRegex(syndicate_git.CleanupError, "still active"):
            self.cleanup(series_guard=guard)
        self.assert_artifacts()
        self.assertIn("locked syndicate: task-123", self.git(self.repo, "worktree", "list", "--porcelain"))
        active[0] = False
        self.cleanup(series_guard=guard)
        self.assert_cleaned(target)
        self.assertEqual(calls, [self.task.resolve()] * 3)

    def test_squash_merge_rejects_foreign_lock(self):
        self.git(self.repo, "worktree", "lock", "--reason", "someone else's", str(self.task))
        self.squash()
        with self.assertRaisesRegex(syndicate_git.CleanupError, "locked by another tool"):
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

    def test_merge_commit_with_local_tip_behind_pr_head_cleans_up(self):
        first = self.head
        self.task_commit("second.txt", "second\n", "second task change")
        self.git(self.integrator, "fetch", "origin")
        target = self.merge()
        self.git(self.task, "reset", "-q", "--hard", first)
        self.cleanup()
        self.assert_cleaned(target)
        self.assertFalse(any("squash/rebase" in message for message in self.messages))

    def test_merge_commit_with_remote_pushed_after_merge_preserves(self):
        self.merge()
        self.git(self.task, "commit", "--allow-empty", "-m", "pushed after merge")
        moved = self.git(self.task, "rev-parse", "HEAD")
        self.git(self.task, "push", "origin", self.branch)
        self.git(self.task, "reset", "-q", "--hard", self.head)
        with self.assertRaisesRegex(syndicate_git.CleanupError,
                                    f"Remote task branch tip {moved} cannot be proven.*preserved"):
            self.cleanup()
        self.assertTrue(self.task.is_dir())
        self.assertEqual(self.git(self.repo, "rev-parse", self.task_ref), self.head)
        self.assertEqual(self.git(self.remote, "rev-parse", self.task_ref), moved)
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
