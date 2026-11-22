"""Quota observations, durable handoffs, and POSIX CLI supervision.

Only documented provider surfaces are used. No inference is required for a
checkpoint or a Codex quota refresh. Claude publishes via its status line.
"""
from __future__ import annotations

import contextvars
import fcntl
import json
import math
import os
import pty
import re
import select
import selectors
import shlex
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

import syndicate_worktrees

STATE = Path.home() / ".syndicate" / "limits"
STALE_SECONDS = 180
POLL_SECONDS = 30
# One print turn is enough for Claude to publish rate_limits. Kept short so a
# stale reading cannot stall launch, and small enough that the turn itself
# cannot wander into tool use.
USAGE_PROBE_TIMEOUT = 90
USAGE_PROBE_PROMPT = "ok"
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
ROLE_QUOTA_MESSAGES = {
    "claude": re.compile(r"^You['’]ve hit your (?:usage |session |weekly )?limit(?:$|[. ·])"),
    "codex": re.compile(r"^You['’]ve hit your usage limit(?:$|\.)"),
}
CLAUDE_QUOTA_RESET = re.compile(
    r"^You['’]ve hit your (?:usage |session |weekly )?limit · resets? \S"
)
ROLE_OUTPUT_TAIL = 32 * 1024
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


# Longer budgets are decided before shorter ones. A monthly cap outranks the
# weekly window, which outranks the 5-hour window.
WINDOW_KINDS = ("monthly", "weekly", "five_hour")


def _percent_used(value, allow_over=False):
    """Used-percent in [0, 100]. Spend caps may report more than 100 once exceeded."""
    if allow_over and _real(value, 0, 1e6) and value > 100:
        return 100.0
    return value


def window(name, used, reset, kind=None):
    if not _real(used, 0, 100):
        return None
    if not _real(reset, 1, MAX_TIMESTAMP):
        reset = None
    record = {"name": name, "remaining_percent": 100 - used, "resets_at": reset}
    if kind in WINDOW_KINDS:
        record["kind"] = kind
    return record


def _kind_from_minutes(minutes):
    """Map a Codex window length onto monthly, weekly, or 5-hour."""
    if not _real(minutes, 1, 1e7):
        return None
    if minutes <= 12 * 60:
        return "five_hour"
    if minutes <= 14 * 24 * 60:
        return "weekly"
    return "monthly"


def _claude_kind(name, value):
    if name == "five_hour":
        return "five_hour"
    if name == "seven_day":
        return "weekly"
    period = value.get("period") if isinstance(value, dict) else None
    return {"daily": "five_hour", "weekly": "weekly", "monthly": "monthly"}.get(period, "monthly")


def _limit_windows(bucket, rates):
    """Windows from one Codex rate-limit snapshot, including its spend cap."""
    found = []
    if not isinstance(rates, dict):
        return found
    for slot, default_kind in (("primary", "five_hour"), ("secondary", "weekly")):
        value = rates.get(slot)
        if not isinstance(value, dict):
            continue
        kind = _kind_from_minutes(value.get("windowDurationMins")) or default_kind
        found.append(window(f"{bucket}/{slot}", _percent_used(value.get("usedPercent"), True),
                            value.get("resetsAt"), kind))
    limit = rates.get("individualLimit")
    if isinstance(limit, dict):
        remaining = limit.get("remainingPercent")
        used = None if not _real(remaining, 0, 100) else 100 - remaining
        found.append(window(f"{bucket}/monthly", used, limit.get("resetsAt"), "monthly"))
    if rates.get("spendControlReached") is True and not any(item and item.get("kind") == "monthly" for item in found):
        found.append(window(f"{bucket}/monthly", 100, None, "monthly"))
    return found


def normalize(provider, data, now=None):
    windows = []
    data = data if isinstance(data, dict) else {}
    if provider == "claude":
        rates = data.get("rate_limits") or {}
        rates = rates if isinstance(rates, dict) else {}
        for name in ("five_hour", "seven_day", "spend_limit"):
            value = rates.get(name)
            if isinstance(value, dict):
                windows.append(window(name, _percent_used(value.get("used_percentage"), name == "spend_limit"),
                                      value.get("resets_at"), _claude_kind(name, value)))
    else:
        # Preserve all returned buckets: a model-specific exhausted limit must
        # not disappear merely because the default bucket is still available.
        # primary/secondary are slots, so the duration (not the slot) decides
        # whether a window is 5-hour, weekly, or monthly.
        buckets = data.get("rateLimitsByLimitId") or {"codex": data.get("rateLimits")}
        buckets = buckets if isinstance(buckets, dict) else {}
        for bucket, rates in buckets.items():
            windows.extend(_limit_windows(bucket, rates))
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


