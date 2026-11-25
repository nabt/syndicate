"""Phase 3 migration rehearsal for the per-user installation plan.

Each test rehearses one step of the release transition runbook
(docs/user-install.md) end to end: the real installer (bin/syndicate-install),
the real launcher of each release, and stand-in `codex`/`claude` CLIs. No live
provider, tmux server, GitHub or user state is involved.

Isolation: every test gets its own disposable HOME, XDG_CONFIG_HOME,
XDG_DATA_HOME, XDG_CACHE_HOME, CODEX_HOME, CLAUDE_CONFIG_DIR, TMPDIR and
TMUX_TMPDIR, with inherited SYNDICATE_*/TMUX*/CODEX_*/CLAUDE_*/GIT_* variables
stripped and PYTHONDONTWRITEBYTECODE=1. PATH is limited to a per-test stand-in
directory plus /usr/bin:/bin, so a real `codex`, `claude` or installed
`syndicate` can never be reached. Every process whose command line or HOME
points into the test directory is killed at cleanup.

Fixture history (local bare "upstream", tags created here):
  L  legacy checkout: the last main commit before the runtime boundary
     (LEGACY_COMMIT), i.e. the code a checkout-symlink install runs today
  A  this worktree's content + marker A, tag v1.0.0
  B  this worktree's content + marker B, tag v1.1.0
"""
import fcntl
import hashlib
import http.client
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "bin" / "syndicate-install"
sys.path.insert(0, str(REPO / "bin"))
import syndicate_release as rel  # noqa: E402

# Last main commit before the runtime boundary (phase 1) and the installer
# (phase 2): what ~/.local/bin/syndicate -> checkout runs before the cutover.
LEGACY_COMMIT = "b48583a"
MARKER = "Fixture release marker: {}"
FIXTURE = {}
PYTHON = os.path.abspath(sys.executable)
LINUX = sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir()


def clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SYNDICATE_", "TMUX", "GIT_", "CODEX_", "CLAUDE_", "XDG_"))
           and k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PATH", "HOME", "TMPDIR")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, PYTHONDONTWRITEBYTECODE="1",
               GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
               GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
               PATH="/usr/bin:/bin")
    env.update(extra)
    return env


def run_git(repo, *args, env=None):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            env=env or FIXTURE["env"])
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def copy_worktree(dest: Path, env) -> None:
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            capture_output=True, check=True, env=env).stdout.split(b"\0")
    for raw in listed:
        name = raw.decode()
        origin = REPO / name
        if name and origin.is_file() and not origin.is_symlink():
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)


def setUpModule():
    base = Path(tempfile.mkdtemp(prefix="syndicate-rehearsal-fixture-"))
    FIXTURE.update(base=base)
    try:
        build_fixture(base)
    except BaseException:
        tearDownModule()  # unittest skips tearDownModule when setUpModule fails
        raise


def build_fixture(base: Path):
    (base / "home").mkdir()
    env = clean_env(HOME=str(base / "home"), TMPDIR=os.environ.get("TMPDIR", tempfile.gettempdir()))
    FIXTURE.update(base=base, env=env)
    src = base / "source checkout"
    upstream = base / "upstream repo.git"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True, env=env)

    def commit(message):
        run_git(src, "add", "-A")
        run_git(src, "commit", "-q", "-m", message)
        return run_git(src, "rev-parse", "HEAD")

    legacy = subprocess.run(["git", "-C", str(REPO), "rev-parse", "-q", "--verify", LEGACY_COMMIT + "^{commit}"],
                            capture_output=True, text=True, env=env).stdout.strip()
    if legacy:
        archive = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", legacy],
                                 capture_output=True, check=True, env=env).stdout
        subprocess.run(["tar", "-x", "-C", str(src)], input=archive, check=True, env=env)
    else:  # history unavailable (shallow clone): the current tree stands in for the legacy code
        copy_worktree(src, env)
    FIXTURE["legacy_source"] = legacy or "current tree (legacy commit unavailable)"
    lcommit = commit("legacy checkout")
    run_git(src, "rm", "-rq", ".")
    copy_worktree(src, env)
    role = src / "roles" / "backend-engineer.md"
    original_role = role.read_text()
    role.write_text(original_role + "\n" + MARKER.format("A") + "\n")
    a = commit("release A")
    run_git(src, "tag", "-a", "v1.0.0", "-m", "release A")
    role.write_text(original_role + "\n" + MARKER.format("B") + "\n")
    b = commit("release B")
    run_git(src, "tag", "-a", "v1.1.0", "-m", "release B")
    subprocess.run(["git", "init", "-q", "--bare", str(upstream)], check=True, env=env)
    run_git(src, "push", "-q", str(upstream), "main", "--tags")
    FIXTURE.update(src=src, upstream=upstream, legacy=lcommit, a=a, b=b)


def tearDownModule():
    base = FIXTURE.get("base")
    if base:
        rel.make_writable(base)
        shutil.rmtree(base, ignore_errors=True)


# --------------------------------------------------------------------------
# Stand-in provider CLIs
# --------------------------------------------------------------------------

# A stand-in `codex`. Modes:
#   app-server   minimal JSON-RPC for the limits monitor (90% remaining)
#   queue        record a live-inbox delivery
#   exec         a headless specialist: record and exit
#   (otherwise)  a project orchestrator. It records its launch (run.json,
#                generated agent configs, pinned prefix) in FAKE_CODEX_CTL/
#                ready.json, then executes commands the test drops into that
#                directory with its own inherited environment, like an agent.
FAKE_CODEX = r'''
import json, os, re, subprocess, sys, time
from pathlib import Path

argv = sys.argv[1:]
LOG = os.environ.get("FAKE_CODEX_LOG")


def record(kind, **data):
    if LOG:
        with open(LOG, "a") as handle:
            handle.write(json.dumps(dict(kind=kind, pid=os.getpid(), argv=argv, **data)) + "\n")


def atomic(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


if argv[:1] == ["app-server"]:
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("id") == 1:
            print(json.dumps({"id": 1, "result": {}}), flush=True)
        elif message.get("id") == 2:
            print(json.dumps({"id": 2, "result": {"rateLimits": {"primary": {
                "usedPercent": 10, "windowDurationMins": 300, "resetsAt": int(time.time()) + 3600}}}}), flush=True)
    sys.exit(0)
if argv[:1] == ["queue"]:
    record("queue", thread=argv[argv.index("--thread") + 1], message=argv[argv.index("--message") + 1])
    sys.exit(0)
if argv[:1] == ["exec"]:
    record("specialist")
    print("specialist finished")
    sys.exit(0)
if argv[:1] == ["--version"]:
    print("codex-cli 0.0.0-fixture")
    sys.exit(0)

project = argv[argv.index("-C") + 1] if "-C" in argv else os.getcwd()
raw = next((a.split("=", 1)[1] for a in argv if a.startswith("developer_instructions=")), '""')
instructions = json.loads(raw)
configs = [json.loads(a.split("=", 1)[1]) for a in argv if re.match(r"agents\.[^.=]+\.config_file=", a)]
prefix = re.search(r"Syndicate executable prefix: (.+)", instructions)
run_path = Path(project, ".syndicate", "run.json")
launch = dict(pid=os.getpid(), project=project, cwd=os.getcwd(), argv=argv,
              run=json.loads(run_path.read_text()) if run_path.is_file() else None,
              prefix=prefix.group(1) if prefix else None, instructions=instructions,
              configs={c: Path(c).read_text() for c in configs},
              env={k: os.environ.get(k) for k in ("HOME", "PYTHONDONTWRITEBYTECODE", "SYNDICATE_RUN_ID")})
record("orchestrator", run=launch["run"])
ctl = os.environ.get("FAKE_CODEX_CTL")
if not ctl:
    sys.exit(0)
ctl = Path(ctl)
atomic(ctl / "ready.json", launch)
n, deadline = 1, time.time() + 300
while time.time() < deadline:
    command = ctl / f"cmd-{n:04d}.json"
    if command.exists():
        spec = json.loads(command.read_text())
        env = dict(os.environ, **(spec.get("env") or {}))
        try:
            done = subprocess.run(spec["argv"], cwd=spec.get("cwd") or project, env=env, capture_output=True,
                                  text=True, timeout=180, input=spec.get("input"),
                                  stdin=None if spec.get("input") is not None else subprocess.DEVNULL)
            result = dict(rc=done.returncode, out=done.stdout, err=done.stderr)
        except Exception as error:
            result = dict(rc=-1, out="", err=repr(error))
        atomic(ctl / f"res-{n:04d}.json", result)
        n += 1
        continue
    if (ctl / "exit").exists():
        sys.exit(int((ctl / "exit").read_text() or 0))
    time.sleep(0.05)
sys.exit(3)
'''

FAKE_CLAUDE = r'''
import json, os, sys
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\n")
print("claude stand-in")
'''


# --------------------------------------------------------------------------
# Durable-state inventory and legacy-context inventory (mechanizable steps)
# --------------------------------------------------------------------------

