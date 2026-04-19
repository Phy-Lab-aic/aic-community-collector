"""
작업 큐 상태 조회.

Producer/Consumer 양쪽에서 사용. 파일시스템을 직접 스캔하므로 별도 DB 없이
동작하며, atomic rename으로 워커 간 동시성을 처리한다(state를 "상태 디렉토리
소속"으로 표현).

Legacy 파일(상태 디렉토리 바깥, flat 구조)도 별도 카운트로 노출한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aic_collector.job_queue.layout import (
    TASK_TYPES,
    QueueState,
    legacy_dir,
    queue_dir,
)


@dataclass(frozen=True)
class QueueCounts:
    """단일 task_type 큐의 상태별 카운트."""

    pending: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0
    legacy: int = 0

    @property
    def total(self) -> int:
        return self.pending + self.running + self.done + self.failed + self.legacy


def list_configs(root: Path, task_type: str, state: QueueState) -> list[Path]:
    """`<root>/<task_type>/<state>/` 아래 *.yaml 파일 정렬 리스트.

    디렉토리가 없으면 빈 리스트.
    """
    d = queue_dir(root, task_type, state)
    if not d.exists():
        return []
    return sorted(
        f for f in d.iterdir() if f.is_file() and f.suffix == ".yaml"
    )


def list_legacy(root: Path, task_type: str) -> list[Path]:
    """`<root>/<task_type>/*.yaml` — 상태 디렉토리가 아닌 직접 하위 파일만.

    상태 디렉토리 도입 전에 생성된 config들을 열거한다.
    """
    d = legacy_dir(root, task_type)
    if not d.exists():
        return []
    return sorted(
        f for f in d.iterdir() if f.is_file() and f.suffix == ".yaml"
    )


def queue_counts(root: Path, task_type: str) -> QueueCounts:
    """단일 task_type 큐의 pending/running/done/failed/legacy 카운트."""
    return QueueCounts(
        pending=len(list_configs(root, task_type, QueueState.PENDING)),
        running=len(list_configs(root, task_type, QueueState.RUNNING)),
        done=len(list_configs(root, task_type, QueueState.DONE)),
        failed=len(list_configs(root, task_type, QueueState.FAILED)),
        legacy=len(list_legacy(root, task_type)),
    )


def all_counts(root: Path) -> dict[str, QueueCounts]:
    """모든 task_type의 큐 카운트를 dict로."""
    return {t: queue_counts(root, t) for t in TASK_TYPES}
