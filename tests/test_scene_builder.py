#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy", "pyyaml"]
# ///
"""
scene_builder.build_scene_config 동등성 테스트.

Phase 1 리팩토링이 기존 `build_training_config.build_training_config()`와
**동일한 엔진 config dict**를 생성하는지 검증 (behavior preservation).

실행:
    uv run tests/test_scene_builder.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.sampler import (  # noqa: E402
    TrainingSample,
    sample_training_configs,
)
from aic_collector.build_training_config import (  # noqa: E402
    build_training_config as legacy_build,
)
from aic_collector.scene_plan import ScenePlan, TrialPlan  # noqa: E402
from aic_collector.scene_builder import build_scene_config  # noqa: E402

TEMPLATE_PATH = PROJECT_DIR / "configs/community_random_config.yaml"


def _sample_to_plan(s: TrainingSample) -> ScenePlan:
    """TrainingSample → ScenePlan (1-trial) 변환. 테스트 로컬 헬퍼.

    sampler.py에도 같은 변환 함수가 들어갈 예정이나, 테스트가 독립적이도록
    여기서도 정의.
    """
    trial = TrialPlan(
        task_type=s.task_type,
        nic_rails=list(s.nic_rails),
        nic_poses={int(k): dict(v) for k, v in s.nic_poses.items()},
        sc_rails=list(s.sc_rails),
        sc_poses={int(k): dict(v) for k, v in s.sc_poses.items()},
        target_rail=int(s.target_rail),
        target_port_name=s.target_port_name,
        gripper=dict(s.gripper),
    )
    return ScenePlan(sample_index=s.sample_index, seed=s.seed, trials=[trial])


def test_sfp_single_trial_equivalent() -> None:
    """SFP 1-trial: legacy build_training_config == 신규 build_scene_config."""
    samples = sample_training_configs({}, "sfp", 5, 42)
    for s in samples:
        legacy_cfg = legacy_build(s, TEMPLATE_PATH)
        new_cfg = build_scene_config(_sample_to_plan(s), TEMPLATE_PATH)
        assert legacy_cfg == new_cfg, (
            f"SFP sample_index={s.sample_index} 불일치"
        )


def test_sc_single_trial_equivalent() -> None:
    samples = sample_training_configs({}, "sc", 5, 42)
    for s in samples:
        legacy_cfg = legacy_build(s, TEMPLATE_PATH)
        new_cfg = build_scene_config(_sample_to_plan(s), TEMPLATE_PATH)
        assert legacy_cfg == new_cfg, (
            f"SC sample_index={s.sample_index} 불일치"
        )


def test_different_seeds_equivalent() -> None:
    """다양한 seed에서도 동등성 유지."""
    for seed in (1, 7, 42, 100, 999):
        samples = sample_training_configs({}, "sfp", 3, seed)
        for s in samples:
            assert legacy_build(s, TEMPLATE_PATH) == build_scene_config(
                _sample_to_plan(s), TEMPLATE_PATH
            )


def test_multi_trial_trial_keys() -> None:
    """ScenePlan에 3개 trial을 넣으면 trial_1/2/3 키가 생긴다."""
    samples = sample_training_configs({}, "sfp", 3, 42)
    plan = ScenePlan(
        sample_index=0,
        seed=samples[0].seed,
        trials=[_sample_to_plan(s).trials[0] for s in samples],
    )
    cfg = build_scene_config(plan, TEMPLATE_PATH)
    assert set(cfg["trials"].keys()) == {"trial_1", "trial_2", "trial_3"}
    # 각 trial이 독립적인 scene/tasks 구조를 가져야 함
    for i, s in enumerate(samples, start=1):
        tname = f"trial_{i}"
        assert "scene" in cfg["trials"][tname]
        assert "tasks" in cfg["trials"][tname]
        # target_module_name이 sample의 target_rail과 일치
        tm = cfg["trials"][tname]["tasks"]["task_1"]["target_module_name"]
        assert tm == f"nic_card_mount_{s.target_rail}"


def test_multi_trial_mixed_task_types() -> None:
    """SFP 2개 + SC 1개 혼합 trial → 엔진 config에 정확히 반영."""
    sfp_samples = sample_training_configs({}, "sfp", 2, 42)
    sc_samples = sample_training_configs({}, "sc", 1, 42)
    trials = [
        _sample_to_plan(sfp_samples[0]).trials[0],
        _sample_to_plan(sfp_samples[1]).trials[0],
        _sample_to_plan(sc_samples[0]).trials[0],
    ]
    plan = ScenePlan(sample_index=0, seed=42, trials=trials)
    cfg = build_scene_config(plan, TEMPLATE_PATH)

    # trial_1, trial_2는 SFP (target_module = nic_card_mount_*)
    assert cfg["trials"]["trial_1"]["tasks"]["task_1"]["target_module_name"].startswith("nic_card_mount_")
    assert cfg["trials"]["trial_2"]["tasks"]["task_1"]["target_module_name"].startswith("nic_card_mount_")
    # trial_3은 SC (target_module = sc_port_*)
    assert cfg["trials"]["trial_3"]["tasks"]["task_1"]["target_module_name"].startswith("sc_port_")

    # task_board.pose도 task_type에 맞게
    assert cfg["trials"]["trial_1"]["scene"]["task_board"]["pose"]["yaw"] == 3.1415
    assert cfg["trials"]["trial_3"]["scene"]["task_board"]["pose"]["yaw"] == 3.0


def test_empty_trials_raises() -> None:
    plan = ScenePlan(sample_index=0, seed=42, trials=[])
    try:
        build_scene_config(plan, TEMPLATE_PATH)
    except ValueError:
        return
    raise AssertionError("빈 trials에서 ValueError 기대")


def main() -> int:
    tests = [
        ("SFP 1-trial 동등성", test_sfp_single_trial_equivalent),
        ("SC 1-trial 동등성", test_sc_single_trial_equivalent),
        ("다양한 seed 동등성", test_different_seeds_equivalent),
        ("다중 trial 키 생성", test_multi_trial_trial_keys),
        ("SFP+SC 혼합 trial", test_multi_trial_mixed_task_types),
        ("빈 trials → ValueError", test_empty_trials_raises),
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
