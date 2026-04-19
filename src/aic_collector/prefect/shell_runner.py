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


def _pg_alive(pid: int) -> bool:
    """해당 pid의 프로세스 그룹이 아직 살아있는지.

    좀비 상태 오탐 방지를 위해 우리 자식이면 `waitpid(WNOHANG)`로 먼저 reap.
    """
    try:
        reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return False  # reap 성공 — 확실히 종료됨
    except ChildProcessError:
        pass  # 우리 자식이 아니거나 이미 reap됨 — 아래 probe로 진행
    except OSError:
        pass
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def _pattern_alive(pattern: str) -> bool:
    """pgrep -f pattern이 매칭을 찾는지 (rc=0이면 생존 프로세스 존재)."""
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
    return r.returncode == 0


def graceful_cleanup(
    pids: list[int] | None = None,
    patterns: list[str] | None = None,
    grace_sec: float = 3.0,
    poll_interval: float = 0.1,
) -> float:
    """일괄 SIGTERM → polling → 잔존 시 SIGKILL. 공용 grace 윈도우.

    기존 `kill_process_tree` / `pkill_pattern`은 각각 고정 sleep을 써서 대상이 N개면
    시간이 N배로 들어감. 이 함수는 모든 대상에 한 번에 SIGTERM을 보낸 뒤 **단일
    grace_sec 윈도우** 안에서 `poll_interval`마다 생존 여부를 확인하고, 전부 죽으면
    즉시 탈출. grace 만료 시 남아 있는 것만 SIGKILL.

    Args:
        pids: 프로세스 그룹(PGID)로 취급할 PID 목록.
        patterns: `pkill -f <pattern>` 대상 문자열 목록.
        grace_sec: TERM 이후 KILL까지 최대 대기.
        poll_interval: 생존 체크 간격.

    Returns:
        실제 대기한 초.
    """
    pids = list(pids or [])
    patterns = list(patterns or [])

    # Phase 1 — broadcast SIGTERM
    for pid in pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            pass
    for pat in patterns:
        subprocess.run(["pkill", "-f", pat], capture_output=True)

    if not pids and not patterns:
        return 0.0

    # Phase 2 — polled wait
    start = time.time()
    deadline = start + grace_sec
    while True:
        now = time.time()
        if now >= deadline:
            break
        any_alive = any(_pg_alive(p) for p in pids) or any(
            _pattern_alive(pat) for pat in patterns
        )
        if not any_alive:
            return now - start
        time.sleep(poll_interval)

    # Phase 3 — SIGKILL anything remaining
    for pid in pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            pass
    for pat in patterns:
        subprocess.run(["pkill", "-9", "-f", pat], capture_output=True)
    return time.time() - start