def file_digests(roots: dict) -> dict:
    """{label/relpath: sha256 or '-> target'} for every file and symlink under each root."""
    found = {}
    for label, root in roots.items():
        root = Path(root)
        if root.is_file():
            found[label] = hashlib.sha256(root.read_bytes()).hexdigest()
            continue
        for directory, dirs, files in os.walk(root):
            for name in sorted(dirs + files):
                path = Path(directory) / name
                key = f"{label}/{path.relative_to(root)}"
                if path.is_symlink():
                    found[key] = "-> " + os.readlink(path)
                elif path.is_file():
                    found[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def record_inventory(home: Path, projects) -> dict:
    """IDs, states and revisions of every durable record, read straight from disk."""
    portfolio = home / ".syndicate" / "portfolio"
    decisions = (read_json(portfolio / "decisions.json") or {}).get("decisions", [])
    coordination = read_json(portfolio / "coordination.json") or {}
    messages = read_json(portfolio / "messages.json") or []
    inventory = dict(
        decisions={d["id"]: (d["state"], d["answer_revision"], d["key"], d["task_id"]) for d in decisions},
        requests={r["id"]: (r["state"], r.get("session_id"), r["key"], r["task_id"])
                  for r in coordination.get("requests", [])},
        sessions={s["id"]: (s["state"], s["source_path"], s["task_id"]) for s in coordination.get("sessions", [])},
        briefs={p: b["revision"] for p, b in coordination.get("briefs", {}).items()},
        messages={m["id"]: (m["state"], m["key"], m["session_id"]) for m in messages},
        registry=sorted(e.get("path") for e in
                       (read_json(home / ".syndicate" / "projects.json", {}) or {}).get("projects", [])),
        tasks={})
    for project in projects:
        status = read_json(Path(project) / ".syndicate" / "status.json") or {}
        inventory["tasks"][str(project)] = {t["id"]: t.get("summary") for t in status.get("tasks", [])
                                            if isinstance(t, dict) and t.get("id")}
    return inventory


def legacy_context_inventory(checkout: Path, roots, tmux_sockets=(), env=None) -> dict:
    """Every reference to an old checkout path: {category: sorted locations}.

    Searches regular files (text, up to 4 MiB) and symlinks under each root,
    then the start commands of panes on each given tmux socket. The checkout
    itself is skipped. Categories follow the runbook inventory: entry point,
    shell profile, generated settings, per-run records, handoffs, codex hook,
    hook backup, installer records, approvals, portfolio state, tmux, other.
    """
    needles = {str(checkout), os.path.realpath(checkout)}
    # The path itself or anything below it, never a longer sibling name ("checkout-other").
    pattern = re.compile(b"|".join(re.escape(n.encode()) for n in needles) + rb"(?![\w.-])")
    skip = {os.path.realpath(checkout)}
    found = {}

    def add(category, where):
        found.setdefault(category, set()).add(str(where))

    def category(path: Path) -> str:
        text = str(path)
        if "/.syndicate/.generated/" in text:
            return "generated settings"
        if "/.syndicate/runs/" in text or path.name == "run.json":
            return "per-run records"
        if "/.syndicate/handoffs/" in text:
            return "handoffs"
        if path.name.startswith("hooks.json.syndicate-backup-"):
            return "hook backup"
        if path.name == "hooks.json":
            return "codex hook"
        if path.name in ("entrypoints.jsonl", "activations.jsonl"):
            return "installer records"
        if path.suffix == ".rules" or (path.name.startswith("settings") and path.suffix == ".json"):
            return "approvals"
        if "/.syndicate/portfolio/" in text or "/.syndicate/" in text:
            return "portfolio state"
        if path.name.startswith(".") and path.parent.name in ("home",) or path.name in (".bashrc", ".zshrc", ".profile"):
            return "shell profile"
        return "other"

    for root in roots:
        for directory, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if os.path.realpath(Path(directory) / d) not in skip and d != ".git"]
            for name in files + [d for d in dirs if (Path(directory) / d).is_symlink()]:
                path = Path(directory) / name
                if path.is_symlink():
                    target = os.path.realpath(path)
                    if any(target == n or target.startswith(n.rstrip("/") + "/") for n in needles):
                        add("entry point" if path.parent.name == "bin" else "symlink", path)
                    continue
                try:
                    if path.stat().st_size > 4 << 20:
                        continue
                    data = path.read_bytes()
                except OSError:
                    continue
                if pattern.search(data):
                    add(category(path), path)
    for sock in tmux_sockets:
        listing = subprocess.run(["tmux", "-S", str(sock), "list-panes", "-a", "-F",
                                  "#{session_name}:#{window_name} #{pane_start_command}"],
                                 capture_output=True, text=True, env=env, timeout=30)
        for line in listing.stdout.splitlines():
            if pattern.search(line.encode()):
                add("tmux", f"{sock} {line.split(' ', 1)[0]}")
    return {k: sorted(v) for k, v in found.items()}


# --------------------------------------------------------------------------
# Test harness
# --------------------------------------------------------------------------

class TeamRun:
    """A launched team whose stand-in orchestrator executes commands on request."""

    def __init__(self, case, proc, ctl, output, master=None):
        self.case, self.proc, self.ctl, self.output, self.master = case, proc, ctl, output, master
        self.n = 0
        self.launch = None
        self.buffer = bytearray()
        if master is not None:
            self.reader = threading.Thread(target=self._drain, daemon=True)
            self.reader.start()

    def _drain(self):
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            self.buffer.extend(chunk)

    def text(self):
        if self.output is None:
            return self.buffer.decode(errors="replace")
        return Path(self.output).read_text(errors="replace") if Path(self.output).exists() else ""

    def ready(self, timeout=90):
        deadline = time.time() + timeout
        path = self.ctl / "ready.json"
        while not path.exists():
            if self.proc.poll() is not None:
                self.case.fail(f"team exited {self.proc.returncode} before its orchestrator started:\n{self.text()}")
            if time.time() > deadline:
                self.case.fail(f"orchestrator did not start:\n{self.text()}")
            time.sleep(0.05)
        self.launch = json.loads(path.read_text())
        return self.launch

    @property
    def session(self):
        return self.launch["run"]["session_id"]

    @property
    def task(self):
        return self.launch["run"]["task_id"]

    def run(self, argv, *, ok=True, cwd=None, env=None, timeout=180):
        """Run argv inside the orchestrator process (its environment and cwd)."""
        self.n += 1
        spec = dict(argv=[str(a) for a in argv], cwd=str(cwd) if cwd else None, env=env)
        tmp = self.ctl / f"cmd-{self.n:04d}.json.tmp"
        tmp.write_text(json.dumps(spec))
        os.replace(tmp, self.ctl / f"cmd-{self.n:04d}.json")
        result_path = self.ctl / f"res-{self.n:04d}.json"
        deadline = time.time() + timeout
        while not result_path.exists():
            if self.proc.poll() is not None:
                self.case.fail(f"team exited during {argv}:\n{self.text()}")
            if time.time() > deadline:
                self.case.fail(f"agent command timed out: {argv}")
            time.sleep(0.05)
        result = json.loads(result_path.read_text())
        if ok and result["rc"]:
            self.case.fail(f"agent command {argv} failed ({result['rc']}):\n{result['err']}{result['out']}")
        if ok is False and result["rc"] == 0:
            self.case.fail(f"agent command {argv} unexpectedly succeeded:\n{result['out']}")
        return result

    def finish(self, code=0, timeout=120):
        (self.ctl / "exit").write_text(str(code))
        try:
            self.proc.wait(timeout=timeout)
        finally:
            if self.master is not None:
                self.reader.join(timeout=10)
                os.close(self.master)
                self.master = None
        return self.proc.returncode


class RehearsalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="syndicate-rehearsal-"))
        self.addCleanup(self._cleanup)
        for name in ("home", "config", "data home", "cache", "codex", "claude", "tmp", "tmux", "elsewhere",
                     "fakebin", "ctl", "logs", "projects", "backups"):
            (self.tmp / name).mkdir()
        self.home = self.tmp / "home"
        fakebin = self.tmp / "fakebin"
        for name, body in (("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE)):
            (fakebin / name).write_text(f"#!{PYTHON}\n{body}")
            (fakebin / name).chmod(0o755)
        # Legacy `#!/usr/bin/env python3` launchers use the test interpreter.
        (fakebin / "python3").symlink_to(PYTHON)
        real_path = os.environ.get("PATH", "")
        for tool in ("jq",):
            found = shutil.which(tool, path=real_path)
            if found and not shutil.which(tool, path="/usr/bin:/bin"):
                (fakebin / tool).symlink_to(os.path.realpath(found))
        self.codex_log = self.tmp / "logs" / "codex.jsonl"
        self.env = clean_env(
            HOME=str(self.home), XDG_CONFIG_HOME=str(self.tmp / "config"),
            XDG_DATA_HOME=str(self.tmp / "data home"), XDG_CACHE_HOME=str(self.tmp / "cache"),
            CODEX_HOME=str(self.tmp / "codex"), CLAUDE_CONFIG_DIR=str(self.tmp / "claude"),
            TMPDIR=str(self.tmp / "tmp"), TMUX_TMPDIR=str(self.tmp / "tmux"),
            PATH=f"{fakebin}:/usr/bin:/bin", FAKE_CODEX_LOG=str(self.codex_log),
            FAKE_CLAUDE_LOG=str(self.tmp / "logs" / "claude.jsonl"))
        self.root = self.tmp / "data home" / "syndicate"
        self.paths = rel.Paths(self.root, self.home / ".local" / "bin",
                               self.tmp / "cache" / "syndicate" / "source.git")
        self.shim = self.paths.shim
        self.user_config = self.tmp / "config" / "syndicate" / "syndicate.conf"
        self.limits = self.home / ".syndicate" / "limits"

    # -- cleanup ----------------------------------------------------------
    def scoped_pids(self):
        marker = str(self.tmp).encode()
        home = f"HOME={self.home}".encode()
        pids = []
        for entry in Path("/proc").iterdir() if Path("/proc").is_dir() else ():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
                environ = (entry / "environ").read_bytes()
            except OSError:
                continue
            if marker in cmdline or home in environ.split(b"\0"):
                pids.append(int(entry.name))
        return pids

    def _cleanup(self):
        for sock in self.tmp.joinpath("tmux").glob("*"):
            subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True, env=self.env, timeout=30)
        for attempt in range(50):
            pids = self.scoped_pids()
            if not pids:
                break
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            time.sleep(0.1)
        rel.make_writable(self.tmp)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- running things -----------------------------------------------------
    def installer(self, *args, ok=True):
        result = subprocess.run([PYTHON, "-B", str(INSTALLER), *args], cwd=self.tmp / "elsewhere",
                                env=self.env, capture_output=True, text=True, timeout=300)
        if ok and result.returncode:
            self.fail(f"syndicate-install {' '.join(args)} failed:\n{result.stderr}")
        if ok is False and result.returncode == 0:
            self.fail(f"syndicate-install {' '.join(args)} unexpectedly succeeded:\n{result.stdout}")
        return result

    def install(self, tag, *extra, ok=True):
        return self.installer("install", "--tag", tag, "--upstream", str(FIXTURE["upstream"]),
                              "--python", PYTHON, *extra, ok=ok)

    def current(self):
        return rel.current_commit(self.paths)

    def release_dir(self, commit):
        return Path(os.path.realpath(self.root / "releases" / commit))

    def syn(self, *args, prefix=None, ok=True, env=None, cwd=None, timeout=180):
        argv = list(prefix or [str(self.shim)]) + [str(a) for a in args]
        result = subprocess.run(argv, cwd=cwd or self.tmp / "elsewhere", env=env or self.env, capture_output=True,
                                text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        if ok and result.returncode:
            self.fail(f"syndicate {' '.join(map(str, args))} failed ({result.returncode}):\n{result.stderr}{result.stdout}")
        if ok is False and result.returncode == 0:
            self.fail(f"syndicate {' '.join(map(str, args))} unexpectedly succeeded:\n{result.stdout}")
        return result

    def synj(self, *args, **kwargs):
        return json.loads(self.syn(*args, **kwargs).stdout)

    def version(self, prefix=None):
        return self.synj("--version", "--json", prefix=prefix)

    def make_project(self, name):
        project = self.tmp / "projects" / name
        project.mkdir(parents=True)
        run_git(project, "init", "-q", "-b", "main", env=self.env)
        (project / "README.md").write_text(f"# {name}\n")
        run_git(project, "add", "-A", env=self.env)
        run_git(project, "commit", "-q", "-m", "init", env=self.env)
        return project

    def launch_team(self, project, *args, prefix=None, terminal=False, env=None):
        """Start `team task --project PROJECT ARGS` with a controllable stand-in orchestrator."""
        ctl = Path(tempfile.mkdtemp(prefix="team-", dir=self.tmp / "ctl"))
        run_env = dict(env or self.env, FAKE_CODEX_CTL=str(ctl))
        argv = list(prefix or [str(self.shim)]) + ["team", "task", "--project", str(project), *args]
        if terminal:
            master, slave = pty.openpty()
            try:
                proc = subprocess.Popen(argv, cwd=self.tmp / "elsewhere", env=run_env, stdin=slave, stdout=slave,
                                        stderr=slave, start_new_session=True)
            finally:
                os.close(slave)
            team = TeamRun(self, proc, ctl, None, master=master)
        else:
            output = ctl.with_suffix(".out")
            with open(output, "wb") as handle:
                proc = subprocess.Popen(argv, cwd=self.tmp / "elsewhere", env=run_env, stdin=subprocess.DEVNULL,
                                        stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
            team = TeamRun(self, proc, ctl, output)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        return team

    def launch_chief(self, *args, prefix=None):
        """Start `chief-of-staff ARGS` with a controllable stand-in Codex Chief."""
        ctl = Path(tempfile.mkdtemp(prefix="chief-", dir=self.tmp / "ctl"))
        output = ctl.with_suffix(".out")
        argv = list(prefix or [str(self.shim)]) + ["chief-of-staff", *args]
        with open(output, "wb") as handle:
            proc = subprocess.Popen(argv, cwd=self.tmp / "elsewhere", env=dict(self.env, FAKE_CODEX_CTL=str(ctl)),
                                    stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        return TeamRun(self, proc, ctl, output)

    def codex_records(self, kind=None):
        if not self.codex_log.exists():
            return []
        records = [json.loads(line) for line in self.codex_log.read_text().splitlines() if line.strip()]
        return [r for r in records if kind is None or r["kind"] == kind]

    def wait_for(self, predicate, what, timeout=60, interval=0.1):
        deadline = time.time() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.time() > deadline:
                self.fail(f"timed out waiting for {what}")
            time.sleep(interval)

    def write_handoff(self, project, task_id, name="rehearsal-checkpoint"):
        """A resumable checkpoint for task_id, as a drained session leaves one."""
        handoff = project / ".syndicate" / "handoffs" / name
        handoff.mkdir(parents=True, exist_ok=True)
        status = read_json(project / ".syndicate" / "status.json")
        status.setdefault("current", {})["task_id"] = task_id
        (handoff / "status.json").write_text(json.dumps(status, indent=2) + "\n")
        (handoff / "checkpoint.md").write_text(f"Checkpoint for {task_id}: request incorporation pending.\n")
        (handoff / "handoff.md").write_text(f"# Handoff\n\nResume task {task_id}; see checkpoint.md.\n")
        return handoff

    # -- dashboard -----------------------------------------------------------
    def start_web(self, port=0, no_monitor=False):
        env = dict(self.env, PYTHONUNBUFFERED="1")
        argv = [str(self.shim), "web", "--port", str(port)] + (["--no-monitor"] if no_monitor else [])
        proc = subprocess.Popen(argv, cwd=self.tmp / "elsewhere", env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.stderr.close)
        # Bounded wait for the banner (the pipe is empty before the first read).
        ready, _, _ = select.select([proc.stdout], [], [], 60)
        line = proc.stdout.readline() if ready else ""
        if "http://127.0.0.1:" not in line:
            proc.poll() is None and proc.kill()
            proc.wait(timeout=30)
            self.fail(f"web did not start: {line}{proc.stderr.read()}")
        return proc, int(line.split("http://127.0.0.1:", 1)[1].split("/", 1)[0])

    def get(self, port, path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    # -- monitor -------------------------------------------------------------
    def monitor_lock_state(self):
        lock = self.limits / "monitor.lock"
        if not lock.exists():
            return "free"
        with open(lock, "rb") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return "held"
            fcntl.flock(handle, fcntl.LOCK_UN)
            return "free"

    def monitor_lock_owners(self):
        """PIDs holding an open descriptor on monitor.lock, with their command lines."""
        lock = os.path.realpath(self.limits / "monitor.lock")
        owners = {}
        for pid in self.scoped_pids():
            try:
                fds = os.listdir(f"/proc/{pid}/fd")
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            for fd in fds:
                try:
                    if os.readlink(f"/proc/{pid}/fd/{fd}") == lock:
                        owners[pid] = [a.decode() for a in cmdline if a]
                except OSError:
                    continue
        return owners

    def monitor_processes(self):
        procs = {}
        for pid in self.scoped_pids():
            try:
                args = [a.decode() for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
            except OSError:
                continue
            if "_limits-monitor" in args:
                procs[pid] = args
        return procs

    def stop_monitor(self, prefix=None):
        """Runbook: `limits --stop`, then wait for verified lock release (stop returns first)."""
        pids = set(self.monitor_processes())
        started = time.time()
        result = self.syn("limits", "--stop", prefix=prefix)
        self.assertIn("stop requested", result.stdout)
        self.assertLess(time.time() - started, 30)
        self.wait_for(lambda: self.monitor_lock_state() == "free", "monitor lock release", timeout=30)
        self.wait_for(lambda: not (pids & set(self.monitor_processes())), "monitor exit", timeout=30)

    # -- legacy home -----------------------------------------------------------
    def legacy_checkout(self, name="syndicate checkout", commit=None):
        checkout = self.home / "workspace" / name
        checkout.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", str(FIXTURE["upstream"]), str(checkout)], check=True,
                       env=self.env, capture_output=True)
        run_git(checkout, "reset", "-q", "--hard", commit or FIXTURE["legacy"], env=self.env)
        return checkout

    def seed_legacy_home(self, handoff="off"):
        """A checkout-symlink install with config, overrides, handoffs and pending records.

        Records are written by the legacy launcher itself, through a real team
        session that is then drained (its CLI exits): the decision, message and
        requests below are bound to that stopped owner.
        """
        checkout = self.legacy_checkout()
        self.shim.parent.mkdir(parents=True, exist_ok=True)
        self.shim.symlink_to(checkout / "bin" / "syndicate")
        self.user_config.parent.mkdir(parents=True, exist_ok=True)
        # Current form, plus a user-level role choice (applies to every project).
        self.user_config.write_text(
            "# user-wide settings\ntmux = off\n"
            f"handoff = {handoff}\ndefault = codex\nqa-engineer = claude\n"
            "codex-model_verbosity = low\ninbox-interval = 10\n")
        p1 = self.make_project("alpha project")
        local = p1 / ".syndicate"
        (local / "roles").mkdir(parents=True)
        (local / "syndicate.conf").write_text("# project layer\ncode-reviewer = claude\nhandoff-reserve = 20\n")
        (local / "roles" / "backend-engineer.md").write_text(
            "---\nname: backend-engineer\n---\nProject override: alpha backend rules.\n")
        (local / "roles" / "devops-engineer.append.md").write_text("Alpha deploys check migrations.\n")
        p2 = self.make_project("beta project")
        (p2 / ".syndicate").mkdir()
        legacy_config = p2 / ".syndicate" / "config"
        legacy_config.write_text("# legacy project config name\ndefault = claude\nhandoff-reserve = 15\n")
        legacy_config.chmod(0o640)
        # Runbook step 1: snapshot the legacy project config before any lookup can rename it.
        backup = self.tmp / "backups" / "beta.syndicate.config"
        shutil.copy2(legacy_config, backup)
        legacy = [str(self.shim)]
        old = self.launch_team(p1, "legacy task", prefix=legacy)
        old.ready()
        session, task = old.session, old.task
        old.run(legacy + ["decisions", "request", "Which database should alpha use?", "--key", "database",
                          "-C", p1, "--option", "postgres", "--option", "sqlite", "--blocking"])
        old.run(legacy + ["decisions", "request", "Ship on Friday?", "--key", "ship-date", "-C", p1])
        old.run(legacy + ["messages", "send", "Legacy progress report", "--key", "progress", "-C", p1,
                          "--session", session])
        decisions = {d["key"]: d for d in self.synj("decisions", "list", "--json", prefix=legacy)}
        self.syn("decisions", "answer", decisions["ship-date"]["id"], "Yes, Friday", prefix=legacy)
        pending = self.synj("requests", "send", "Please add migration tests", "--key", "tests",
                            "-C", p1, "--task", task, prefix=legacy)
        done = self.synj("requests", "send", "Note the schema", "--key", "schema", "-C", p1, "--task", task,
                         prefix=legacy)
        old.run(legacy + ["requests", "acknowledge", done["id"], "-C", p1, "--task", task, "--session", session,
                          "--note", "schema noted by legacy owner"])
        self.syn("briefs", "update", "--project", p1, "--goal", "Ship alpha safely", "--priority", "high",
                 prefix=legacy)
        self.assertEqual(pending["session_id"], session)
        handoff = self.write_handoff(p1, task)
        pid = old.launch["pid"]
        self.assertEqual(old.finish(), 0, old.text())  # drained: the legacy CLI exits
        return dict(checkout=checkout, p1=p1, p2=p2, legacy=legacy, legacy_config_backup=backup,
                    session=session, task=task, pending=pending["id"], acknowledged=done["id"],
                    decisions={k: d["id"] for k, d in decisions.items()}, handoff=handoff, old_pid=pid,
                    old_launch=old.launch)

    def effective_config(self, prefix, projects):
        """`config` output (winning layers and the full stack), with and without $SYNDICATE_RUNTIME."""
        captured = {"global": self.syn("config", "--global", prefix=prefix).stdout}
        for project in projects:
            for runtime in (None, "claude"):
                env = dict(self.env, SYNDICATE_RUNTIME=runtime) if runtime else None
                for local in (False, True):
                    args = ["config", "-C", project] + (["--local"] if local else [])
                    captured[f"{project.name}|{runtime}|{local}"] = self.syn(*args, prefix=prefix, env=env).stdout
        return captured

    def durable_roots(self, projects):
        roots = {"syndicate-state": self.home / ".syndicate", "user-config": self.tmp / "config"}
        for project in projects:
            roots[f"project:{project.name}"] = project / ".syndicate"
        return roots


# --------------------------------------------------------------------------
# 1. Legacy checkout install -> installed release
# --------------------------------------------------------------------------

class LegacyMigration(RehearsalCase):
    def test_legacy_checkout_install_migrates_with_config_and_records_preserved(self):
        seed = self.seed_legacy_home()
        p1, p2, legacy = seed["p1"], seed["p2"], seed["legacy"]
        legacy_config = p2 / ".syndicate" / "config"
        self.assertTrue(legacy_config.is_file(), "nothing may read beta's config before its snapshot")
        before_config = self.effective_config(legacy, [p1, p2])
        # The lookup renamed the legacy file; its content and mode are exactly the snapshot.
        renamed = p2 / ".syndicate" / "syndicate.conf"
        self.assertFalse(legacy_config.exists())
        self.assertEqual(renamed.read_bytes(), seed["legacy_config_backup"].read_bytes())
        self.assertEqual(stat.S_IMODE(renamed.stat().st_mode), 0o640)
        # Winning sources: project > user > $SYNDICATE_RUNTIME > built-in defaults.
        self.assertIn("code-reviewer = claude", before_config[f"{p1.name}|None|False"])
        self.assertIn("handoff-reserve = 20", before_config[f"{p1.name}|None|False"])
        self.assertIn("default = claude", before_config[f"{p2.name}|None|False"])
        self.assertIn("overridden by", before_config[f"{p2.name}|claude|True"])

        projects = [p1, p2]
        roots = self.durable_roots(projects)
        files_before = file_digests(roots)
        records_before = record_inventory(self.home, projects)
        self.assertEqual(len(records_before["decisions"]), 2)
        self.assertEqual(len(records_before["requests"]), 2)
        self.assertEqual(len(records_before["messages"]), 1)
        self.assertEqual(records_before["requests"][seed["pending"]][:2], ("queued", seed["session"]))

        self.install("v1.0.0")
        a = FIXTURE["a"]
        self.assertTrue(rel.is_managed_shim(self.shim))
        (entry,) = rel.read_jsonl(self.paths.entrypoints)
        self.assertEqual((entry["kind"], entry["target"]), ("symlink", str(seed["checkout"] / "bin" / "syndicate")))
        version = self.version()
        self.assertEqual((version["mode"], version["commit"], version.get("tag")), ("release", a, "v1.0.0"))
        # The installer never writes config or state: every durable byte is unchanged.
        self.assertEqual(file_digests(roots), files_before)
        self.assertEqual(record_inventory(self.home, projects), records_before)
        # Effective config, including winning sources and the runtime override, is identical.
        self.assertEqual(self.effective_config(None, [p1, p2]), before_config)

        # The installed release reads every legacy record.
        listed = {d["id"]: d for d in self.synj("decisions", "list", "--all", "--json")}
        self.assertEqual(set(listed), set(records_before["decisions"]))
        self.assertEqual(listed[seed["decisions"]["ship-date"]]["answer"], "Yes, Friday")
        requests = {r["id"]: r for r in self.synj("requests", "list", "--all", "--json")}
        self.assertEqual(set(requests), set(records_before["requests"]))
        self.assertEqual(requests[seed["pending"]]["delivery_state"], "failed")  # its owner is stopped
        self.assertEqual(requests[seed["acknowledged"]]["state"], "acknowledged")
        self.assertEqual([m["id"] for m in self.synj("messages", "list", "--all")],
                         list(records_before["messages"]))
        self.assertEqual(self.synj("briefs", "show", "--project", p1, "--json")["goal"], "Ship alpha safely")

        # A normal installed launch on the same source adds only its own session and task.
        team = self.launch_team(p1, "installed task")
        launch = team.ready()
        self.assertEqual(launch["run"]["release"]["commit"], a)
        configs = "\n".join(launch["configs"].values())
        # Project role override and append, and the user/project CLI choices (relays), take effect.
        for text in ("Project override: alpha backend rules.", "Alpha deploys check migrations.",
                     "`code-reviewer` role, which is configured for the claude CLI",
                     "`qa-engineer` role, which is configured for the claude CLI"):
            self.assertTrue(text in configs, f"{text!r} missing from the installed launch's agent configs")
        self.assertNotIn(str(seed["checkout"]), json.dumps(launch["configs"]) + launch["instructions"])
        self.assertEqual(team.finish(), 0)
        after = record_inventory(self.home, projects)
        self.assertEqual(after["decisions"], records_before["decisions"])
        self.assertEqual(after["messages"], records_before["messages"])
        self.assertEqual(after["briefs"], records_before["briefs"])
        self.assertEqual(after["registry"], records_before["registry"])
        self.assertEqual(set(after["requests"]), set(records_before["requests"]))
        self.assertEqual(after["requests"][seed["acknowledged"]], records_before["requests"][seed["acknowledged"]])
        # The stopped legacy owner's queued request may be marked failed (superseded), never lost or rebound.
        self.assertIn(after["requests"][seed["pending"]][0], ("queued", "failed"))
        self.assertEqual(after["requests"][seed["pending"]][1], seed["session"])
        new_sessions = set(after["sessions"]) - set(records_before["sessions"])
        self.assertEqual(len(new_sessions), 1)
        self.assertEqual(after["sessions"][new_sessions.pop()][1:], (str(p1), launch["run"]["task_id"]))
        self.assertEqual(set(after["sessions"]) & set(records_before["sessions"]), set(records_before["sessions"]))
        self.assertEqual(after["tasks"][str(p2)], records_before["tasks"][str(p2)])
        self.assertEqual(set(after["tasks"][str(p1)]) - set(records_before["tasks"][str(p1)]),
                         {launch["run"]["task_id"]})
        files_after = file_digests(roots)
        for key, digest in files_before.items():
            if "/handoffs/" in key or "/roles/" in key or key.endswith(("syndicate.conf",)) or key == "user-config/syndicate/syndicate.conf":
                self.assertEqual(files_after.get(key), digest, key)
        self.assertEqual(list(self.release_dir(a).rglob("__pycache__")), [])


# --------------------------------------------------------------------------
# 2. Request bound to a drained owner, rebound to the installed replacement
# --------------------------------------------------------------------------

@unittest.skipUnless(LINUX, "needs /proc and a PTY for the supervised live inbox")
class RequestRebinding(RehearsalCase):
    def test_pending_request_is_rebound_delivered_and_acknowledged_once(self):
        seed = self.seed_legacy_home()
        p1, old_session, task = seed["p1"], seed["session"], seed["task"]
        self.install("v1.0.0")
        a = FIXTURE["a"]
        # The replacement runs supervised so the live inbox delivers requests.
        self.user_config.write_text(self.user_config.read_text().replace("handoff = off", "handoff = on"))

        # The old owner is stopped: its process is gone and its record is not live.
        with self.assertRaises(ProcessLookupError):
            os.kill(seed["old_pid"], 0)
        sessions = {s["id"]: s for s in self.synj("sessions", "--all", "--json")}
        self.assertFalse(sessions[old_session]["live"])
        self.assertEqual(sessions[old_session]["liveness"], "dead")

        team = self.launch_team(p1, "-x", "codex", "--handoff", seed["handoff"], terminal=True)
        self.addCleanup(lambda: self.monitor_processes() and self.stop_monitor())
        launch = team.ready()
        new_session = team.session
        self.assertEqual(team.task, task, "the replacement resumes the same task")
        self.assertNotEqual(new_session, old_session)
        self.assertEqual(launch["run"]["release"]["commit"], a)
        prefix = shlex.split(launch["prefix"])
        self.assertEqual(prefix[1:], ["-I", "-B", str(self.release_dir(a) / "bin" / "syndicate")])
        live = {s["id"]: s for s in self.synj("sessions", "--all", "--json")}
        self.assertTrue(live[new_session]["live"])
        self.assertEqual((live[new_session]["source_path"], live[new_session]["task_id"]), (str(p1), task))
        self.assertFalse(live[old_session]["live"])

        # Inspect before retrying: still bound to the old owner and not deliverable.
        request = next(r for r in self.synj("requests", "list", "--all", "--json") if r["id"] == seed["pending"])
        self.assertEqual(request["session_id"], old_session)
        self.assertIn(request["state"], ("queued", "failed"))
        thread = str(uuid.uuid4())
        team.run(prefix + ["live", "attach", "-C", p1], env={"CODEX_THREAD_ID": thread})

        rebound = self.synj("requests", "retry", seed["pending"])
        self.assertEqual((rebound["state"], rebound["session_id"]), ("queued", new_session))
        # Retrying again while the live replacement owns it is refused: no duplicate incorporation.
        again = self.syn("requests", "retry", seed["pending"], ok=False)
        self.assertIn("still awaiting acknowledgment", again.stderr)
        # Already-acknowledged requests are left alone.
        self.assertIn("Only pending or failed", self.syn("requests", "retry", seed["acknowledged"], ok=False).stderr)

        def delivered():
            return [r for r in self.codex_records("queue") if seed["pending"] in r["message"]]
        (delivery,) = self.wait_for(delivered, "live inbox delivery of the rebound request", timeout=60)
        self.assertEqual(delivery["thread"], thread)
        events = json.loads(delivery["message"].split("New durable inbox events: ", 1)[1].split("\n", 1)[0])
        event = next(e for e in events if e["payload"]["record_id"] == seed["pending"])
        self.assertEqual(event["recipient"]["session_id"], new_session)

        # Acknowledgment under the old token is refused; under the new token it succeeds.
        stale = team.run(prefix + ["requests", "acknowledge", seed["pending"], "-C", p1, "--task", task,
                                   "--session", old_session, "--note", "stale"], ok=False)
        self.assertIn("owning active task/session", stale["err"])
        done = json.loads(team.run(prefix + ["requests", "acknowledge", seed["pending"], "-C", p1, "--task", task,
                                             "--session", new_session, "--note", "tests added"])["out"])
        self.assertEqual((done["state"], done["session_id"], done["note"]), ("acknowledged", new_session, "tests added"))
        self.assertIn("Only pending or failed", self.syn("requests", "retry", seed["pending"], ok=False).stderr)
        time.sleep(2.5)  # two more supervisor ticks: nothing is redelivered
        self.assertEqual(len(delivered()), 1)
        records = {r["id"]: r for r in self.synj("requests", "list", "--all", "--json")}
        self.assertEqual([h["action"] for h in records[seed["pending"]]["history"]], ["retry", "acknowledge"])
        untouched = records[seed["acknowledged"]]
        self.assertEqual((untouched["state"], untouched["session_id"]), ("acknowledged", old_session))
        self.assertEqual(len(untouched["history"]), 1)
        team.finish()
        self.assertEqual(team.proc.returncode, 0, team.text())
        self.stop_monitor()


# --------------------------------------------------------------------------
# 3. State-preserving restart and rollback after writes
# --------------------------------------------------------------------------

class RestartAndRollback(RehearsalCase):
    def test_records_written_under_a_and_b_survive_rollback_and_are_readable_by_a(self):
        a, b = FIXTURE["a"], FIXTURE["b"]
        self.user_config.parent.mkdir(parents=True)
        self.user_config.write_text("tmux = off\nhandoff = off\ndefault = codex\n")
        project = self.make_project("gamma")
        self.install("v1.0.0")
        shim = [str(self.shim)]

        # Session under A.
        team_a = self.launch_team(project, "work under A")
        team_a.ready()
        self.assertEqual(team_a.launch["run"]["release"]["commit"], a)
        d1 = json.loads(team_a.run(shim + ["decisions", "request", "Use A's plan?", "--key", "plan-a", "-C",
                                           project])["out"])
        m1 = json.loads(team_a.run(shim + ["messages", "send", "Progress under A", "--key", "a", "-C", project,
                                           "--session", team_a.session])["out"])
        r1 = self.synj("requests", "send", "Request under A", "--key", "r-a", "-C", project, "--task", team_a.task)
        team_a.run(shim + ["requests", "acknowledge", r1["id"], "-C", project, "--task", team_a.task,
                           "--session", team_a.session, "--note", "done under A"])
        self.syn("briefs", "update", "--project", project, "--goal", "Goal from A")
        self.assertEqual(team_a.finish(), 0)

        # Activate B and restart: a new session writes more.
        self.install("v1.1.0")
        self.assertEqual(self.version()["commit"], b)
        team_b = self.launch_team(project, "work under B")
        team_b.ready()
        self.assertEqual(team_b.launch["run"]["release"]["commit"], b)
        d2 = json.loads(team_b.run(shim + ["decisions", "request", "Use B's plan?", "--key", "plan-b", "-C",
                                           project])["out"])
        m2 = json.loads(team_b.run(shim + ["messages", "send", "Progress under B", "--key", "b", "-C", project,
                                           "--session", team_b.session])["out"])
        r2 = self.synj("requests", "send", "Request under B", "--key", "r-b", "-C", project, "--task", team_b.task)
        self.assertEqual(r2["session_id"], team_b.session)
        answered = self.synj("decisions", "answer", d1["id"], "Yes, use it")
        self.assertEqual(answered["answer_revision"], 1)
        self.synj("messages", "acknowledge", m1["id"])
        self.syn("briefs", "update", "--project", project, "--goal", "Goal from B")
        self.assertEqual(team_b.finish(), 0)
        files_before = file_digests(self.durable_roots([project]))

        # Roll back to A. The executable changes; no record is rewritten.
        self.installer("rollback")
        self.assertEqual(self.current(), a)
        self.assertEqual(self.version()["commit"], a)
        self.assertEqual(file_digests(self.durable_roots([project])), files_before)
        decisions = {d["id"]: d for d in self.synj("decisions", "list", "--all", "--json")}
        self.assertEqual((decisions[d1["id"]]["state"], decisions[d1["id"]]["answer_revision"]), ("answered", 1))
        self.assertEqual(decisions[d2["id"]]["state"], "open")
        messages = {m["id"]: m for m in self.synj("messages", "list", "--all")}
        self.assertEqual((messages[m1["id"]]["state"], messages[m2["id"]]["state"]), ("acknowledged", "pending"))
        requests = {r["id"]: r for r in self.synj("requests", "list", "--all", "--json")}
        self.assertEqual(requests[r1["id"]]["state"], "acknowledged")
        self.assertEqual(requests[r2["id"]]["session_id"], team_b.session)
        brief = self.synj("briefs", "show", "--project", project, "--json")
        self.assertEqual((brief["goal"], brief["revision"]), ("Goal from B", 2))
        briefing = self.synj("briefing", "--json")
        self.assertIn(d2["id"], [d["id"] for d in briefing["decisions"]])

        # A acts on records B wrote, and a restarted A session works normally.
        resolved = self.synj("decisions", "acknowledge", d1["id"], "-C", project, "--task", team_a.task,
                             "--answer-revision", "1")
        self.assertEqual(resolved["state"], "resolved")
        self.synj("decisions", "answer", d2["id"], "No")
        team_a2 = self.launch_team(project, "work after rollback")
        team_a2.ready()
        self.assertEqual(team_a2.launch["run"]["release"]["commit"], a)
        team_a2.run(shim + ["decisions", "acknowledge", d2["id"], "-C", project, "--task", team_b.task,
                            "--answer-revision", "1"])
        self.assertEqual(team_a2.finish(), 0)
        final = record_inventory(self.home, [project])
        self.assertEqual({k: v[0] for k, v in final["decisions"].items()}, {d1["id"]: "resolved", d2["id"]: "resolved"})
        self.assertEqual(set(final["messages"]), {m1["id"], m2["id"]})
        self.assertEqual(set(final["requests"]), {r1["id"], r2["id"]})
        self.assertEqual(len(final["sessions"]), 3)
        for commit in (a, b):
            self.assertEqual(list(self.release_dir(commit).rglob("__pycache__")), [])


# --------------------------------------------------------------------------
# 4. Limits monitor version/lock transition, with dashboard autostart
# --------------------------------------------------------------------------

@unittest.skipUnless(LINUX, "identifies the monitor lock owner through /proc")
class MonitorTransition(RehearsalCase):
    def monitor_release(self):
        return (read_json(self.limits / "monitor.json", {}) or {}).get("release", {})

    def wait_monitor(self, commit, exclude=()):
        """The running monitor: lock held by exactly one process of `commit`'s release."""
        launcher = str(self.release_dir(commit) / "bin" / "syndicate")

        def running():
            owners = self.monitor_lock_owners()
            procs = self.monitor_processes()
            if (self.monitor_lock_state() == "held" and len(owners) == 1 and set(owners) == set(procs)
                    and not set(owners) & set(exclude) and self.monitor_release().get("commit") == commit):
                return owners
            return None
        owners = self.wait_for(running, f"monitor of release {commit}", timeout=60)
        (pid, args), = owners.items()
        self.assertEqual(args[1:], ["-I", "-B", launcher, "_limits-monitor"])
        state = read_json(self.limits / "monitor.json")
        self.assertEqual(state["pid"], pid)
        self.assertEqual(state["release"]["root"], str(self.release_dir(commit)))
        self.assertEqual(state["release"]["mode"], "release")
        return pid

    def test_monitor_and_dashboard_switch_releases_through_stop_and_lock_release(self):
        a, b = FIXTURE["a"], FIXTURE["b"]
        self.install("v1.0.0")

        # Dashboard-triggered autostart: nothing else starts a monitor here, so
        # the one that appears comes from the old-release dashboard's launch.
        self.assertEqual((self.monitor_processes(), self.monitor_lock_state()), ({}, "free"))
        web_a, port_a = self.start_web()
        monitor_a = self.wait_monitor(a)
        status, body = self.get(port_a, "/api/limits")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["monitor"]["release"]["commit"], a)

        # Hazard: stopping the monitor while that dashboard still serves lets
        # its next data request auto-start A again.
        self.stop_monitor()
        self.assertEqual(self.get(port_a, "/api/limits")[0], 200)
        restarted = self.wait_monitor(a, exclude={monitor_a})
        self.assertNotEqual(restarted, monitor_a)

        # Runbook: quiesce the old dashboard (or disable its autostart) first,
        # then stop the monitor and wait for verified lock release.
        web_a.terminate()
        web_a.wait(timeout=30)
        self.stop_monitor()
        self.assertEqual(self.monitor_processes(), {})

        # An old-release dashboard with autostart disabled (--no-monitor) may
        # keep serving through the transition: neither its launch nor its data
        # requests start a monitor while the singleton is stopped.
        web_quiet, port_quiet = self.start_web(no_monitor=True)
        for _ in range(3):
            status, body = self.get(port_quiet, "/api/limits")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["monitor"].get("pid"), restarted)  # last record of the stopped one
        time.sleep(1.5)
        self.assertEqual((self.monitor_processes(), self.monitor_lock_state()), ({}, "free"))

        # Activate B and restart the monitor from it.
        self.install("v1.1.0")
        self.assertEqual(self.get(port_quiet, "/api/limits")[0], 200)
        self.assertEqual((self.monitor_processes(), self.monitor_lock_state()), ({}, "free"))
        self.assertIn("codex:", self.syn("limits").stdout)
        monitor_b = self.wait_monitor(b, exclude={monitor_a, restarted})
        observation = read_json(self.limits / "codex.json")
        self.assertEqual(observation["source"], "codex-app-server")
        # The paused A dashboard now reports B's monitor and still starts nothing.
        status, body = self.get(port_quiet, "/api/limits")
        self.assertEqual(json.loads(body)["monitor"]["release"]["commit"], b)
        self.assertEqual(set(self.monitor_processes()), {monitor_b})
        web_quiet.terminate()
        web_quiet.wait(timeout=30)

        # The intended dashboard comes from B and reuses B's monitor.
        web_b, port_b = self.start_web()
        status, body = self.get(port_b, "/api/limits")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["monitor"]["release"]["commit"], b)
        self.assertEqual(set(self.monitor_processes()), {monitor_b})
        status, page = self.get(port_b, "/")
        self.assertEqual((status, page), (200, (self.release_dir(b) / "web" / "dashboard.html").read_bytes()))
        web_b.terminate()
        web_b.wait(timeout=30)
        self.stop_monitor()


# --------------------------------------------------------------------------
# 5. Codex hook migration and partial-failure recovery
# --------------------------------------------------------------------------

class HookRecovery(RehearsalCase):
    def write_hooks(self, gate_command):
        data = {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "echo start"}]}],
                "Stop": [
                    {"hooks": [{"type": "command", "command": "/usr/local/bin/other-stop", "timeout": 5}]},
                    {"matcher": "*", "hooks": [{"type": "command", "command": "echo sibling"},
                                               {"type": "command", "command": gate_command, "timeout": 30}]},
                ],
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pre"}]}],
            },
            "unrelated": {"keep": True, "nested": [1, 2, 3]},
        }
        self.hooks.write_text(json.dumps(data, indent=2) + "\n")
        self.hooks.chmod(0o600)
        return data

    def setUp(self):
        super().setUp()
        self.codex = self.tmp / "codex"
        self.hooks = self.codex / "hooks.json"

    def migrate(self, *extra):
        return json.loads(self.installer("migrate-codex-hook", "--codex-home", str(self.codex), "--json",
                                         *extra).stdout)

    def gate_command(self):
        stop = json.loads(self.hooks.read_text())["hooks"]["Stop"]
        commands = [h["command"] for e in stop for h in e["hooks"] if h["command"].endswith("require-review.sh")
                    or h["command"].endswith("require-review.sh'")]
        self.assertEqual(len(commands), 1, commands)
        return commands[0]

    def assert_unrelated_preserved(self, original: dict, gate_command: str, extra_stop=None):
        """Everything except the one recorded gate command equals the original, byte for byte."""
        expected = json.loads(json.dumps(original))
        expected["hooks"]["Stop"][1]["hooks"][1]["command"] = gate_command
        if extra_stop:
            expected["hooks"]["Stop"].append(extra_stop)
        self.assertEqual(self.hooks.read_text(), json.dumps(expected, indent=2) + "\n")
        self.assertEqual(stat.S_IMODE(self.hooks.stat().st_mode), 0o600)

    def assert_gate_runs(self, command):
        path = Path(shlex.split(command)[0])
        self.assertTrue(os.access(path, os.X_OK), path)
        if shutil.which("jq", path=self.env["PATH"]) and shutil.which("bash", path=self.env["PATH"]):
            gate = subprocess.run([str(path)], input=json.dumps({"stop_hook_active": True}), env=self.env,
                                  capture_output=True, text=True, cwd=self.tmp / "elsewhere", timeout=60)
            self.assertEqual((gate.returncode, gate.stdout), (0, ""), gate.stderr)

    def restore_recorded_entry(self, recorded: str, migrated: str):
        """Put back only the recorded hook command; every other entry stays as it is now."""
        data = json.loads(self.hooks.read_text())
        hits = 0
        for entry in data["hooks"]["Stop"]:
            for hook in entry["hooks"]:
                if hook.get("command") == migrated:
                    hook["command"] = recorded
                    hits += 1
        self.assertEqual(hits, 1)
        mode = stat.S_IMODE(self.hooks.stat().st_mode)
        tmp = self.hooks.with_name(".hooks.json.restore")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.chmod(mode)
        os.replace(tmp, self.hooks)

    def dashboard_fails(self):
        """Simulated partial failure: the target release's dashboard cannot start (port taken)."""
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        try:
            result = self.syn("web", "--port", blocker.getsockname()[1], ok=False, timeout=60)
        finally:
            blocker.close()
        self.assertIn("Address already in use", result.stderr)
        self.assertFalse(self.monitor_processes(), "a dashboard that failed to bind started no monitor")

    def test_failed_upgrade_restores_prior_release_and_only_the_recorded_hook(self):
        a, b = FIXTURE["a"], FIXTURE["b"]
        checkout = self.legacy_checkout()
        legacy_command = shlex.quote(str(checkout / "hooks" / "require-review.sh"))
        original = self.write_hooks(legacy_command)
        operator_backup = self.tmp / "backups" / "hooks.json"
        shutil.copy2(self.hooks, operator_backup)

        self.install("v1.0.0")
        result = self.migrate()
        self.assertIn("checkout", result["changes"][0]["provenance"])
        a_command = shlex.quote(str(self.release_dir(a) / "hooks" / "require-review.sh"))
        self.assertEqual(self.gate_command(), a_command)
        self.assert_unrelated_preserved(original, a_command)
        self.assert_gate_runs(a_command)

        # Record before switching (runbook step 1), then switch to B.
        recorded = self.gate_command()
        self.install("v1.1.0")
        self.migrate()
        b_command = shlex.quote(str(self.release_dir(b) / "hooks" / "require-review.sh"))
        self.assertEqual(self.gate_command(), b_command)
        # Meanwhile another tool adds an unrelated Stop hook.
        data = json.loads(self.hooks.read_text())
        late = {"hooks": [{"type": "command", "command": "echo added-during-transition"}]}
        data["hooks"]["Stop"].append(late)
        self.hooks.write_text(json.dumps(data, indent=2) + "\n")

        self.dashboard_fails()
        # Recovery: select the prior artifact, restore only the recorded hook entry.
        self.installer("rollback")
        self.assertEqual(self.current(), a)
        self.migrate("--target", a)
        self.assertEqual(self.gate_command(), recorded)
        self.assert_unrelated_preserved(original, recorded, extra_stop=late)
        self.assert_gate_runs(recorded)
        # Both releases stay retained, and prune keeps the hook's release.
        self.assertEqual({r["commit"] for r in json.loads(self.installer("list", "--json").stdout)}, {a, b})
        kept = json.loads(self.installer("prune", "--dry-run", "--codex-home", str(self.codex), "--json").stdout)
        self.assertEqual(kept["removed"], [])
        self.assertEqual(self.version()["commit"], a)
        self.assertEqual(operator_backup.read_bytes(), json.dumps(original, indent=2).encode() + b"\n")
        # Restart and verify only the intended prior services before resuming.
        self.assert_prior_dashboard(self.release_dir(a), a)

    def assert_prior_dashboard(self, root, commit=None):
        """The restarted dashboard serves `root`'s page and its data endpoint; its monitor follows."""
        web, port = self.start_web()
        status, page = self.get(port, "/")
        self.assertEqual((status, page), (200, (root / "web" / "dashboard.html").read_bytes()))
        status, body = self.get(port, "/api/limits")
        self.assertEqual(status, 200)
        self.assertIn("codex:", json.loads(body)["summary"])
        if commit:
            self.wait_for(lambda: (read_json(self.limits / "monitor.json", {}) or {}).get("release", {})
                          .get("commit") == commit, f"monitor of release {commit}", timeout=60)
        else:
            self.wait_for(lambda: self.monitor_lock_state() == "held", "monitor autostart", timeout=60)
        web.terminate()
        web.wait(timeout=30)
        self.stop_monitor()

    def test_failed_first_cutover_restores_entrypoint_and_only_the_recorded_hook(self):
        checkout = self.legacy_checkout()
        self.shim.parent.mkdir(parents=True)
        self.shim.symlink_to(checkout / "bin" / "syndicate")
        legacy_command = shlex.quote(str(checkout / "hooks" / "require-review.sh"))
        original = self.write_hooks(legacy_command)
        recorded = legacy_command

        self.install("v1.0.0")
        migrated = self.migrate()
        backup = Path(migrated["backup"])
        self.assertEqual(backup.read_bytes(), json.dumps(original, indent=2).encode() + b"\n")
        new_command = self.gate_command()
        self.assertIn(FIXTURE["a"], new_command)
        self.dashboard_fails()

        # No prior release: restore the recorded entry point and the recorded hook entry.
        self.installer("restore-entrypoint")
        self.assertEqual(os.readlink(self.shim), str(checkout / "bin" / "syndicate"))
        self.restore_recorded_entry(recorded, new_command)
        self.assert_unrelated_preserved(original, recorded)
        self.assertEqual(self.hooks.read_bytes(), backup.read_bytes())
        self.assert_gate_runs(recorded)
        self.assertIn("usage: syndicate", self.syn("--help").stdout)
        self.assertNotIn(str(self.root), self.syn("chief-of-staff", "--dry-run", "-x", "codex").stdout)
        # The restored checkout entry point serves its own dashboard again.
        self.assert_prior_dashboard(checkout)


