from __future__ import annotations

import os
import signal
import subprocess
import threading
import time


def _stream_to_file_and_stdout(proc: subprocess.Popen, log_path: str) -> None:
    """Daemon thread target: relay stdout to log file and print()."""
    with open(log_path, "w") as f:
        for line in proc.stdout:
            f.write(line)
            f.flush()
            print(line, end="")


def _stream_to_file(proc: subprocess.Popen, log_path: str) -> None:
    """Daemon thread target: relay stdout to log file only."""
    with open(log_path, "w") as f:
        for line in proc.stdout:
            f.write(line)
            f.flush()


def run_shell_process(
    cmd: list[str],
    log_path: str,
    env: dict[str, str] | None = None,
    timeout_sec: float | None = None,
    cwd: str | None = None,
) -> tuple[int, bool]:
    """Run cmd, stream output to log + stdout, optionally enforce timeout."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
        env=env,
        cwd=cwd,
    )
    t = threading.Thread(target=_stream_to_file_and_stdout, args=(proc, log_path), daemon=True)
    t.start()

    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        return proc.returncode, True

    t.join(timeout=5)
    return proc.returncode, False


def run_process_background(
    cmd: list[str],
    log_path: str,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> int:
    """Launch cmd in background, stream to log file, return PID."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
        env=env,
        cwd=cwd,
    )
    t = threading.Thread(target=_stream_to_file, args=(proc, log_path), daemon=True)
    t.start()
    return proc.pid


def run_process_until_log_match(
    cmd: list[str],
    log_path: str,
    pattern: str,
    timeout_sec: float = 300,
    poll_interval_sec: float = 2.0,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[bool, int]:
    """Launch cmd, poll log for pattern, return (matched, pid)."""
    pid = run_process_background(cmd, log_path, env=env, cwd=cwd)
    deadline = time.monotonic() + timeout_sec

    while time.monotonic() < deadline:
        time.sleep(poll_interval_sec)
        try:
            with open(log_path) as f:
                if pattern in f.read():
                    return True, pid
        except FileNotFoundError:
            pass

    return False, pid


def kill_process_tree(
    pid: int,
    stop_signal: signal.Signals = signal.SIGTERM,
    grace_sec: float = 3,
) -> None:
    """Send stop_signal then SIGKILL to the process group."""
    try:
        os.killpg(os.getpgid(pid), stop_signal)
    except OSError:
        pass

    time.sleep(grace_sec)

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        pass


def pkill_pattern(pattern: str) -> None:
    """pkill -f pattern, wait, then pkill -9 -f pattern."""
    subprocess.run(["pkill", "-f", pattern], capture_output=True)
    time.sleep(2)
    subprocess.run(["pkill", "-9", "-f", pattern], capture_output=True)
