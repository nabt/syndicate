"""Quota observations, durable handoffs, and POSIX CLI supervision.

Only documented provider surfaces are used. No inference is required for a
checkpoint or a Codex quota refresh. Claude publishes via its status line.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import pty
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tty
import uuid
from contextlib import contextmanager
from pathlib import Path
from stat import S_ISREG

STATE = Path.home() / ".syndicate" / "limits"
STALE_SECONDS = 180
POLL_SECONDS = 30
LEASE_DIRECTORY = "specialists"
LEASE_MAX_BYTES = 4096
LEASE_SCAN_LIMIT = 64  # lease-named entries examined per scan
LEASE_SCAN_ENTRIES = 4 * LEASE_SCAN_LIMIT  # directory entries of any name read per scan
LEASE_NAME = re.compile(r"[0-9a-f]{32}\.json")
# Role names as accepted by the configuration (ROLE_NAME in bin/syndicate), bounded.
ROLE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# supervise() run ids: %Y%m%dT%H%M%S-<8 hex>.
RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{8}")
# Passed to the supervised CLI. Codex's default shell_environment_policy drops
# names matching *KEY*, *SECRET* or *TOKEN*; this name avoids all three.
RUN_ENV = "SYNDICATE_RUN_ID"
MAX_TIMESTAMP = 10**10  # epoch seconds (year 2286); larger values are treated as malformed
PROVIDERS = ("claude", "codex")
clock = time.time  # supervisor loop clock; tests patch this module-local name
_O = lambda name: getattr(os, name, 0)  # noqa: E731 - flags absent on some platforms


def read_json(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, RecursionError):  # RecursionError: pathologically nested JSON
        return {}


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, allow_nan=False)
            f.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def window(name, used, reset):
    if not _real(used, 0, 100):
        return None
    if not _real(reset, 1, MAX_TIMESTAMP):
        reset = None
    return {"name": name, "remaining_percent": 100 - used, "resets_at": reset}


def normalize(provider, data, now=None):
    windows = []
    data = data if isinstance(data, dict) else {}
    if provider == "claude":
        rates = data.get("rate_limits") or {}
        rates = rates if isinstance(rates, dict) else {}
        for name in ("five_hour", "seven_day", "spend_limit"):
            value = rates.get(name)
            if isinstance(value, dict):
                windows.append(window(name, value.get("used_percentage"), value.get("resets_at")))
    else:
        # Preserve all returned buckets: a model-specific exhausted limit must
        # not disappear merely because the default bucket is still available.
        buckets = data.get("rateLimitsByLimitId") or {"codex": data.get("rateLimits")}
        buckets = buckets if isinstance(buckets, dict) else {}
        for bucket, rates in buckets.items():
            if not isinstance(rates, dict):
                continue
            for name in ("primary", "secondary"):
                value = rates.get(name)
                if isinstance(value, dict):
                    windows.append(window(f"{bucket}/{name}", value.get("usedPercent"), value.get("resetsAt")))
    return {"provider": provider, "observed_at": time.time() if now is None else now,
            "windows": [w for w in windows if w], "source": "claude-statusline" if provider == "claude" else "codex-app-server"}


def save_observation(provider, data):
    observation = normalize(provider, data)
    # Missing fields do not turn a last-known limit into 100% availability.
    if observation["windows"]:
        atomic_json(STATE / f"{provider}.json", observation)
    return observation


def codex_limits(timeout=15):
    """Documented JSON-RPC handshake; no model turn, credentials, or shell."""
    proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    buffer = b""
    def send(message):
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        proc.stdin.flush()
    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "syndicate_limits", "version": "1"}}})
        while time.monotonic() < deadline:
            for key, _ in sel.select(max(0, deadline - time.monotonic())):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    raise RuntimeError("Codex quota reader exited")
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    message = json.loads(line)
                    if message.get("id") == 1:
                        if "error" in message:
                            raise RuntimeError("Codex initialize rejected")
                        send({"method": "initialized"})
                        send({"id": 2, "method": "account/rateLimits/read"})
                    elif message.get("id") == 2:
                        if "error" in message:
                            raise RuntimeError("Codex account limits unavailable for this login")
                        return message.get("result", {})
        raise RuntimeError("Codex account limits timed out")
    finally:
        sel.close()
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdin.close()
        proc.stdout.close()


def _real(value, low, high):
    """value is a finite int/float (never bool) within [low, high].

    The range is checked first: comparing even a huge JSON integer never
    overflows (math.isfinite would raise OverflowError for it), and NaN fails
    every comparison.
    """
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and low <= value <= high and math.isfinite(value))


def usage_windows(observation):
    """Recorded windows whose remaining_percent is a finite number in [0, 100].

    Malformed telemetry (wrong types, NaN, out-of-range values, huge integers,
    non-string names) is dropped, so a damaged state file reads as unknown
    instead of crashing a caller.
    """
    windows = observation.get("windows") if isinstance(observation, dict) else None
    if not isinstance(windows, list):
        return []
    return [w for w in windows if isinstance(w, dict) and isinstance(w.get("name"), str)
            and _real(w.get("remaining_percent"), 0, 100)]


def _timestamp(value):
    """A plausible epoch time safe to pass to time.localtime, else None."""
    return value if _real(value, 1, MAX_TIMESTAMP) else None


def observed_time(observation):
    return _timestamp(observation.get("observed_at")) if isinstance(observation, dict) else None


def format_time(value, pattern="%Y-%m-%d %H:%M:%S %z", missing="unknown"):
    value = _timestamp(value)
    return time.strftime(pattern, time.localtime(value)) if value is not None else missing


def availability(observation, now=None):
    now = time.time() if now is None else now
    windows = usage_windows(observation)
    if not windows:
        return "unknown", None
    observed = observed_time(observation)
    if observed is None or now - observed > STALE_SECONDS:
        return "stale", None
    if any(_timestamp(w.get("resets_at")) is not None and now >= w["resets_at"] for w in windows):
        return "awaiting reset confirmation", None
    return "current", min(w["remaining_percent"] for w in windows)


def latest_reading(observation):
    """Most constrained window of the last real reading, whatever its age.

    None means no usable reading was ever recorded. A passed reset time does
    not restore capacity, and no quota is inferred from tokens or elapsed time.
    """
    return min((w["remaining_percent"] for w in usage_windows(observation)), default=None)


def supervision_usage(observation, chief=False, now=None):
    """(state, remaining) for enforcing the reserve with the latest real reading.

    Every supervised provider is enforced with its most recent recorded
    percentages, including stale readings and readings awaiting reset
    confirmation; unknown or stale telemetry never stops a session by itself.
    Chief sessions report the fixed state "last observed". Project sessions
    report the freshness state from availability() so the supervisor can warn
    that the reading may be outdated. Neither establishes current capacity.
    """
    remaining = latest_reading(observation)
    if chief:
        return "last observed", remaining
    return availability(observation, now)[0], remaining


def forget_hint(provider):
    """Recovery guidance for a recorded reading the operator knows is outdated."""
    return (f"if you know this {provider} reading is outdated, 'syndicate limits --forget {provider}' removes it; "
            f"the reserve then cannot be enforced for {provider} until a new reading is recorded")


def usage_warning(provider, state, observation):
    """One-line operator warning for telemetry that is not current, else None."""
    if state == "current":
        return None
    remaining = latest_reading(observation)
    if remaining is None:
        return (f"no {provider} usage reading yet; the reserve cannot be enforced for "
                f"{provider} until one is recorded")
    when = format_time(observed_time(observation), "%H:%M", "unknown time")
    return (f"{provider} usage telemetry is {state} (last observed {when}, {remaining:g}% remaining); "
            "enforcing the reserve with the last real reading")


# Specialist leases. A delegated specialist (team run-role) creates a randomly
# named lease file and holds an exclusive flock on it for its whole lifetime.
# The supervisor treats a lease as active only while that lock is held, which
# works across PID namespaces (a sandboxed specialist's PIDs are meaningless
# to the supervisor), and only for its own run (SYNDICATE_RUN_ID).

def lease_directory(project):
    return Path(project) / ".syndicate" / LEASE_DIRECTORY


def _open_lease_directory(project, create):
    """fd of .syndicate/specialists, opened without following symbolic links.

    The directory must be owned by this user and not writable by group or
    others. With create, missing directories are made 0700 and the lease
    directory is tightened to 0700 through its descriptor.
    """
    flags = os.O_RDONLY | _O("O_DIRECTORY") | _O("O_NOFOLLOW") | _O("O_CLOEXEC")
    local_path = Path(project) / ".syndicate"
    if create:
        try:
            os.mkdir(local_path, 0o700)
        except FileExistsError:
            pass
    local = os.open(local_path, flags)
    try:
        if create:
            try:
                os.mkdir(LEASE_DIRECTORY, 0o700, dir_fd=local)
            except FileExistsError:
                pass
        directory = os.open(LEASE_DIRECTORY, flags, dir_fd=local)
    finally:
        os.close(local)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid():
            raise PermissionError(f"{lease_directory(project)} is not owned by this user")
        if create and info.st_mode & 0o077:
            os.fchmod(directory, 0o700)
            info = os.fstat(directory)
        if info.st_mode & 0o022:
            raise PermissionError(f"{lease_directory(project)} is writable by other users")
    except BaseException:
        os.close(directory)
        raise
    return directory


def valid_run_id(value):
    return isinstance(value, str) and RUN_ID.fullmatch(value) is not None


@contextmanager
def specialist_lease(project, provider, role, run_id):
    """Hold a lease for a running delegated specialist of supervised run run_id.

    Raises OSError (or ValueError for invalid fields) if the lease cannot be
    recorded; callers fail closed. The lease is unlinked and its lock released
    when the context exits, including on exceptions.
    """
    if provider not in PROVIDERS or not valid_run_id(run_id):
        raise ValueError("invalid lease provider or run id")
    if not isinstance(role, str) or not ROLE.fullmatch(role):
        role = "unnamed-role"  # still enforced; the label is only descriptive
    payload = json.dumps({"version": 2, "run_id": run_id, "provider": provider, "role": role,
                          "created_at": time.time()}).encode()
    directory = _open_lease_directory(project, create=True)
    fd = None
    name = None
    try:
        token = uuid.uuid4().hex
        pending = f".pending-{token}"
        # The pending name never matches the lease pattern, so a supervisor
        # cannot find (and reap) it before it is locked.
        fd = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O("O_NOFOLLOW") | _O("O_CLOEXEC"),
                     0o600, dir_fd=directory)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, payload)
            os.rename(pending, f"{token}.json", src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            try:
                os.unlink(pending, dir_fd=directory)
            except OSError:
                pass
            raise
        name = f"{token}.json"
    except BaseException:
        if fd is not None:
            os.close(fd)
        os.close(directory)
        raise
    try:
        yield lease_directory(project) / name
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except OSError:
            pass
        os.close(fd)  # releases the lock
        os.close(directory)


def read_leases(project, run_id=None, on_cap=None):
    """Active, well-formed leases ({"provider", "role", "run_id"}).

    A lease is active only while another open file holds its lock. Unlocked
    leases belong to exited specialists and are unlinked through the
    verified directory descriptor while this call holds their lock. Unsafe
    entries (symbolic links, FIFOs, foreign or oversized files, an unsafe
    directory) and malformed records are ignored. With run_id, leases of
    other runs are ignored. Never blocks.

    The scan is bounded: it reads at most LEASE_SCAN_ENTRIES directory
    entries and examines at most LEASE_SCAN_LIMIT lease-named ones, in
    directory order. If either cap leaves entries unread, on_cap (if given)
    is called with a description; leases beyond the cap are not seen.
    """
    leases = []
    try:
        directory = _open_lease_directory(project, create=False)
    except OSError:
        return leases
    try:
        names, scanned, capped = [], 0, None
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if len(names) >= LEASE_SCAN_LIMIT:
                        capped = f"at least {LEASE_SCAN_LIMIT} lease files and further entries"
                        break
                    if scanned >= LEASE_SCAN_ENTRIES:
                        capped = f"more than {LEASE_SCAN_ENTRIES} entries"
                        break
                    scanned += 1
                    if LEASE_NAME.fullmatch(entry.name):
                        names.append(entry.name)
        except OSError:
            pass  # evaluate what was read before the error
        if capped and on_cap is not None:
            on_cap(f"{lease_directory(project)} holds {capped}; only part of it was examined")
        for name in names:
            try:
                fd = os.open(name, os.O_RDONLY | _O("O_NOFOLLOW") | _O("O_NONBLOCK") | _O("O_CLOEXEC"),
                             dir_fd=directory)
            except OSError:
                continue
            try:
                info = os.fstat(fd)
                if (not S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_size > LEASE_MAX_BYTES):
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass  # held: the specialist is running
                else:
                    # Nobody holds it: left by an exited or killed specialist.
                    try:
                        os.unlink(name, dir_fd=directory)
                    except OSError:
                        pass
                    continue
                raw = os.read(fd, LEASE_MAX_BYTES + 1)
            except OSError:
                continue
            finally:
                os.close(fd)
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeDecodeError, RecursionError):
                continue
            if not isinstance(data, dict):
                continue
            provider, role, owner = data.get("provider"), data.get("role"), data.get("run_id")
            if (provider not in PROVIDERS or not isinstance(role, str) or not ROLE.fullmatch(role)
                    or not valid_run_id(owner)):
                continue
            if run_id is not None and owner != run_id:
                continue
            leases.append({"provider": provider, "role": role, "run_id": owner})
    finally:
        os.close(directory)
    return leases


def reset_events(previous, current, notified, now):
    """Return deduplicated due/confirmed notices; never infer fresh capacity."""
    events = []
    current_windows = {w.get("name"): w for w in usage_windows(current)}
    for old in usage_windows(previous):
        reset = _timestamp(old.get("resets_at"))
        if reset is None or reset > now:
            continue
        key = f'{previous.get("provider")}/{old.get("name")}/{reset}'
        new = current_windows.get(old.get("name"))
        confirmed = (new and (observed_time(current) or 0) >= reset
                     and _timestamp(new.get("resets_at")) is not None and new["resets_at"] > reset)
        state = "confirmed" if confirmed else "expected"
        if notified.get(key) == "confirmed" or notified.get(key) == state:
            continue
        notified[key] = state
        events.append(f'{previous.get("provider")} {old.get("name")}: reset {state}' +
                      (" by fresh telemetry" if confirmed else "; fresh usage is not yet available"))
    return events


def monitor_tick():
    state = read_json(STATE / "monitor.json")
    previous = state.get("observations", {})
    notified = state.get("notified", {})
    if shutil.which("codex"):
        try:
            observation = save_observation("codex", codex_limits())
            if not observation["windows"]:
                raise RuntimeError("Codex returned no account limits for this login")
            state.pop("codex_error", None)
        except (OSError, RuntimeError, ValueError) as error:
            state["codex_error"] = str(error)
    else:
        state["codex_error"] = "Codex CLI is not installed or is not on PATH"
    observations = {}
    events = []
    for provider in ("claude", "codex"):
        current = read_json(STATE / f"{provider}.json")
        old = previous.get(provider, current)
        events.extend(reset_events(old, current, notified, time.time()))
        observations[provider] = current
    for message in events:
        with (STATE / "notifications.log").open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S %z')} {message}\n")
        if shutil.which("notify-send"):
            try:
                subprocess.run(["notify-send", "Syndicate limits", message], timeout=3,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                pass
    state.update(observations=observations, notified=notified, checked_at=time.time(), pid=os.getpid())
    atomic_json(STATE / "monitor.json", state)
    return events


def monitor_forever():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "monitor.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        while True:
            try:
                monitor_tick()
            except (OSError, ValueError):
                pass  # a failed refresh never overwrites the last observation
            for _ in range(POLL_SECONDS):
                if (STATE / "stop").exists():
                    (STATE / "stop").unlink(missing_ok=True)
                    return 0
                time.sleep(1)


def start_monitor(script):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Lock is authoritative, unlike a possibly reused/stale PID.
    with (STATE / "monitor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        fcntl.flock(lock, fcntl.LOCK_UN)
    (STATE / "stop").unlink(missing_ok=True)
    subprocess.Popen([sys.executable, str(script), "_limits-monitor"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def describe_limits():
    lines = []
    monitor = read_json(STATE / "monitor.json")
    for provider in ("claude", "codex"):
        data = read_json(STATE / f"{provider}.json")
        state, remaining = availability(data)
        text = f"{provider}: {state}"
        if state != "current":
            if provider == "codex":
                text += " — " + (monitor.get("codex_error") or "waiting for the account-limit monitor to refresh")
            else:
                text += " — waiting for an interactive Claude session launched through Syndicate to publish account limits"
        observed = observed_time(data)
        if observed is not None:
            text += " (observed " + format_time(observed) + ")"
        if remaining is not None:
            text += f", {remaining:g}% remaining (most constrained window)"
        for w in usage_windows(data):
            when = format_time(w.get("resets_at"))
            text += f'\n  {w.get("name")}: last observed {w["remaining_percent"]:g}% remaining; reset {when}'
        lines.append(text)
    return "\n".join(lines)


def create_handoff(project, provider, task, reason, run_id, limited=None):
    """Snapshot interrupted work. limited names the provider whose usage reading
    stopped the run (None for other reasons); it is recorded in handoff.json."""
    local = project / ".syndicate"
    target = local / "handoffs" / run_id
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    for filename in ("status.json", "checkpoint.md"):
        source = local / filename
        if source.is_file():
            shutil.copy2(source, target / filename)
    if not (target / "checkpoint.md").exists():
        (target / "checkpoint.md").write_text("No semantic checkpoint was written. Reconstruct from task, worktree, status, and session output; do not assume any work completed.\n")
    for filename, arguments in (("git-status.txt", ["status", "--short"]),
                                ("worktree.diff", ["diff", "--binary"]),
                                ("staged.diff", ["diff", "--cached", "--binary"])):
        try:
            result = subprocess.run(["git", "-C", str(project), *arguments], capture_output=True, timeout=10)
            (target / filename).write_bytes(result.stdout if result.returncode == 0 else b"Git snapshot unavailable.\n")
        except (OSError, subprocess.TimeoutExpired):
            (target / filename).write_text("Git snapshot unavailable.\n")
    atomic_json(target / "limits.json", {p: read_json(STATE / f"{p}.json") for p in ("claude", "codex")})
    (target / "handoff.md").write_text(
        f"# Interrupted Syndicate work\n\nProvider: {provider}\n\nReason: {reason}\n\n"
        f"## Original request\n\n{task}\n\n"
        "## Continue\n\nRead checkpoint.md and status.json in this directory. Verify the live worktree, "
        "including untracked files; diffs are evidence, not a complete backup. "
        "Tools may have been interrupted. Complete outstanding tests and reviews. "
        "Native session IDs and conversations do not transfer.\n\n"
        f"Session output: .syndicate/runs/{run_id}.log\n")
    record = {"path": str(target), "provider": provider, "reason": reason, "state": "waiting_on_user"}
    if limited in PROVIDERS:
        record["limited_provider"] = limited
    atomic_json(local / "handoff.json", record)
    status = read_json(local / "status.json")
    current = status.get("current") or {}
    current.update(role="chief-of-staff" if current.get("role") == "chief-of-staff" else "orchestrator",
                   round="handoff", summary=reason)
    status.update(state="waiting_on_user", current=current)
    atomic_json(local / "status.json", status)
    return target


def signal_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def process_identity(pid):
    """Linux start time prevents signalling a reused PID."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        return int(fields[1]), fields[19]
    except (OSError, ValueError, IndexError):
        return None