# --------------------------------------------------------------------------
# 6. First cutover with no prior release: checkout-backed fallback
# --------------------------------------------------------------------------

class FirstCutoverFallback(RehearsalCase):
    def test_fallback_snapshot_survives_checkout_edits_and_consumes_preserved_state(self):
        a, b = FIXTURE["a"], FIXTURE["b"]
        editable = self.legacy_checkout("editable checkout", commit=a)
        self.shim.parent.mkdir(parents=True)
        self.shim.symlink_to(editable / "bin" / "syndicate")
        self.user_config.parent.mkdir(parents=True)
        self.user_config.write_text("tmux = off\nhandoff = off\ndefault = codex\n")
        project = self.make_project("delta")

        # Record a fallback pinned to a verified commit, in a preserved snapshot
        # outside the editable checkout (its own clone, detached at the commit).
        verified = run_git(editable, "rev-parse", "HEAD", env=self.env)
        self.assertEqual(verified, a)
        snapshot = self.home / ".local" / "share" / "syndicate-fallback" / verified
        snapshot.parent.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", "--no-checkout", str(editable), str(snapshot)], check=True,
                       env=self.env, capture_output=True)
        run_git(snapshot, "checkout", "-q", "--detach", verified, env=self.env)
        for directory, dirs, files in os.walk(snapshot):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in files:
                path = Path(directory) / name
                path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
        fallback_prefix = [PYTHON, "-B", str(snapshot / "bin" / "syndicate")]
        record = dict(commit=verified, prefix=fallback_prefix,
                      assets={name: str(snapshot / name) for name in ("roles", "skills", "web",
                                                                       "hooks/require-review.sh")},
                      hook_target=str(snapshot / "hooks" / "require-review.sh"),
                      digests={p: hashlib.sha256((snapshot / p).read_bytes()).hexdigest()
                               for p in ("bin/syndicate", "roles/backend-engineer.md", "hooks/require-review.sh")})
        record_path = snapshot.parent / "fallback.json"
        record_path.write_text(json.dumps(record, indent=2) + "\n")

        def verify_fallback(prefix):
            info = self.version(prefix=prefix)
            self.assertEqual((info["mode"], info["commit"], info["dirty"]), ("checkout", verified, False))
            self.assertEqual(info["root"], str(snapshot))
            for path, digest in record["digests"].items():
                self.assertEqual(hashlib.sha256((snapshot / path).read_bytes()).hexdigest(), digest, path)
            dry = self.syn("chief-of-staff", "--dry-run", "-x", "codex", prefix=prefix).stdout
            self.assertIn(str(snapshot), dry)
            self.assertNotIn(str(editable), dry)
            self.assertIn("usage: syndicate", self.syn("--help", prefix=prefix).stdout)

        verify_fallback(fallback_prefix)  # preflight before touching the entry point

        # Cutover to B (no prior installed release).
        self.install("v1.1.0")
        self.assertEqual(self.version()["commit"], b)
        team_b = self.launch_team(project, "cutover task")
        team_b.ready()
        shim = [str(self.shim)]
        decision = json.loads(team_b.run(shim + ["decisions", "request", "Keep the cutover?", "--key", "cutover",
                                                 "-C", project])["out"])
        request = self.synj("requests", "send", "Record cutover notes", "--key", "notes", "-C", project,
                            "--task", team_b.task)
        self.assertEqual(team_b.finish(), 0)
        handoff = self.write_handoff(project, team_b.task)

        # The transition fails; restore the recorded fallback (not the editable checkout).
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        failed = self.syn("web", "--port", blocker.getsockname()[1], ok=False, timeout=60)
        blocker.close()
        self.assertIn("Address already in use", failed.stderr)
        self.assertTrue(rel.is_managed_shim(self.shim))
        restored = json.loads(record_path.read_text())
        temp = self.shim.with_name(".syndicate.fallback")
        temp.symlink_to(Path(restored["prefix"][2]))
        os.replace(temp, self.shim)

        # Now the editable checkout moves on: a new commit and a dirty launcher edit.
        role = editable / "roles" / "backend-engineer.md"
        role.write_text(role.read_text() + "\nEDITED IN CHECKOUT\n")
        run_git(editable, "commit", "-qam", "editable change", env=self.env)
        launcher = editable / "bin" / "syndicate"
        lines = launcher.read_text().split("\n", 1)
        launcher.write_text(lines[0] + "\nraise SystemExit('editable checkout executed')\n" + lines[1])
        self.assertNotEqual(run_git(editable, "rev-parse", "HEAD", env=self.env), verified)

        # The fallback still resolves to the verified snapshot.
        entry = [str(self.shim)]
        verify_fallback(entry)
        verify_fallback(restored["prefix"])
        self.assertEqual(os.path.realpath(self.shim), str(snapshot / "bin" / "syndicate"))

        # The Chief restarts from the fallback and consumes the preserved
        # portfolio state through the pinned prefix in its launch context.
        chief = self.launch_chief("-x", "codex", prefix=entry)
        chief_launch = chief.ready()
        workspace = self.home / ".syndicate" / "portfolio" / "workspace"
        self.assertEqual(chief_launch["project"], str(workspace))
        pinned = re.search(r"Syndicate executable prefix: (.+)", chief_launch["instructions"])
        self.assertIsNotNone(pinned, chief_launch["instructions"][-2000:])
        chief_prefix = shlex.split(pinned.group(1))
        self.assertEqual(chief_prefix[1:], ["-I", "-B", str(snapshot / "bin" / "syndicate")])
        self.assertNotIn(str(editable), chief_launch["instructions"])
        self.assertEqual(json.loads(chief.run(chief_prefix + ["--version", "--json"])["out"])["root"], str(snapshot))
        briefing = json.loads(chief.run(chief_prefix + ["briefing", "--json"])["out"])
        self.assertIn(decision["id"], [d["id"] for d in briefing["decisions"]])
        seen = {r["id"]: r for r in json.loads(chief.run(chief_prefix + ["requests", "list", "--all", "--json"])["out"])}
        self.assertEqual(seen[request["id"]]["session_id"], team_b.session)
        self.assertEqual(chief.finish(), 0, chief.text())
        self.assertEqual(read_json(workspace / ".syndicate" / "status.json")["state"], "done")
        self.stop_monitor(prefix=entry)  # the Chief started the shared monitor from the snapshot

        # It consumes the state written during the failed cutover.
        self.assertEqual([d["id"] for d in self.synj("decisions", "list", "--json", prefix=entry)], [decision["id"]])
        self.synj("decisions", "answer", decision["id"], "Not yet", prefix=entry)
        team = self.launch_team(project, "-x", "codex", "--handoff", handoff, prefix=entry)
        launch = team.ready()
        self.assertEqual(team.task, team_b.task)
        self.assertEqual((launch["run"]["release"]["commit"], launch["run"]["release"]["root"]),
                         (verified, str(snapshot)))
        configs = "\n".join(launch["configs"].values())
        self.assertIn(MARKER.format("A"), configs)
        self.assertNotIn("EDITED IN CHECKOUT", configs)
        self.assertNotIn(str(editable), json.dumps(launch["configs"]) + launch["instructions"])
        rebound = self.synj("requests", "retry", request["id"], prefix=entry)
        self.assertEqual(rebound["session_id"], team.session)
        team.run(entry + ["requests", "acknowledge", request["id"], "-C", project, "--task", team.task,
                          "--session", team.session, "--note", "notes recorded after fallback"])
        team.run(entry + ["decisions", "acknowledge", decision["id"], "-C", project, "--task", team.task,
                          "--answer-revision", "1"])
        self.assertEqual(team.finish(), 0, team.text())
        final = record_inventory(self.home, [project])
        self.assertEqual(final["decisions"][decision["id"]][0], "resolved")
        self.assertEqual(final["requests"][request["id"]][:2], ("acknowledged", team.session))
        # The installed release is retained for a later retry.
        self.assertEqual(self.current(), b)
        self.assertEqual(rel.verify_release(self.release_dir(b))["commit"], b)
        self.assertFalse(list(snapshot.rglob("__pycache__")))
        status = subprocess.run(["git", "-C", str(snapshot), "status", "--porcelain", "--ignored"],
                                capture_output=True, text=True, env=self.env).stdout
        self.assertEqual(status, "")