def window_kind(record):
    """monthly, weekly, or five_hour for a stored window, else None."""
    if not isinstance(record, dict):
        return None
    kind = record.get("kind")
    if kind in WINDOW_KINDS:
        return kind
    name = record.get("name")
    if not isinstance(name, str):
        return None
    if name == "five_hour" or name.endswith("/primary"):
        return "five_hour"
    if name in ("seven_day", "weekly") or name.endswith("/secondary"):
        return "weekly"
    if name == "spend_limit" or name.endswith("/monthly") or "monthly" in name:
        return "monthly"
    return None


def readings_by_kind(observation):
    """Remaining percent for each recognized budget, tightest window of that kind."""
    found = {}
    for record in usage_windows(observation):
        kind = window_kind(record)
        if kind is None:
            continue
        remaining = record["remaining_percent"]
        found[kind] = remaining if kind not in found else min(found[kind], remaining)
    return found


def latest_reading(observation, threshold=None):
    """Remaining percent the reserve decision uses, whatever the reading's age.

    Windows are judged monthly, then weekly, then 5-hour. The first of those
    at or below `threshold` is the deciding one. When every recognized window
    is above the threshold, the tightest window still counts, so a short window
    can block on its own. None means no usable reading was ever recorded. A
    passed reset time does not restore capacity, and no quota is inferred from
    tokens or elapsed time.
    """
    if threshold is not None:
        ranked = readings_by_kind(observation)
        for kind in WINDOW_KINDS:
            remaining = ranked.get(kind)
            if remaining is not None and remaining <= threshold:
                return remaining
    return min((w["remaining_percent"] for w in usage_windows(observation)), default=None)


def recorded_remaining(provider, threshold=None):
    return latest_reading(read_json(STATE / f"{provider}.json"), threshold)


class RoleUsageLimit(Exception):
    """A role could not continue on its current provider's account quota."""

    def __init__(self, reason, exit_code=75):
        super().__init__(reason)
        self.exit_code = exit_code


@contextmanager
def specialist_signals():
    """Stop the whole specialist group when its wrapper is interrupted."""
    received = None

    def interrupt(sig, _):
        nonlocal received
        if received is None:
            received = sig

    def check():
        if received is not None:
            raise SystemExit(128 + received)

    handlers = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        yield check
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def reported_role_quota(provider, stdout, stderr):
    """Match account-quota diagnostics in the provider's text and JSON output."""
    pattern = ROLE_QUOTA_MESSAGES[provider]
    if provider == "claude":
        if any(CLAUDE_QUOTA_RESET.match(line) or (line.startswith("Error: ") and pattern.match(line[7:]))
               for line in stderr.splitlines()):
            return True
    elif any(pattern.match(line.removeprefix("ERROR: "))
             for line in stderr.splitlines() if line.startswith("ERROR: ")):
        return True
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        kind = event.get("type")
        if provider == "claude":
            if kind != "result" or event.get("is_error") is not True:
                continue
            messages = [event.get("result")]
            errors = event.get("errors")
            if isinstance(errors, list):
                messages.extend(errors)
        elif kind == "error":
            messages = [event.get("message")]
        else:
            error = event.get("error")
            messages = [error.get("message")] if kind == "turn.failed" and isinstance(error, dict) else []
        if any(isinstance(message, str) and pattern.match(message) for message in messages):
            return True
    return False


def render_claude_result(output):
    """Render Claude's default JSON capture as the print CLI's text result."""
    try:
        event = json.loads(output)
    except (ValueError, RecursionError):
        return output
    if isinstance(event, dict) and event.get("type") == "result":
        result = event.get("result")
        if isinstance(result, str):
            return (result if result.endswith("\n") else result + "\n").encode(errors="replace")
        errors = event.get("errors")
        if isinstance(errors, list) and all(isinstance(error, str) for error in errors):
            return ("\n".join(errors) + "\n").encode(errors="replace")
    return output


