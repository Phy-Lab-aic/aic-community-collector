from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.job_queue import QueueState, queue_dir
from aic_collector.team_preset import TeamPreset
from aic_collector.webapp import build_team_mode_state, build_team_submit_preset


def _preset(
    *,
    shard_stride: int = 10,
    index_width: int = 5,
    tasks: dict[str, int] | None = None,
) -> TeamPreset:
    return TeamPreset(
        base_seed=42,
        shard_stride=shard_stride,
        index_width=index_width,
        strategy="uniform",
        ranges={},
        scene={
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
        },
        tasks=tasks or {"sfp_default_count": 6, "sc_default_count": 0},
        members=(
            {"id": "m0", "name": "Member 0"},
            {"id": "m1", "name": "Member 1"},
        ),
        preset_hash="sha256:test",
    )


def _touch_config(queue_root: Path, task_type: str, state: QueueState, index: int, width: int = 5) -> None:
    path = queue_dir(queue_root, task_type, state) / f"config_{task_type}_{index:0{width}d}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


def test_build_team_mode_state_reports_slot_usage_and_clamps_sfp_count(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    preset = _preset(tasks={"sfp_default_count": 9, "sc_default_count": 0})
    for index in (10, 11, 12):
        _touch_config(queue_root, "sfp", QueueState.PENDING, index)

    state = build_team_mode_state(
        preset,
        queue_root=queue_root,
        member_id="m1",
        requested_sfp_count=20,
    )

    assert state["slot_start"] == 10
    assert state["slot_end_exclusive"] == 20
    assert state["used_slots"] == 3
    assert state["remaining_slots"] == 7
    assert state["next_start_index"] == 13
    assert state["preview_filename"] == "config_sfp_00013.yaml"
    assert state["default_sfp_count"] == 7
    assert state["selected_sfp_count"] == 7
    assert state["slot_exhausted"] is False


def test_build_team_mode_state_marks_slot_exhaustion_and_zeroes_counts(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    preset = _preset()
    for index in range(10, 20):
        _touch_config(queue_root, "sfp", QueueState.PENDING, index)

    state = build_team_mode_state(
        preset,
        queue_root=queue_root,
        member_id="m1",
        requested_sfp_count=3,
    )

    assert state["slot_start"] == 10
    assert state["slot_end_exclusive"] == 20
    assert state["used_slots"] == 10
    assert state["remaining_slots"] == 0
    assert state["next_start_index"] is None
    assert state["preview_filename"] is None
    assert state["default_sfp_count"] == 0
    assert state["selected_sfp_count"] == 0
    assert state["slot_exhausted"] is True


def test_build_team_submit_preset_overrides_runtime_sfp_count_only() -> None:
    preset = _preset(tasks={"sfp_default_count": 8, "sc_default_count": 0})

    submit_preset = build_team_submit_preset(preset, sfp_count=5)

    assert submit_preset.tasks["sfp"] == 5
    assert submit_preset.tasks["sfp_default_count"] == 8
    assert submit_preset.tasks["sc_default_count"] == 0
    assert "sfp" not in preset.tasks


def test_build_team_mode_state_rejects_missing_sfp_default_key(tmp_path: Path) -> None:
    preset = _preset(tasks={"sc_default_count": 0})

    with pytest.raises(ValueError, match="sfp_default_count"):
        build_team_mode_state(
            preset,
            queue_root=tmp_path / "queue",
            member_id="m0",
        )
