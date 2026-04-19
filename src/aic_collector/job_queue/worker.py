"""
Consumer 원시 — pending→running→done/failed atomic 전이.

`claim_one()`은 POSIX atomic rename을 사용해 **여러 워커가 동시에 돌아도
같은 파일을 두 번 가져가지 않는다**. 실제 엔진 실행은 이 모듈의 책임이 아니며,
호출자가 claim의 running_path를 받아 engine을 실행한 후 결과에 따라
`mark_done()` 또는 `mark_failed()`를 부른다.

Phase 2b UI/CLI와 결합해 파일 기반 작업 큐를 완성한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from aic_collector.job_queue.layout import (
    TASK_TYPES,
    QueueState,
    queue_dir,
)

_FILENAME_RE = re.compile(r"^config_(sfp|sc)_(\d+)\.yaml$")


@dataclass(frozen=True)
class ClaimedConfig:
    """워커가 claim한 config 1개의 실행 정보."""

    task_type: str
    sample_index: int
    running_path: Path
    """pending에서 running/으로 이동된 현재 경로."""

    @property
    def name(self) -> str:
        return self.running_path.name


def _parse_filename(name: str) -> tuple[str, int] | None:
    m = _FILENAME_RE.match(name)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def claim_one(
    root: Path,
    task_types: list[str] | None = None,
) -> ClaimedConfig | None:
    """pending에서 1개를 atomic rename으로 running/으로 가져간다.

    Args:
        root: 큐 루트.
        task_types: 소비할 task_type 리스트 (None이면 전부). 순서대로 탐색.

    Returns:
        `ClaimedConfig` — 성공. `None` — 모든 대상 pending이 비어있음.

    동시성:
        `Path.rename()`은 POSIX 환경에서 atomic. 두 워커가 같은 파일을
        시도하면 한쪽만 성공하고 다른 쪽은 `FileNotFoundError`가 발생해
        다음 후보로 넘어간다.
    """
    targets = list(task_types) if task_types else list(TASK_TYPES)
    for tt in targets:
        if tt not in TASK_TYPES:
            raise ValueError(f"task_type은 {TASK_TYPES} 중 하나: {tt!r}")

        pending = queue_dir(root, tt, QueueState.PENDING)
        if not pending.exists():
            continue
        running = queue_dir(root, tt, QueueState.RUNNING)
        running.mkdir(parents=True, exist_ok=True)

        for src in sorted(pending.iterdir()):
            if not src.is_file() or src.suffix != ".yaml":
                continue
            parsed = _parse_filename(src.name)
            if parsed is None or parsed[0] != tt:
                continue
            dst = running / src.name
            try:
                src.rename(dst)  # atomic
            except FileNotFoundError:
                continue  # 다른 워커가 가져감 — 다음 후보
            return ClaimedConfig(
                task_type=tt,
                sample_index=parsed[1],
                running_path=dst,
            )
    return None


def mark_done(claim: ClaimedConfig, root: Path) -> Path:
    """running→done 이동. 덮어쓰지 않음 (이미 있으면 IOError)."""
    done = queue_dir(root, claim.task_type, QueueState.DONE)
    done.mkdir(parents=True, exist_ok=True)
    dst = done / claim.running_path.name
    if dst.exists():
        raise FileExistsError(f"done/에 이미 존재: {dst}")
    claim.running_path.rename(dst)
    return dst


def mark_failed(claim: ClaimedConfig, root: Path) -> Path:
    """running→failed 이동."""
    failed = queue_dir(root, claim.task_type, QueueState.FAILED)
    failed.mkdir(parents=True, exist_ok=True)
    dst = failed / claim.running_path.name
    if dst.exists():
        raise FileExistsError(f"failed/에 이미 존재: {dst}")
    claim.running_path.rename(dst)
    return dst


def recover_running_to_pending(root: Path, task_type: str) -> int:
    """비정상 종료로 running/에 남은 파일을 pending/으로 되돌린다.

    워커 재시작 시 호출하면 안전. 이미 pending에 같은 이름이 있으면 건너뜀.
    Returns: 되돌린 파일 수.
    """
    if task_type not in TASK_TYPES:
        raise ValueError(f"task_type은 {TASK_TYPES} 중 하나: {task_type!r}")
    running = queue_dir(root, task_type, QueueState.RUNNING)
    pending = queue_dir(root, task_type, QueueState.PENDING)
    if not running.exists():
        return 0
    pending.mkdir(parents=True, exist_ok=True)
    moved = 0
    for f in running.iterdir():
        if not f.is_file() or f.suffix != ".yaml":
            continue
        dst = pending / f.name
        if dst.exists():
            continue
        f.rename(dst)
        moved += 1
    return moved