def run_specialist_process(argv, *, cwd, env=None, reserve=None, claude_text=False, structured_stdout=False):
    """Forward CLI output, enforcing quota for just this specialist process."""
    claude_output = bytearray()
    pending_record = bytearray()
    stderr_tail = b""
    structured_quota = False
    with specialist_signals() as check_interrupt, selectors.DefaultSelector() as selector, subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True) as proc:
        for name in ("stdout", "stderr"):
            pipe = getattr(proc, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)

        def forward(name, chunk):
            stream = getattr(sys, name)
            if hasattr(stream, "buffer"):
                stream.buffer.write(chunk)
            else:
                stream.write(chunk.decode(errors="replace"))
            stream.flush()

        def inspect_structured_stdout(chunk):
            nonlocal pending_record, structured_quota
            first, *records = chunk.split(b"\n")
            pending_record.extend(first)
            if records:
                remainder = records.pop()
                structured_quota = any(
                    reported_role_quota(argv[0], record.decode(errors="replace"), "")
                    for record in (pending_record, *records)
                )
                pending_record = bytearray() if structured_quota else bytearray(remainder)

        def drain(timeout):
            nonlocal stderr_tail
            for key, _ in selector.select(timeout):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                name = key.data
                if claude_text and name == "stdout":
                    claude_output.extend(chunk)
                else:
                    forward(name, chunk)
                    if name == "stderr":
                        stderr_tail = (stderr_tail + chunk)[-ROLE_OUTPUT_TAIL:]
                    elif structured_stdout and not structured_quota:
                        inspect_structured_stdout(chunk)

        try:
            next_check = 0
            next_worker_check = 0
            workers = {}
            while proc.poll() is None:
                check_interrupt()
                now = time.monotonic()
                if now >= next_worker_check:
                    workers.update(descendants(proc.pid))
                    next_worker_check = now + 0.25
                if selector.get_map():
                    drain(0.25)
                else:
                    try:
                        proc.wait(timeout=0.25)
                    except subprocess.TimeoutExpired:
                        pass
                check_interrupt()
                now = time.monotonic()
                if reserve is not None and now >= next_check and proc.poll() is None:
                    next_check = now + 1
                    remaining = recorded_remaining(argv[0])
                    if remaining is not None and remaining <= reserve:
                        raise RoleUsageLimit(f"{argv[0]} reached the role usage reserve "
                                             f"({remaining:g}% remaining, {reserve:g}% reserve)")
            # Drain both pipes without waiting on descendants holding them open.
            for _ in range(4):
                drain(0)
            check_interrupt()
            if claude_text:
                forward("stdout", render_claude_result(claude_output))
            stdout = claude_output if claude_text else pending_record
            if proc.returncode and (structured_quota or reported_role_quota(
                    argv[0], stdout.decode(errors="replace"), stderr_tail.decode(errors="replace"))):
                raise RoleUsageLimit(f"{argv[0]} reported a usage limit", proc.returncode)
        except BaseException:
            stop_group(proc, workers)
            raise
    return proc.returncode


def supervision_usage(observation, chief=False, now=None, threshold=None):
    """(state, remaining) for enforcing the reserve with the latest real reading.

    Every supervised provider is enforced with its most recent recorded
    percentages, including stale readings and readings awaiting reset
    confirmation; unknown or stale telemetry never stops a session by itself.
    The remaining percent walks monthly, then weekly, then the 5-hour window
    when `threshold` is set. Chief sessions report the fixed state "last
    observed". Project sessions report the freshness state from availability()
    so the supervisor can warn that the reading may be outdated. Neither
    establishes current capacity.
    """
    remaining = latest_reading(observation, threshold)
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


def claude_usage_probe_command(capture=None):
    """One short Claude turn whose status line records account limits.

    Print mode does not run the status line, so this is an interactive session
    on a terminal. Tools are disabled, effort is low, and the model is Sonnet,
    so the turn is a few tokens. `capture`, when set, receives the status-line
    JSON (including the throwaway session path) before it is recorded.
    """
    script = Path(__file__).resolve().parent / "syndicate"
    command = [sys.executable, str(script), "_claude-usage", "--state", str(STATE)]
    if capture is not None:
        command += ["--capture", str(capture)]
    settings = {"statusLine": {"type": "command", "command": shlex.join(command)}}
    return ["claude",
            "--model", "sonnet",
            "--tools", "",
            "--disable-slash-commands",
            "--permission-mode", "dontAsk",
            "--effort", "low",
            "--system-prompt", "Reply with the single word ok.",
            "--settings", json.dumps(settings)]