# --------------------------------------------------------------------------
# 7. Legacy context inventory
# --------------------------------------------------------------------------

@unittest.skipUnless(shutil.which("tmux", path="/usr/bin:/bin"), "tmux is not installed: pane start commands cannot be inventoried")
class LegacyContextInventory(RehearsalCase):
    def test_inventory_finds_every_seeded_reference_to_the_old_checkout(self):
        seed = self.seed_legacy_home()
        checkout, p1 = seed["checkout"], seed["p1"]
        script = checkout / "bin" / "syndicate"
        # Shell alias and an explicit interpreter/script caller.
        (self.home / ".bashrc").write_text(f"alias syn={shlex.quote(str(script))}\n")
        launcher = self.home / "bin" / "syndicate-chief"
        launcher.parent.mkdir()
        launcher.write_text(f"#!/bin/sh\nexec python3 {shlex.quote(str(script))} chief-of-staff \"$@\"\n")
        # Registered review hook (legacy setup-codex-gate writes ~/.codex/hooks.json).
        self.syn("setup-codex-gate", prefix=seed["legacy"])
        # Approvals granted for the old command path.
        rules = self.tmp / "codex" / "rules" / "default.rules"
        rules.parent.mkdir(parents=True)
        rules.write_text(f'prefix_rule(pattern=["{script}", "tasks"], decision="allow")\n')
        claude_settings = p1 / ".claude" / "settings.local.json"
        claude_settings.parent.mkdir()
        claude_settings.write_text(json.dumps({"permissions": {"allow": [f"Bash({script} team run-role:*)"]}}))
        # A tmux window started by the old launcher (disposable server and socket).
        sock = self.tmp / "tmux" / "rehearsal.sock"
        subprocess.run(["tmux", "-S", str(sock), "-f", os.devnull, "new-session", "-d", "-s", "the-syndicate",
                        "-n", "alpha", f"exec sleep 300 # {shlex.quote(str(script))} _in-tmux team task"],
                       env=self.env, check=True, capture_output=True, timeout=30)
        # Unrelated paths are not reported.
        (self.home / "notes.txt").write_text(f"{checkout}-other is a different checkout\n")

        roots = [self.home, self.tmp / "config", self.tmp / "codex", self.tmp / "claude", self.tmp / "data home",
                 seed["p1"], seed["p2"]]
        found = legacy_context_inventory(checkout, roots, [sock], env=self.env)
        self.assertEqual(found["entry point"], [str(self.shim)])
        self.assertIn(str(self.home / ".bashrc"), found["shell profile"])
        self.assertIn(str(launcher), found["other"])
        self.assertEqual(found["codex hook"], [str(self.home / ".codex" / "hooks.json")])
        self.assertEqual(sorted(found["approvals"]), sorted([str(rules), str(claude_settings)]))
        self.assertTrue(found["generated settings"], "the legacy launch generated relay settings")
        self.assertTrue(all(str(p1 / ".syndicate" / ".generated") in p for p in found["generated settings"]))
        self.assertEqual(found["tmux"], [f"{sock} the-syndicate:alpha"])
        self.assertNotIn(str(self.home / "notes.txt"), sum(found.values(), []))
        self.assertFalse(any(p.startswith(str(checkout) + "/") for p in sum(found.values(), [])))

        # After the cutover steps the entry point and hook no longer reference the checkout;
        # the remaining entries are the explicit follow-up list.
        self.install("v1.0.0")
        hooks = self.home / ".codex" / "hooks.json"
        self.installer("migrate-codex-hook", "--codex-home", str(hooks.parent))
        after = legacy_context_inventory(checkout, roots, [sock], env=self.env)
        self.assertNotIn("entry point", after)
        self.assertNotIn("codex hook", after)
        self.assertEqual(after["approvals"], found["approvals"])
        self.assertEqual(after["tmux"], found["tmux"])
        self.assertEqual(after["installer records"], [str(self.paths.entrypoints)])
        self.assertEqual(len(after["hook backup"]), 1)
        self.assertEqual(after["generated settings"], found["generated settings"])