def descendants(root):
    if not Path("/proc").is_dir():
        return {}
    processes = {}
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            identity = process_identity(int(path.name))
            if identity:
                processes[int(path.name)] = identity
    owned = {root}
    result = {}
    while True:
        added = {pid for pid, (parent, _) in processes.items() if parent in owned and pid not in owned}
        if not added:
            return result
        for pid in added:
            result[pid] = processes[pid][1]
        owned.update(added)


def stop_group(proc, known=None):
    # Linux additionally tracks descendants that create their own process groups.
    owned = dict(known or {})
    owned.update(descendants(proc.pid))
    def send(sig):
        signal_group(proc.pid, sig)
        for pid, started in owned.items():
            identity = process_identity(pid)
            if identity and identity[1] == started:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
    send(signal.SIGINT)
    time.sleep(0.3)
    owned.update(descendants(proc.pid))
    send(signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    send(signal.SIGKILL)
    proc.wait()


def supervise(argv, project, providers, threshold, task, host, headless=False, live=None, pass_fds=(),
              on_start=None, chief=False):
    """Proxy a CLI terminal while keeping monitoring outside the model loop.

    providers are enforced for the whole run (the host). In project sessions,
    the CLI runs with SYNDICATE_RUN_ID set to this run's id, and the provider of
    each held specialist lease of this run (see specialist_lease) is also
    enforced while that specialist runs. Enforcement uses the latest real
    reading; stale or unknown telemetry only produces a warning.

    on_start(pid), if given, is called once the CLI child has been started,
    so the caller can record it as an owner of the session."""
    run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    local = project / ".syndicate"
    (local / "runs").mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(local / "run.json", {"id": run_id, "task": task, "provider": host, "started_at": time.time()})
    warned = {}  # provider -> telemetry state last warned about (one warning per transition)
    for provider in sorted(providers):
        observation = read_json(STATE / f"{provider}.json")
        state, remaining = supervision_usage(observation, chief=chief, now=clock())
        if remaining is not None and remaining <= threshold:
            reason = f"{provider}: {remaining:g}% account usage remaining (reserve {threshold:g}%); no CLI launched"
            print(f"Syndicate: {reason}. The last recorded reading counts even if it is stale or its reset "
                  f"time has passed; {forget_hint(provider)}.", file=sys.stderr)
            return 75, create_handoff(project, host, task, reason, run_id, limited=provider)
        if not chief:
            warned[provider] = state
            message = usage_warning(provider, state, observation)
            if message:
                print(f"Syndicate: WARNING: {message}", file=sys.stderr)
    interactive = not headless and sys.stdin.isatty() and sys.stdout.isatty()
    delivery = None
    if live and interactive:
        # Lazy import avoids a runtime/portfolio/live import cycle.
        from syndicate_live import Delivery
        delivery = Delivery(live['directory'], project, run_id, host,
                            chief=live.get('chief', False), interval=live.get('interval', 5),
                            transport=live.get('transport', 'auto'))
        record = read_json(local / 'run.json')
        record['live_endpoint'] = str(delivery.path)
        atomic_json(local / 'run.json', record)
        argv = delivery.prepare(argv)
    # Delegated specialists copy the run id into their leases; the supervisor
    # honours only leases of its own run. An inherited id is never passed on.
    env = {key: value for key, value in os.environ.items() if key != RUN_ENV}
    if not chief:
        env[RUN_ENV] = run_id
    old_terminal = termios.tcgetattr(sys.stdin) if interactive else None
    master = None
    sel = selectors.DefaultSelector()
    def resize(*_):
        if interactive:
            size = fcntl.ioctl(sys.stdin, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)
    if interactive:
        master, slave = pty.openpty()
        resize()
        def child_setup():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        try:
            proc = subprocess.Popen(argv, cwd=project, stdin=slave, stdout=slave, stderr=slave,
                                    preexec_fn=child_setup, pass_fds=pass_fds, env=env)
        except BaseException:
            if delivery:
                delivery.close()
            os.close(master)
            sel.close()
            raise
        finally:
            os.close(slave)
        sel.register(master, selectors.EVENT_READ, "output")
        sel.register(sys.stdin, selectors.EVENT_READ, "input")
    else:
        # Preserve stdin, stdout, and stderr semantics for headless pipelines.
        proc = subprocess.Popen(argv, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True, pass_fds=pass_fds, env=env)
        sel.register(proc.stdout, selectors.EVENT_READ, "output")
        sel.register(proc.stderr, selectors.EVENT_READ, "error")
    old_winch = signal.signal(signal.SIGWINCH, resize)
    def interrupted(*_):
        raise KeyboardInterrupt
    old_signals = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGHUP)}
    reason = None
    workers = {}
    lease_failed = False  # warn once while lease evaluation itself breaks; reset on recovery
    lease_capped = []  # a partial lease scan is reported once per run

    def lease_cap(detail):
        if not lease_capped:
            lease_capped.append(detail)
            sys.stderr.write(f"\r\nSyndicate: WARNING: {detail}; specialists whose leases were not "
                             "examined are not enforced in-run\r\n")
            sys.stderr.flush()

    trigger = None  # provider whose reading stopped the run, if any
    next_check = 0
    notices_offset = 0
    notice_file = STATE / "notifications.log"
    if notice_file.exists():
        notices_offset = notice_file.stat().st_size
    try:
        # Inside the try: an interrupt while recording the owner still stops the CLI.
        if on_start is not None:
            try:
                on_start(proc.pid)
            except Exception as error:  # recording ownership must never orphan the CLI
                print(f"syndicate: could not record the CLI owner: {error}", file=sys.stderr)
        if interactive:
            tty.setraw(sys.stdin)
        with (local / "runs" / f"{run_id}.log").open("wb") as log:
            while proc.poll() is None:
                for key, _ in sel.select(0.25):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except OSError:
                        chunk = b""
                    if key.data in ("output", "error"):
                        if chunk:
                            log.write(chunk)
                            log.flush()
                            output = sys.stderr if key.data == "error" else sys.stdout
                            output.buffer.write(chunk)
                            output.buffer.flush()
                        else:
                            sel.unregister(key.fileobj)
                    elif chunk:
                        os.write(master, chunk)
                now = clock()
                if now < next_check:
                    continue
                next_check = now + 1
                workers.update(descendants(proc.pid))
                if notice_file.exists():
                    with notice_file.open() as notices:
                        notices.seek(notices_offset)
                        for line in notices:
                            sys.stderr.write("\r\nSyndicate: " + line.rstrip() + "\r\n")
                            sys.stderr.flush()
                        notices_offset = notices.tell()
                enforced = {provider: None for provider in sorted(providers)}
                if not chief:
                    # A lease bug must never abort the host session: on any
                    # failure, treat this tick as having no leases.
                    try:
                        for lease in read_leases(project, run_id, on_cap=lease_cap):
                            enforced.setdefault(lease["provider"], lease["role"])
                    except Exception as error:
                        if not lease_failed:
                            lease_failed = True
                            sys.stderr.write("\r\nSyndicate: WARNING: could not evaluate specialist leases "
                                             f"({type(error).__name__}); delegated providers are not enforced "
                                             f"while this persists; still enforcing {host}\r\n")
                            sys.stderr.flush()
                    else:
                        if lease_failed:
                            lease_failed = False
                            sys.stderr.write("\r\nSyndicate: specialist lease evaluation recovered; delegated "
                                             "providers are enforced again\r\n")
                            sys.stderr.flush()
                for provider, role in enforced.items():
                    observation = read_json(STATE / f"{provider}.json")
                    state, remaining = supervision_usage(observation, chief=chief, now=now)
                    if remaining is not None and remaining <= threshold:
                        reason = f"{provider}: {remaining:g}% account usage remaining (reserve {threshold:g}%)"
                        trigger = provider
                        if role is not None:
                            reason += f" while the {role} specialist was running"
                        break
                    if not chief and warned.get(provider) != state:
                        warned[provider] = state
                        message = usage_warning(provider, state, observation)
                        if message:
                            sys.stderr.write(f"\r\nSyndicate: WARNING: {message}\r\n")
                            sys.stderr.flush()
                if reason:
                    break
                if delivery:
                    delivery.tick(now)
            # Drain bytes already written by a fast-exiting CLI. Do not wait
            # for worker-held descriptors after the leader exits.
            for key in list(sel.get_map().values()):
                if key.data == "input":
                    continue
                os.set_blocking(key.fd, False)
                while True:
                    try:
                        chunk = os.read(key.fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    log.write(chunk)
                    output = sys.stderr if key.data == "error" else sys.stdout
                    output.buffer.write(chunk)
                    output.buffer.flush()
    except KeyboardInterrupt:
        reason = "Interrupted by user"
    finally:
        stop_group(proc, workers)
        if delivery:
            delivery.close()
        sel.close()
        if master is not None:
            os.close(master)
        else:
            proc.stdout.close()
            proc.stderr.close()
        signal.signal(signal.SIGWINCH, old_winch)
        for sig, handler in old_signals.items():
            signal.signal(sig, handler)
        if old_terminal is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_terminal)
    if reason is None and proc.returncode != 0:
        reason = f"{host} exited with status {proc.returncode}; verify interrupted work"
    if reason:
        return proc.returncode, create_handoff(project, host, task, reason, run_id, limited=trigger)
    return proc.returncode, None


