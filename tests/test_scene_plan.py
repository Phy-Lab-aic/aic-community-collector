#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""
ScenePlan / TrialPlan DTO 단위 테스트.

실행:
    uv run tests/test_scene_plan.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.scene_plan import ScenePlan, TrialPlan  # noqa: E402


def _sample_trial(task_type: str = "sfp") -> TrialPlan:
    return TrialPlan(
        task_type=task_type,
        nic_rails=[0, 2, 4],
        nic_poses={
            0: {"translation": 0.01, "yaw": 0.05},
            2: {"translation": -0.01, "yaw": -0.05},
            4: {"translation": 0.02, "yaw": 0.0},
        },
        sc_rails=[0],
        sc_poses={0: {"translation": 0.0, "yaw": 0.0}},
        target_rail=0,
        target_port_name="sfp_port_0",
        gripper={"x": 0.0, "y": 0.015, "z": 0.04, "roll": 0.44, "pitch": -0.48, "yaw": 1.33},
    )


def test_trial_to_dict_int_keys_to_str() -> None:
    t = _sample_trial()
    d = t.to_dict()
    assert set(d["nic_poses"].keys()) == {"0", "2", "4"}, "int 키가 str로 변환되어야 함"
    assert set(d["sc_poses"].keys()) == {"0"}


def test_trial_to_dict_preserves_values() -> None:
    t = _sample_trial()
    d = t.to_dict()
    assert d["task_type"] == "sfp"
    assert d["nic_rails"] == [0, 2, 4]
    assert d["target_port_name"] == "sfp_port_0"
    assert d["gripper"]["z"] == 0.04
    assert d["nic_poses"]["2"]["yaw"] == -0.05


def test_scene_plan_with_single_trial() -> None:
    trial = _sample_trial("sc")
    plan = ScenePlan(sample_index=7, seed=49, trials=[trial])
    assert plan.sample_index == 7
    assert plan.seed == 49
    assert len(plan.trials) == 1
    assert plan.primary_task_type == "sc"


def test_scene_plan_with_multiple_trials() -> None:
    """trials_per_config=3 시나리오 (Phase 2+)."""
    t1 = _sample_trial("sfp")
    t2 = _sample_trial("sfp")
    t3 = _sample_trial("sc")
    plan = ScenePlan(sample_index=0, seed=42, trials=[t1, t2, t3])
    assert len(plan.trials) == 3
    assert plan.primary_task_type == "sfp"
    d = plan.to_dict()
    assert len(d["trials"]) == 3
    assert d["trials"][2]["task_type"] == "sc"


def test_scene_plan_empty_trials_primary_type_raises() -> None:
    plan = ScenePlan(sample_index=0, seed=42, trials=[])
    try:
        _ = plan.primary_task_type
    except ValueError:
        return
    raise AssertionError("빈 trials에서 primary_task_type 접근 시 ValueError 기대")


def test_scene_plan_to_dict_roundtrip_preserves() -> None:
    plan = ScenePlan(sample_index=3, seed=45, trials=[_sample_trial()])
    d = plan.to_dict()
    assert d["sample_index"] == 3
    assert d["seed"] == 45
    assert d["trials"][0]["nic_rails"] == [0, 2, 4]


def test_trial_default_fields_are_independent() -> None:
    """dataclass field(default_factory=...) 공유 bug 방지."""
    a = TrialPlan(task_type="sfp")
    b = TrialPlan(task_type="sfp")
    a.nic_rails.append(1)
    a.nic_poses[1] = {"translation": 0.01, "yaw": 0.0}
    assert b.nic_rails == [], "기본값 list가 공유되면 안 됨"
    assert b.nic_poses == {}, "기본값 dict가 공유되면 안 됨"


def main() -> int:
    tests = [
        ("TrialPlan.to_dict int→str 키 변환", test_trial_to_dict_int_keys_to_str),
        ("TrialPlan.to_dict 값 보존", test_trial_to_dict_preserves_values),
        ("ScenePlan 단일 trial", test_scene_plan_with_single_trial),
        ("ScenePlan 다중 trial", test_scene_plan_with_multiple_trials),
        ("ScenePlan 빈 trials → ValueError", test_scene_plan_empty_trials_primary_type_raises),
        ("ScenePlan.to_dict 왕복 보존", test_scene_plan_to_dict_roundtrip_preserves),
        ("TrialPlan 기본 필드 독립성", test_trial_default_fields_are_independent),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"✅ {name}")
        except AssertionError as e:
            print(f"❌ {name} — {e}")
            failed += 1
        except Exception as e:
            print(f"💥 {name} — {type(e).__name__}: {e}")
            failed += 1
    total = len(tests)
    print(f"\n{total - failed}/{total} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
