"""
Producer — ScenePlan을 YAML로 직렬화해서 pending/에 기록.

파일명 규약: `config_{task_type}_{sample_index:04d}.yaml`
append 모드를 위해 `next_sample_index()`가 모든 상태 디렉토리(+legacy)를
훑어서 번호 충돌을 방지한다.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from aic_collector.job_queue.layout import (
    TASK_TYPES,
    QueueState,
    legacy_dir,
    queue_dir,
)
from aic_collector.job_queue.state import list_legacy
from aic_collector.scene_builder import build_scene_config, dump_config
from aic_collector.scene_plan import ScenePlan


def write_plan(
    plan: ScenePlan,
    root: Path,
    template_path: Path,
    index_width: int = 4,
) -> Path:
    """ScenePlan 1개를 pending/에 기록하고 경로 반환.

    task_type은 plan.primary_task_type에서 파생 → `<root>/<task>/pending/`.
    """
    task_type = plan.primary_task_type
    out_dir = queue_dir(root, task_type, QueueState.PENDING)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"config_{task_type}_{plan.sample_index:0{index_width}d}.yaml"
    out_path = out_dir / fname
    cfg = build_scene_config(plan, template_path)
    out_path.write_text(dump_config(cfg))
    return out_path


def write_plans(
    plans: list[ScenePlan],
    root: Path,
    template_path: Path,
    index_width: int = 4,
) -> list[Path]:
    """여러 ScenePlan을 pending/에 일괄 기록."""
    return [write_plan(p, root, template_path, index_width) for p in plans]


def next_sample_index(
    root: Path,
    task_type: str,
    index_width: int = 4,
) -> int:
    """기존 큐(모든 상태 + legacy)를 훑어 다음 sample_index 반환.

    `config_{task_type}_NNNN.yaml` 중 최대 NNNN + 1. 하나도 없으면 0.
    append 모드에서 번호 충돌을 막는다.
    """
    pattern = re.compile(rf"^config_{re.escape(task_type)}_(\d+)\.yaml$")
    maxn = -1

    # 모든 상태 디렉토리
    for state in QueueState:
        d = queue_dir(root, task_type, state)
        if not d.exists():
            continue
        for f in d.iterdir():
            if not f.is_file():
                continue
            m = pattern.match(f.name)
            if m:
                maxn = max(maxn, int(m.group(1)))

    # Legacy (state 디렉토리 도입 전 flat 파일)
    d = legacy_dir(root, task_type)
    if d.exists():
        for f in d.iterdir():
            if not f.is_file():
                continue
            m = pattern.match(f.name)
            if m:
                maxn = max(maxn, int(m.group(1)))

    return maxn + 1


def migrate_legacy_to_pending(root: Path) -> dict[str, int]:
    """Legacy flat 구조의 *.yaml을 `<root>/<task>/pending/`으로 이동.

    동일 이름이 이미 pending에 있으면 해당 파일은 건너뛴다 (데이터 손실 방지).

    Returns:
        task_type별로 이동한 파일 수. 예: {"sfp": 55, "sc": 22}
    """
    moved: dict[str, int] = {t: 0 for t in TASK_TYPES}
    for tt in TASK_TYPES:
        target_dir = queue_dir(root, tt, QueueState.PENDING)
        target_dir.mkdir(parents=True, exist_ok=True)
        for src in list_legacy(root, tt):
            dst = target_dir / src.name
            if dst.exists():
                continue
            shutil.move(str(src), str(dst))
            moved[tt] += 1
    return moved