def _discard_probe_session(capture, started):
    """Remove the one-word session the probe created, and nothing older."""
    try:
        payload = json.loads(Path(capture).read_text())
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    projects = (Path.home() / ".claude" / "projects").resolve()
    temporary = Path(tempfile.gettempdir()).resolve()

    def fresh(path):
        try:
            info = path.lstat()
        except OSError:
            return False
        return not path.is_symlink() and info.st_ctime >= started - 2

    transcript = payload.get("transcript_path")
    if isinstance(transcript, str):
        path = Path(transcript)
        try:
            if path.is_file() and fresh(path) and projects in path.resolve().parents:
                path.unlink()
        except OSError:
            pass
    scratch = payload.get("scratchpad_dir")
    if isinstance(scratch, str):
        path = Path(scratch)
        try:
            resolved = path.resolve()
            if path.is_dir() and fresh(path) and (resolved == temporary or temporary in resolved.parents):
                shutil.rmtree(path)
        except OSError:
            pass


def _run_claude_usage_probe(before, cwd):
    """Type one word into a Claude terminal and stop once limits are recorded."""
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="syndicate-usage-") as tmp:
        capture = Path(tmp) / "statusline.json"
        work = str(cwd) if cwd else tmp
        master, slave = pty.openpty()
        proc = None
        try:
            try:
                proc = subprocess.Popen(claude_usage_probe_command(capture), cwd=work,
                                        stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
            finally:
                os.close(slave)
            sent = False
            seen = b""
            deadline = time.monotonic() + USAGE_PROBE_TIMEOUT
            while time.monotonic() < deadline:
                if _probe_published("claude", before):
                    break
                remaining = deadline - time.monotonic()
                ready, _, _ = select.select([master], [], [], min(0.2, max(0, remaining)))
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                seen = (seen + chunk)[-8192:]
                if b"trust this folder" in seen or b"Not logged in" in seen:
                    break
                if not sent and b"Claude Code" in seen:
                    try:
                        os.write(master, b"ok\r")
                    except OSError:
                        break
                    sent = True
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    proc.wait(timeout=2)
            os.close(master)
        _discard_probe_session(capture, started)
    return _probe_published("claude", before)


def _probe_published(provider, before):
    """The observation written by a probe, if it is newer than `before`."""
    updated = read_json(STATE / f"{provider}.json")
    observed = observed_time(updated)
    if usage_windows(updated) and observed is not None and observed != before:
        return updated
    return None


_usage_probes = contextvars.ContextVar("syndicate_usage_probes", default=None)


def begin_usage_probes():
    """Start one launch decision. Returns a token, or None if a decision is already open."""
    if _usage_probes.get() is not None:
        return None
    return _usage_probes.set(set())


def end_usage_probes(token):
    if token is not None:
        _usage_probes.reset(token)


def _note_probe(provider):
    probes = _usage_probes.get()
    if probes is not None:
        probes.add(provider)


def _already_probed(provider):
    probes = _usage_probes.get()
    return probes is not None and provider in probes


def choose_engine(preferred, readings, threshold, candidates, ranks=None):
    """Pick the CLI to start from remaining-usage percentages.

    `preferred` wins when its remaining usage is above `threshold`. Otherwise
    the candidate with the most usage left above `threshold` wins. That
    comparison walks monthly, then weekly, then the 5-hour window. None means
    no candidate is above the threshold. A missing reading is not usage.
    `ranks` maps a CLI to the remaining percent of each kind it reported.
    """
    usable = [name for name in candidates
              if readings.get(name) is not None and readings[name] > threshold]
    if preferred in usable:
        return preferred
    if not usable:
        return None

    def better(left, right):
        left_rank = (ranks or {}).get(left) or {}
        right_rank = (ranks or {}).get(right) or {}
        for kind in WINDOW_KINDS:
            left_remaining, right_remaining = left_rank.get(kind), right_rank.get(kind)
            if left_remaining is None or right_remaining is None or left_remaining == right_remaining:
                continue
            return left if left_remaining > right_remaining else right
        if readings[left] != readings[right]:
            return left if readings[left] > readings[right] else right
        if preferred in (left, right):
            return preferred
        return left if candidates.index(left) < candidates.index(right) else right

    winner = usable[0]
    for name in usable[1:]:
        winner = better(winner, name)
    return winner


def ensure_reading(provider, cwd=None, now=None, threshold=None):
    """Return (remaining, refreshed) for one provider.

    A current reading is kept. Any other state is queried once. A failed query
    leaves the recorded reading in place and does not invent capacity.
    `threshold` selects the deciding window: monthly, then weekly, then 5-hour.
    """
    now = clock() if now is None else now
    observation = read_json(STATE / f"{provider}.json")
    remaining = latest_reading(observation, threshold)
    if remaining is not None and availability(observation, now)[0] == "current":
        return remaining, False
    state = availability(observation, now)[0]
    if remaining is None:
        print(f"Syndicate: no {provider} usage reading yet; checking {provider} before choosing a CLI.",
              file=sys.stderr)
    else:
        print(f"Syndicate: {provider} usage telemetry is {state} ({remaining:g}% remaining); "
              f"checking {provider} before choosing a CLI.", file=sys.stderr)
    refreshed = refresh_usage(provider, cwd=cwd)
    if not refreshed or latest_reading(refreshed) is None:
        if remaining is None:
            print(f"Syndicate: {provider} usage check did not return a reading.", file=sys.stderr)
        else:
            print(f"Syndicate: {provider} usage check did not return a new reading; "
                  "the last recorded reading still applies.", file=sys.stderr)
        return remaining, False
    remaining = latest_reading(refreshed, threshold)
    print(f"Syndicate: {provider} usage check recorded {remaining:g}% remaining.", file=sys.stderr)
    return remaining, True


def refresh_usage(provider, cwd=None):
    """Query one provider for a new usage reading. Returns it, or None.

    Codex uses the app-server rate-limit read and starts no model turn.
    Claude types one word into a terminal session and stops when its status
    line publishes rate limits. Print mode does not publish that status line.
    A failure leaves the recorded reading untouched. `cwd` is the trusted
    directory the Claude terminal runs in; an untrusted directory cannot
    publish limits.
    """
    if provider not in PROVIDERS:
        return None
    _note_probe(provider)
    before = observed_time(read_json(STATE / f"{provider}.json"))
    observation = None
    try:
        if provider == "codex":
            observation = save_observation("codex", codex_limits())
        else:
            observation = _run_claude_usage_probe(before, cwd)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        observation = None
    published = _probe_published(provider, before)
    if published:
        return published
    if observation and usage_windows(observation):
        return observation
    return None


def confirm_capacity(provider, threshold, now=None, cwd=None):
    """Return (observation, remaining, refreshed) for one launch decision.

    A current reading is used as recorded. A reading that is not current and
    is at or below `threshold` is refreshed once, for this provider only, and
    the new reading replaces it when the query returns one. A failed query
    leaves the recorded reading in place; nothing is inferred from the failure.
    """
    now = clock() if now is None else now
    observation = read_json(STATE / f"{provider}.json")
    remaining = latest_reading(observation, threshold)
    state = availability(observation, now)[0]
    if remaining is None or remaining > threshold or state == "current":
        return observation, remaining, False
    if _already_probed(provider):
        observation = read_json(STATE / f"{provider}.json")
        return observation, latest_reading(observation, threshold), False
    print(f"Syndicate: {provider} usage telemetry is {state} ({remaining:g}% remaining); "
          f"checking {provider} once before applying the {threshold:g}% reserve.",
          file=sys.stderr)
    refreshed = refresh_usage(provider, cwd=cwd)
    if not refreshed or latest_reading(refreshed) is None:
        print(f"Syndicate: {provider} usage check did not return a new reading; "
              "the last recorded reading still applies.", file=sys.stderr)
        return observation, remaining, False
    remaining = latest_reading(refreshed, threshold)
    print(f"Syndicate: {provider} usage check recorded {remaining:g}% remaining.", file=sys.stderr)
    return refreshed, remaining, True


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
def specialist_lease(project, provider, role, run_id, *, local_quota=False):
    """Hold a lease for a running delegated specialist of supervised run run_id.

    Raises OSError (or ValueError for invalid fields) if the lease cannot be
    recorded; callers fail closed. The lease is unlinked and its lock released
    when the context exits, including on exceptions.
    """
    if provider not in PROVIDERS or not valid_run_id(run_id):
        raise ValueError("invalid lease provider or run id")
    if not isinstance(role, str) or not ROLE.fullmatch(role):
        role = "unnamed-role"  # still enforced; the label is only descriptive
    record = {"version": 2, "run_id": run_id, "provider": provider, "role": role,
              "created_at": time.time()}
    if local_quota:
        record["local_quota"] = True
    payload = json.dumps(record).encode()
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
            lease = {"provider": provider, "role": role, "run_id": owner}
            if data.get("local_quota") is True:
                lease["local_quota"] = True
            leases.append(lease)
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
        ordered = sorted(usage_windows(data), key=lambda record: (
            WINDOW_KINDS.index(window_kind(record)) if window_kind(record) in WINDOW_KINDS else len(WINDOW_KINDS)))
        for w in ordered:
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
    # The launcher writes run.json; status.json is agent-written. Prefer the
    # launcher's task and also capture the status task when they differ.
    current = read_json(local / "status.json").get("current")
    task_ids = [read_json(local / "run.json").get("task_id"),
                current.get("task_id") if isinstance(current, dict) else None]
    task_ids = [t for t in dict.fromkeys(task_ids) if isinstance(t, str) and t]
    try:
        worktrees = syndicate_worktrees.capture_task_worktrees(target, project.resolve(), task_ids, run_id)
    except Exception as error:  # noqa: BLE001 - the handoff itself must still be written
        worktrees = f"## Task worktrees\n\nTask worktree capture failed: {error}\n"
    (target / "handoff.md").write_text(
        f"# Interrupted Syndicate work\n\nProvider: {provider}\n\nReason: {reason}\n\n"
        f"## Original request\n\n{task}\n\n"
        "## Continue\n\nRead checkpoint.md and status.json in this directory. Verify the live worktree, "
        "including untracked files; diffs are evidence, not a complete backup. "
        "Tools may have been interrupted. Complete outstanding tests and reviews. "
        "Native session IDs and conversations do not transfer.\n\n"
        f"{worktrees}\n"
        f"Session output: .syndicate/runs/{run_id}.log\n")
    record = {"path": str(target), "provider": provider, "reason": reason, "state": "waiting_on_user"}
    if limited in PROVIDERS:
        record["limited_provider"] = limited
    atomic_json(local / "handoff.json", record)
    status = read_json(local / "status.json")
    current = status.get("current") if isinstance(status.get("current"), dict) else {}
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


def write_run_record(project, task, host, threshold, task_owner):
    run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    record = {"id": run_id, "task": task, "provider": host,
              "reserve": threshold, "started_at": time.time()}
    if task_owner:
        record["task_id"], record["session_id"] = task_owner
    atomic_json(project / ".syndicate" / "run.json", record)
    return run_id


def supervise(argv, project, providers, threshold, task, host, headless=False, live=None, pass_fds=(),
              on_start=None, chief=False, task_owner=None):
    """Proxy a CLI terminal while keeping monitoring outside the model loop.

    providers are enforced for the whole run (the host). In project sessions
    the CLI receives SYNDICATE_RUN_ID. Specialists of this run enforce their
    own quota and backoff. Legacy leases still use supervisor enforcement.
    Enforcement uses the latest real reading. A non-current reading at or
    below the reserve is refreshed once before it can refuse launch, and only
    for the CLI this session is about to run.

    on_start(pid), if given, is called once the CLI child has been started,
    so the caller can record it as an owner of the session."""
    local = project / ".syndicate"
    (local / "runs").mkdir(parents=True, exist_ok=True, mode=0o700)
    run_id = write_run_record(project, task, host, threshold, task_owner)
    warned = {}  # provider -> telemetry state last warned about (one warning per transition)
    for provider in sorted(providers):
        now = clock()
        observation = read_json(STATE / f"{provider}.json")
        state, remaining = supervision_usage(observation, chief=chief, now=now, threshold=threshold)
        refreshed = False
        # Other providers in the set keep their recorded readings. Querying
        # them would spend a turn on a CLI this session is not running.
        if (provider == host and remaining is not None and remaining <= threshold
                and availability(observation, now)[0] != "current"):
            observation, remaining, refreshed = confirm_capacity(provider, threshold, now=now, cwd=project)
            state = supervision_usage(observation, chief=chief, now=now, threshold=threshold)[0]
        if remaining is not None and remaining <= threshold:
            reason = f"{provider}: {remaining:g}% account usage remaining (reserve {threshold:g}%); no CLI launched"
            if refreshed:
                detail = (f"A fresh {provider} reading is still at or below the reserve; "
                          f"{forget_hint(provider)}")
            else:
                detail = ("The last recorded reading counts even if it is stale or its reset "
                          f"time has passed; {forget_hint(provider)}")
            print(f"Syndicate: {reason}. {detail}.", file=sys.stderr)
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
                            if not lease.get("local_quota"):
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
                    state, remaining = supervision_usage(observation, chief=chief, now=now, threshold=threshold)
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
