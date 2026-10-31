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
from pathlib import Path

STATE = Path.home() / ".syndicate" / "limits"
STALE_SECONDS = 180
POLL_SECONDS = 30
UNKNOWN_GRACE_SECONDS = 60


def read_json(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
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
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
        return None
    if isinstance(reset, bool) or not isinstance(reset, (int, float)) or not math.isfinite(reset) or reset <= 0:
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


def availability(observation, now=None):
    now = time.time() if now is None else now
    windows = observation.get("windows") or []
    if not windows:
        return "unknown", None
    if now - observation.get("observed_at", 0) > STALE_SECONDS:
        return "stale", None
    if any(w.get("resets_at") is not None and now >= w["resets_at"] for w in windows):
        return "awaiting reset confirmation", None
    return "current", min(w["remaining_percent"] for w in windows)


def reset_events(previous, current, notified, now):
    """Return deduplicated due/confirmed notices; never infer fresh capacity."""
    events = []
    current_windows = {w["name"]: w for w in current.get("windows", [])}
    for old in previous.get("windows", []):
        reset = old.get("resets_at")
        if reset is None or reset > now:
            continue
        key = f'{previous.get("provider")}/{old["name"]}/{reset}'
        new = current_windows.get(old["name"])
        confirmed = (new and current.get("observed_at", 0) >= reset
                     and new.get("resets_at") is not None and new["resets_at"] > reset)
        state = "confirmed" if confirmed else "expected"
        if notified.get(key) == "confirmed" or notified.get(key) == state:
            continue
        notified[key] = state
        events.append(f'{previous.get("provider")} {old["name"]}: reset {state}' +
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
        observed = data.get("observed_at")
        if observed:
            text += " (observed " + time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(observed)) + ")"
        if remaining is not None:
            text += f", {remaining:g}% remaining (most constrained window)"
        for w in data.get("windows", []):
            reset = w.get("resets_at")
            when = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(reset)) if reset else "unknown"
            text += f'\n  {w["name"]}: last observed {w["remaining_percent"]:g}% remaining; reset {when}'
        lines.append(text)
    return "\n".join(lines)


def create_handoff(project, provider, task, reason, run_id):
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
    atomic_json(local / "handoff.json", {"path": str(target), "provider": provider,
                                        "reason": reason, "state": "waiting_on_user"})
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


def supervise(argv, project, providers, threshold, task, host, headless=False):
    """Proxy a CLI terminal while keeping monitoring outside the model loop."""
    run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    local = project / ".syndicate"
    (local / "runs").mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(local / "run.json", {"id": run_id, "task": task, "provider": host, "started_at": time.time()})
    for provider in providers:
        state, remaining = availability(read_json(STATE / f"{provider}.json"))
        if remaining is not None and remaining <= threshold:
            reason = f"{provider}: {remaining:g}% account usage remaining (reserve {threshold:g}%); no CLI launched"
            return 75, create_handoff(project, host, task, reason, run_id)
    interactive = not headless and sys.stdin.isatty() and sys.stdout.isatty()
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
                                    preexec_fn=child_setup)
        except BaseException:
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
                                start_new_session=True)
        sel.register(proc.stdout, selectors.EVENT_READ, "output")
        sel.register(proc.stderr, selectors.EVENT_READ, "error")
    old_winch = signal.signal(signal.SIGWINCH, resize)
    def interrupted(*_):
        raise KeyboardInterrupt
    old_signals = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGHUP)}
    reason = None
    workers = {}
    unknown_since = {}
    next_check = 0
    notices_offset = 0
    notice_file = STATE / "notifications.log"
    if notice_file.exists():
        notices_offset = notice_file.stat().st_size
    try:
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
                now = time.time()
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
                for provider in providers:
                    state, remaining = availability(read_json(STATE / f"{provider}.json"), now)
                    if remaining is not None:
                        unknown_since.pop(provider, None)
                        if remaining <= threshold:
                            reason = f"{provider}: {remaining:g}% account usage remaining (reserve {threshold:g}%)"
                            break
                    else:
                        since = unknown_since.setdefault(provider, now)
                        if now - since >= UNKNOWN_GRACE_SECONDS:
                            reason = f"{provider}: usage {state} for {UNKNOWN_GRACE_SECONDS:g} seconds; cannot enforce the reserve"
                            break
                if reason:
                    break
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
        return proc.returncode, create_handoff(project, host, task, reason, run_id)
    return proc.returncode, None


def choose_target(host, interactive):
    other = "codex" if host == "claude" else "claude"
    print(f"\nManaged team stopped. Handoff saved. Stop here or continue with {other} (or use only {host})?")
    if not interactive:
        print("No interactive confirmation available; leaving work stopped.")
        return None
    try:
        answer = input("Type 'claude' or 'codex' to continue all roles there, or press Enter to stop: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    return answer if answer in ("claude", "codex") else None
