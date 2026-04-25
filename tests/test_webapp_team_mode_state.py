from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.job_queue import QueueState, queue_dir  # noqa: E402
from aic_collector.team_preset import (  # noqa: E402
    PresetError,
    TeamPreset,
    append_claim,
    load_preset,
    submit_team_claim,
)
from aic_collector.webapp import (  # noqa: E402
    build_team_mode_state,
    build_team_preview_scene_config,
    build_validated_preset_ranges,
    render_scene_svg,
    build_team_slot_summary,
    build_team_submit_preset,
    sync_manage_count_state,
    sync_manage_widget_state,
)


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
        ranges={
            "nic_translation": (-0.0215, 0.0234),
            "nic_yaw": (-0.1745, 0.1745),
            "sc_translation": (-0.06, 0.055),
            "gripper_xy": 0.002,
            "gripper_z": 0.002,
            "gripper_rpy": 0.04,
        },
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


def test_build_team_mode_state_stays_aligned_with_submit_when_stale_ledger_claim_exists(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    ledger_path = tmp_path / "ledger.yaml"
    preset = _preset(index_width=6, tasks={"sfp_default_count": 4, "sc_default_count": 0})

    append_claim(
        ledger_path,
        member_id="m0",
        task_type="sfp",
        base_seed=preset.base_seed,
        start_index=0,
        count=4,
        strategy=preset.strategy,
        queue_root=queue_root,
        preset_hash=preset.preset_hash,
    )

    state = build_team_mode_state(
        preset,
        queue_root=queue_root,
        ledger_path=ledger_path,
        member_id="m0",
    )
    submit_preset = build_team_submit_preset(preset, sfp_count=state["selected_sfp_count"])
    result = submit_team_claim(
        submit_preset,
        member_id="m0",
        task_type="sfp",
        queue_root=queue_root,
        ledger_path=ledger_path,
        template_path=PROJECT_DIR / "configs/community_random_config.yaml",
    )

    assert state["used_slots"] == 4
    assert state["remaining_slots"] == 6
    assert state["next_start_index"] == 4
    assert state["preview_filename"] == "config_sfp_000004.yaml"
    assert result.start_index == state["next_start_index"]


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


def test_build_team_mode_state_rejects_nonzero_sc_default_count(tmp_path: Path) -> None:
    preset = _preset(tasks={"sfp_default_count": 4, "sc_default_count": 1})

    with pytest.raises(PresetError, match="tasks.sc_default_count"):
        build_team_mode_state(
            preset,
            queue_root=tmp_path / "queue",
            member_id="m0",
        )


def test_build_team_submit_preset_rejects_nonzero_sc_default_count() -> None:
    preset = _preset(tasks={"sfp_default_count": 8, "sc_default_count": 1})

    with pytest.raises(PresetError, match="tasks.sc_default_count"):
        build_team_submit_preset(preset, sfp_count=5)


def test_build_team_preview_scene_config_threads_fixed_target_into_collection() -> None:
    preset = _preset()
    preset = TeamPreset(
        base_seed=preset.base_seed,
        shard_stride=preset.shard_stride,
        index_width=preset.index_width,
        strategy=preset.strategy,
        ranges=preset.ranges,
        scene={
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
            "fixed_target": {"sfp": {"rail": 0, "port": "sfp_port_0"}},
        },
        tasks=preset.tasks,
        members=preset.members,
        preset_hash=preset.preset_hash,
    )

    cfg = build_team_preview_scene_config(preset)

    assert cfg == {
        "scene": {
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
        },
        "collection": {
            "fixed_target": {"sfp": {"rail": 0, "port": "sfp_port_0"}},
        },
        "ranges": {
            "nic_translation": (-0.0215, 0.0234),
            "nic_yaw": (-0.1745, 0.1745),
            "sc_translation": (-0.06, 0.055),
            "gripper_xy": 0.002,
            "gripper_z": 0.002,
            "gripper_rpy": 0.04,
        },
    }


def test_build_team_preview_scene_config_accepts_fixed_target_from_loaded_preset(
    tmp_path: Path,
) -> None:
    preset_path = tmp_path / "preset.yaml"
    preset_path.write_text(
        """
team:
  base_seed: 42
  shard_stride: 10
  index_width: 5
sampling:
  strategy: uniform
  ranges:
    nic_translation: [-0.0215, 0.0234]
    nic_yaw: [-0.1745, 0.1745]
    sc_translation: [-0.06, 0.055]
    gripper_xy: 0.002
    gripper_z: 0.002
    gripper_rpy: 0.04
scene:
  nic_count_range: [1, 1]
  sc_count_range: [1, 1]
  target_cycling: false
  fixed_target:
    sfp: {rail: 0, port: "sfp_port_0"}
    sc: null
tasks:
  sfp_default_count: 1
  sc_default_count: 0
members:
  - id: m0
    name: Member 0
""".strip(),
        encoding="utf-8",
    )

    preset = load_preset(preset_path)

    assert preset is not None
    assert build_team_preview_scene_config(preset)["collection"] == {
        "fixed_target": {
            "sfp": {"rail": 0, "port": "sfp_port_0"},
            "sc": None,
        }
    }


def test_build_validated_preset_ranges_rejects_out_of_bounds_or_reversed_values() -> None:
    preset = TeamPreset(
        base_seed=42,
        shard_stride=10,
        index_width=5,
        strategy="uniform",
        ranges={
            "nic_translation": (0.1, -0.1),
            "nic_yaw": (-0.1745, 0.1745),
            "sc_translation": (-0.06, 0.055),
            "gripper_xy": 0.1,
            "gripper_z": 0.002,
            "gripper_rpy": 0.04,
        },
        scene={
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
        },
        tasks={"sfp_default_count": 1, "sc_default_count": 0},
        members=({"id": "m0", "name": "Member 0"},),
        preset_hash="sha256:test",
    )

    with pytest.raises(PresetError, match="sampling.ranges.nic_translation"):
        build_validated_preset_ranges(preset)


def test_build_validated_preset_ranges_rejects_yaml_boolean_pair_value() -> None:
    preset = TeamPreset(
        base_seed=42,
        shard_stride=10,
        index_width=5,
        strategy="uniform",
        ranges={
            "nic_translation": (False, 0.0234),
            "nic_yaw": (-0.1745, 0.1745),
            "sc_translation": (-0.06, 0.055),
            "gripper_xy": 0.002,
            "gripper_z": 0.002,
            "gripper_rpy": 0.04,
        },
        scene={
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
        },
        tasks={"sfp_default_count": 1, "sc_default_count": 0},
        members=({"id": "m0", "name": "Member 0"},),
        preset_hash="sha256:test",
    )

    with pytest.raises(PresetError, match="sampling.ranges.nic_translation"):
        build_validated_preset_ranges(preset)


def test_build_validated_preset_ranges_rejects_yaml_boolean_spread_value() -> None:
    preset = TeamPreset(
        base_seed=42,
        shard_stride=10,
        index_width=5,
        strategy="uniform",
        ranges={
            "nic_translation": (-0.0215, 0.0234),
            "nic_yaw": (-0.1745, 0.1745),
            "sc_translation": (-0.06, 0.055),
            "gripper_xy": True,
            "gripper_z": 0.002,
            "gripper_rpy": 0.04,
        },
        scene={
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
        },
        tasks={"sfp_default_count": 1, "sc_default_count": 0},
        members=({"id": "m0", "name": "Member 0"},),
        preset_hash="sha256:test",
    )

    with pytest.raises(PresetError, match="sampling.ranges.gripper_xy"):
        build_validated_preset_ranges(preset)


def test_build_team_preview_scene_config_rejects_malformed_fixed_target_entries() -> None:
    preset = TeamPreset(
        base_seed=42,
        shard_stride=10,
        index_width=5,
        strategy="uniform",
        ranges=_preset().ranges,
        scene={
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
            "fixed_target": {
                "sfp": {"rail": True, "port": "sfp_port_0"},
                "sc": "bad-shape",
            },
        },
        tasks={"sfp_default_count": 1, "sc_default_count": 0},
        members=({"id": "m0", "name": "Member 0"},),
        preset_hash="sha256:test",
    )

    with pytest.raises(PresetError, match="scene.fixed_target.sfp"):
        build_team_preview_scene_config(preset)


def test_build_team_preview_scene_config_rejects_invalid_sfp_target_tuple() -> None:
    preset = TeamPreset(
        base_seed=42,
        shard_stride=10,
        index_width=5,
        strategy="uniform",
        ranges=_preset().ranges,
        scene={
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
            "fixed_target": {
                "sfp": {"rail": 4, "port": "sfp_port_9"},
            },
        },
        tasks={"sfp_default_count": 1, "sc_default_count": 0},
        members=({"id": "m0", "name": "Member 0"},),
        preset_hash="sha256:test",
    )

    with pytest.raises(PresetError, match="scene.fixed_target.sfp"):
        build_team_preview_scene_config(preset)


def test_build_team_preview_scene_config_rejects_boolean_port() -> None:
    preset = TeamPreset(
        base_seed=42,
        shard_stride=10,
        index_width=5,
        strategy="uniform",
        ranges=_preset().ranges,
        scene={
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
            "fixed_target": {
                "sfp": {"rail": 0, "port": True},
            },
        },
        tasks={"sfp_default_count": 1, "sc_default_count": 0},
        members=({"id": "m0", "name": "Member 0"},),
        preset_hash="sha256:test",
    )

    with pytest.raises(PresetError, match="scene.fixed_target.sfp"):
        build_team_preview_scene_config(preset)


def test_render_scene_svg_threads_fixed_target_to_sampler(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_sample_scenes(cfg: dict[str, object], task_type: str, sample_count: int, seed: int) -> list[object]:
        seen["cfg"] = cfg
        return [
            SimpleNamespace(
                sample_index=0,
                trials=[
                    SimpleNamespace(
                        target_rail=0,
                        target_port_name="sfp_port_0",
                        nic_rails=[0],
                        sc_rails=[0],
                        task_type="sfp",
                    )
                ],
            )
        ]

    monkeypatch.setattr("aic_collector.sampler.sample_scenes", fake_sample_scenes)

    svg = render_scene_svg(
        nic_range=(1, 1),
        sc_range=(1, 1),
        target_cycling=False,
        fixed_target={"sfp": {"rail": 0, "port": "sfp_port_0"}},
        sample_count=1,
    )

    assert seen["cfg"] == {
        "training": {
            "scene": {
                "nic_count_range": [1, 1],
                "sc_count_range": [1, 1],
                "target_cycling": False,
            },
            "collection": {
                "fixed_target": {"sfp": {"rail": 0, "port": "sfp_port_0"}},
            },
            "ranges": {},
        }
    }
    assert "rail 0, sfp_port_0" in svg


def test_build_team_slot_summary_returns_none_without_active_team_state() -> None:
    assert build_team_slot_summary(None, None, None) is None
    assert build_team_slot_summary(_preset(), None, "m0") is None
    assert build_team_slot_summary(None, {"slot_start": 0}, "m0") is None


def test_build_team_slot_summary_formats_caption_and_exhaustion_message() -> None:
    summary = build_team_slot_summary(
        _preset(index_width=6),
        {
            "slot_start": 10,
            "slot_end_exclusive": 20,
            "used_slots": 4,
            "remaining_slots": 6,
            "preview_filename": None,
        },
        "m1",
    )

    assert summary == {
        "caption": "팀 슬롯: 000010 ~ 000019 · 사용 4 · 남은 슬롯 6",
        "slot_exhausted_error": "m1 슬롯이 가득 찼습니다. 다른 멤버를 선택하세요.",
    }


def test_sync_manage_count_state_seeds_locked_team_mode_counts() -> None:
    session_state: dict[str, int] = {}

    sync_manage_count_state(
        session_state,
        team_widgets_locked=True,
        team_state={"selected_sfp_count": 7},
        team_sc_default_count=0,
    )

    assert session_state == {
        "mgr_sfp_count": 7,
        "mgr_sc_count": 0,
    }


def test_sync_manage_count_state_initializes_solo_defaults_once() -> None:
    session_state: dict[str, int] = {}

    sync_manage_count_state(
        session_state,
        team_widgets_locked=False,
        team_state=None,
        team_sc_default_count=0,
    )
    sync_manage_count_state(
        session_state,
        team_widgets_locked=False,
        team_state=None,
        team_sc_default_count=0,
    )

    assert session_state == {
        "mgr_sfp_count": 20,
        "mgr_sc_count": 10,
    }


def test_sync_manage_widget_state_seeds_locked_team_preset_values() -> None:
    session_state: dict[str, object] = {}

    sync_manage_widget_state(
        session_state,
        team_widgets_locked=True,
        team_state={"selected_sfp_count": 7},
        team_sc_default_count=0,
        nic_range=(1, 1),
        sc_range=(1, 1),
        target_cycling=False,
        strategy="uniform",
        seed=42,
        validated_ranges=_preset().ranges,
    )

    assert session_state == {
        "mgr_sfp_count": 7,
        "mgr_sc_count": 0,
        "mgr_nic_fixed": True,
        "mgr_nic_max": 1,
        "mgr_sc_fixed": True,
        "mgr_sc_max": 1,
        "mgr_target_cycling": False,
        "mgr_param_strategy": "uniform",
        "mgr_seed": 42,
        "mgr_range_nic_translation_range": (-0.0215, 0.0234),
        "mgr_range_nic_yaw_range": (-0.1745, 0.1745),
        "mgr_range_sc_translation_range": (-0.06, 0.055),
        "mgr_range_gripper_xy_spread": 0.002,
        "mgr_range_gripper_z_spread": 0.002,
        "mgr_range_gripper_rpy_spread": 0.04,
    }


def test_sync_manage_widget_state_initializes_solo_defaults_without_overwriting() -> None:
    session_state: dict[str, object] = {}

    sync_manage_widget_state(
        session_state,
        team_widgets_locked=False,
        team_state=None,
        team_sc_default_count=0,
        nic_range=None,
        sc_range=None,
        target_cycling=None,
        strategy=None,
        seed=None,
        validated_ranges=None,
    )
    sync_manage_widget_state(
        session_state,
        team_widgets_locked=False,
        team_state=None,
        team_sc_default_count=0,
        nic_range=None,
        sc_range=None,
        target_cycling=None,
        strategy=None,
        seed=None,
        validated_ranges=None,
    )

    assert session_state == {
        "mgr_sfp_count": 20,
        "mgr_sc_count": 10,
        "mgr_nic_fixed": False,
        "mgr_nic_max": 5,
        "mgr_sc_fixed": False,
        "mgr_sc_max": 2,
        "mgr_target_cycling": True,
        "mgr_param_strategy": "uniform",
        "mgr_seed": 42,
        "mgr_range_nic_translation_range": (-0.0215, 0.0234),
        "mgr_range_nic_yaw_range": (-0.1745, 0.1745),
        "mgr_range_sc_translation_range": (-0.06, 0.055),
        "mgr_range_gripper_xy_spread": 0.002,
        "mgr_range_gripper_z_spread": 0.002,
        "mgr_range_gripper_rpy_spread": 0.04,
    }


def test_build_team_mode_state_rejects_missing_sfp_default_key(tmp_path: Path) -> None:
    preset = _preset(tasks={"sc_default_count": 0})

    with pytest.raises(ValueError, match="sfp_default_count"):
        build_team_mode_state(
            preset,
            queue_root=tmp_path / "queue",
            member_id="m0",
        )