# --------------------------------------------------------------------------
# 8. Self-hosted: stable source, isolated linked worktree, ignored-file guard
# --------------------------------------------------------------------------

# The installed release's existing guarded cleanup (syndicate_git.cleanup_pr,
# the core of `tasks cleanup-pr`), with only the GitHub boundary replaced:
# `gh pr view` returns the given PR record and the local bare origin stands in
# for the GitHub repository.
CLEANUP_PR = r'''
import json, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import syndicate_git as g
repo, branch, worktree, pr = Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), json.loads(sys.argv[5])
real = g._run
def run(path, command, expected=(0,), label=None):
    if command[0] == "gh":
        return subprocess.CompletedProcess(command, 0, json.dumps(pr), "")
    return real(path, command, expected, label)
g._run = run
g._github_repository = lambda url: "github.com/example/syndicate"
try:
    sys.exit(0 if g.cleanup_pr(repo, 7, branch, worktree, print) else 2)
except g.CleanupError as error:
    print(f"CleanupError: {error}")
    sys.exit(1)
'''


class SelfHosted(RehearsalCase):
    branch = "work/self-hosted"

    def cleanup_pr(self, release, source, linked, pr):
        # cleanup_pr refuses GIT_CONFIG_* overrides; HOME/XDG stay disposable.
        env = {k: v for k, v in self.env.items() if not k.startswith("GIT_CONFIG")}
        return subprocess.run([PYTHON, "-B", "-c", CLEANUP_PR, str(release / "bin"), str(source), self.branch,
                               str(linked), json.dumps(pr)], env=env, cwd=self.tmp / "elsewhere",
                              capture_output=True, text=True, timeout=120)

    def assert_blocked(self, release, source, linked, pr, origin, tip):
        result = self.cleanup_pr(release, source, linked, pr)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Task worktree is dirty (tracked, untracked or ignored changes)", result.stdout)
        # Nothing was removed: worktree, local and remote task branch, main.
        self.assertTrue(linked.is_dir())
        self.assertEqual(run_git(source, "rev-parse", f"refs/heads/{self.branch}", env=self.env), tip)
        self.assertEqual(run_git(origin, "rev-parse", f"refs/heads/{self.branch}", env=self.env), tip)

    def test_team_on_stable_source_leaves_linked_worktree_free_of_ignored_artifacts(self):
        a = FIXTURE["a"]
        self.user_config.parent.mkdir(parents=True)
        self.user_config.write_text("tmux = off\nhandoff = off\ndefault = codex\n")
        self.install("v1.0.0")
        release = self.release_dir(a)
        # The stable source is a disposable clone of a per-test copy of the fixture repository.
        origin = self.tmp / "projects" / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(FIXTURE["upstream"]), str(origin)], check=True,
                       env=self.env, capture_output=True, timeout=60)
        source = self.tmp / "projects" / "syndicate source"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(source)], check=True, env=self.env,
                       capture_output=True, timeout=60)
        start = run_git(source, "rev-parse", "HEAD", env=self.env)
        linked = self.tmp / "projects" / "linked edit checkout"
        shim = [str(self.shim)]
        team = self.launch_team(source, "self-hosted change")
        launch = team.ready()
        self.assertEqual(launch["project"], str(source))
        self.assertEqual(launch["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        # The orchestrator edits only an isolated linked worktree of the stable source.
        team.run(["git", "-C", source, "worktree", "add", "-q", "-b", self.branch, linked])
        doc = linked / "docs" / "configuration.md"
        team.run([PYTHON, "-c", f"import pathlib; p = pathlib.Path({str(doc)!r}); "
                                "p.write_text(p.read_text() + '\\nSelf-hosted edit.\\n')"], cwd=linked)
        # Exercise the edited checkout's Python the way a developer would.
        team.run(["python3", "bin/syndicate", "--help"], cwd=linked)
        team.run(["python3", "-c", "import sys; sys.path.insert(0, 'bin'); import syndicate_git, syndicate_release"],
                 cwd=linked)
        team.run(["python3", "-m", "unittest", "discover", "-q", "-s", "tests", "-p", "test_completion.py"], cwd=linked)
        team.run(shim + ["--version"], cwd=linked)
        team.run(["git", "-C", linked, "commit", "-qam", "self-hosted edit"])
        team.run(["git", "-C", linked, "push", "-q", "origin", self.branch])
        tip = run_git(linked, "rev-parse", "HEAD", env=self.env)
        self.assertEqual(team.finish(), 0)

        # Project-local runtime data stays at the stable source, never in the linked worktree.
        self.assertTrue((source / ".syndicate" / "status.json").is_file())
        self.assertFalse((linked / ".syndicate").exists())
        status = subprocess.run(["git", "-C", str(linked), "status", "--porcelain", "--ignored",
                                 "--untracked-files=all"], capture_output=True, text=True, env=self.env)
        self.assertEqual(status.stdout, "")
        self.assertEqual(list(linked.rglob("__pycache__")), [])
        self.assertEqual(list(release.rglob("__pycache__")), [])
        source_ignored = subprocess.run(["git", "-C", str(source), "status", "--porcelain", "--ignored"],
                                        capture_output=True, text=True, env=self.env).stdout
        self.assertEqual(source_ignored, "!! .syndicate/\n")

        # The PR merges upstream (an integrator clone stands in for GitHub's merge).
        integrator = self.tmp / "projects" / "integrator"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(integrator)], check=True, env=self.env,
                       capture_output=True, timeout=60)
        run_git(integrator, "merge", "-q", "--no-ff", "--no-edit", f"origin/{self.branch}", env=self.env)
        merge = run_git(integrator, "rev-parse", "HEAD", env=self.env)
        run_git(integrator, "push", "-q", "origin", "main", env=self.env)
        pr = dict(state="MERGED", url="https://github.com/example/syndicate/pull/7", mergedAt="2026-11-24T10:00:00Z",
                  mergeCommit={"oid": merge}, headRefName=self.branch, headRefOid=tip, baseRefName="main",
                  isCrossRepository=False)

        # The guard is real: known bytecode blocks it; an unrelated ignored file
        # blocks it too and is left intact. Blocked attempts change nothing.
        cache = linked / "bin" / "__pycache__"
        cache.mkdir()
        (cache / "syndicate_git.cpython-312.pyc").write_bytes(b"\x00bytecode")
        self.assert_blocked(release, source, linked, pr, origin, tip)
        (cache / "syndicate_git.cpython-312.pyc").unlink()  # narrowly authorized removal
        cache.rmdir()
        unrelated = linked / ".syndicate"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("user data\n")
        self.assert_blocked(release, source, linked, pr, origin, tip)
        self.assertEqual((unrelated / "keep.txt").read_text(), "user data\n")
        shutil.move(str(unrelated), str(self.tmp / "backups" / "linked.syndicate"))  # its owner moves it away

        # With only what the self-hosted run left, the existing safe cleanup proceeds.
        result = self.cleanup_pr(release, source, linked, pr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Removed task worktree {linked}.", result.stdout)
        self.assertFalse(linked.exists())
        self.assertEqual(run_git(source, "branch", "--list", self.branch, env=self.env), "")
        self.assertEqual(run_git(origin, "branch", "--list", self.branch, env=self.env), "")
        self.assertEqual(run_git(source, "rev-parse", "HEAD", env=self.env), merge)
        self.assertNotEqual(merge, start)
        # The stable source keeps its runtime metadata through cleanup.
        self.assertTrue((source / ".syndicate" / "status.json").is_file())
        self.assertEqual((self.tmp / "backups" / "linked.syndicate" / "keep.txt").read_text(), "user data\n")


if __name__ == "__main__":
    unittest.main()
