"""
작업 큐 디렉토리 레이아웃 규약.

큐 루트 아래 task_type별 상태 디렉토리를 둔다:

    <root>/
      ├ sfp/
      │   ├ pending/       ← Producer가 씀
      │   ├ running/       ← Worker가 pick하며 이동 (atomic rename)
      │   ├ done/          ← 실행 성공 시 이동
      │   └ failed/        ← 실행 실패 시 이동
      └ sc/
          ├ pending/
          ├ running/
          ├ done/
          └ failed/

Legacy: 상태 디렉토리 도입 전 flat 구조(`<root>/sfp/config_sfp_NNNN.yaml`)에
남아있던 파일은 `legacy_dir()`로 접근. 마이그레이션 UI(Phase 2a.3)가 선택적으로
pending/으로 이동시킬 수 있다.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

TASK_TYPES: tuple[str, ...] = ("sfp", "sc")


class QueueState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def queue_dir(root: Path, task_type: str, state: QueueState) -> Path:
    """`<root>/<task_type>/<state>/` 경로 반환 (존재 보장 안 함)."""
    if task_type not in TASK_TYPES:
        raise ValueError(f"task_type은 {TASK_TYPES} 중 하나: {task_type!r}")
    return root / task_type / state.value


def legacy_dir(root: Path, task_type: str) -> Path:
    """상태 디렉토리 없이 flat하게 config가 놓였던 과거 경로.

    마이그레이션 대상 파일을 찾을 때 사용.
    """
    if task_type not in TASK_TYPES:
        raise ValueError(f"task_type은 {TASK_TYPES} 중 하나: {task_type!r}")
    return root / task_type


def ensure_queue_dirs(root: Path) -> None:
    """모든 (task_type, state) 디렉토리를 미리 생성.

    신규 수집 시작 전 호출하면 state.queue_counts가 안전하게 0을 돌려준다.
    """
    for t in TASK_TYPES:
        for s in QueueState:
            (root / t / s.value).mkdir(parents=True, exist_ok=True)
