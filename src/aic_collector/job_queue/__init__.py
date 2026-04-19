"""
Producer/Consumer 파일 기반 작업 큐.

구성:
  - layout: pending/running/done/failed 디렉토리 규약
  - state:  큐 상태 조회 (카운트, 파일 목록)
  - writer: Producer (ScenePlan → pending/*.yaml)

Phase 2a에서 도입. Phase 2b에서 worker(Consumer) 추가 예정.

표준 라이브러리 `queue`와 구분하기 위해 패키지명은 `job_queue`.
"""

from aic_collector.job_queue.layout import (
    QueueState,
    TASK_TYPES,
    ensure_queue_dirs,
    legacy_dir,
    queue_dir,
)
from aic_collector.job_queue.state import (
    QueueCounts,
    all_counts,
    list_configs,
    list_legacy,
    queue_counts,
)
from aic_collector.job_queue.worker import (
    ClaimedConfig,
    claim_one,
    mark_done,
    mark_failed,
    recover_running_to_pending,
)
from aic_collector.job_queue.writer import (
    migrate_legacy_to_pending,
    next_sample_index,
    write_plan,
    write_plans,
)

__all__ = [
    "QueueState",
    "TASK_TYPES",
    "QueueCounts",
    "all_counts",
    "ensure_queue_dirs",
    "legacy_dir",
    "queue_dir",
    "list_configs",
    "list_legacy",
    "queue_counts",
    "ClaimedConfig",
    "claim_one",
    "mark_done",
    "mark_failed",
    "recover_running_to_pending",
    "migrate_legacy_to_pending",
    "next_sample_index",
    "write_plan",
    "write_plans",
]