def limited_provider(project, handoff):
    """The provider recorded as having hit the reserve for this handoff, or None."""
    record = read_json(Path(project) / ".syndicate" / "handoff.json")
    limited = record.get("limited_provider")
    return limited if record.get("path") == str(handoff) and limited in PROVIDERS else None


def choose_target(host, interactive, limited=None):
    """Ask whether to continue after a handoff; None leaves work stopped.

    limited is the provider whose reading triggered the handoff. When it is a
    delegated provider (not the host), switching to it is not offered: the
    only continuation is the host for every role. Only an explicit typed
    answer continues; Enter, EOF, an interrupt, or a non-interactive run stops.
    """
    if limited in PROVIDERS and limited != host:
        print(f"\nManaged team stopped: {limited} reached the usage reserve while a delegated specialist was "
              f"running. Handoff saved. Stop here, or continue with {host} for every role, which moves the "
              f"{limited} role(s) onto {host} for this run?")
        choices, prompt = (host,), f"Type '{host}' to continue all roles on {host}, or press Enter to stop: "
    else:
        other = "codex" if host == "claude" else "claude"
        print(f"\nManaged team stopped. Handoff saved. Stop here or continue with {other} (or use only {host})?")
        choices, prompt = PROVIDERS, "Type 'claude' or 'codex' to continue all roles there, or press Enter to stop: "
    if not interactive:
        print("No interactive confirmation available; leaving work stopped.")
        return None
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    return answer if answer in choices else None
