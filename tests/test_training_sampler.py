#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy", "pyyaml"]
# ///
"""
Training sampler / builder 재현성·균등성 테스트.

pytest 없이 단독 실행 가능한 assert 기반 테스트.

실행:
    uv run tests/test_training_sampler.py
"""

from __future__ import annotations

import sys
import tempfile
from collections import Counter
from pathlib import Path

# src/ 를 import path에 추가
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.sampler import sample_training_configs  # noqa: E402
from aic_collector.build_training_config import (  # noqa: E402
    build_training_config,
    next_config_index,
    write_training_configs,
)

TEMPLATE_PATH = PROJECT_DIR / "configs/community_random_config.yaml"


# ---------------------------------------------------------------------------
# Sampler: 재현성
# ---------------------------------------------------------------------------


def test_reproducibility_same_seed() -> None:
    """동일 seed + 동일 task_type + 동일 count → 동일 출력."""
    a = sample_training_configs({}, "sfp", 5, 42)
    b = sample_training_configs({}, "sfp", 5, 42)
    for x, y in zip(a, b):
        assert x.to_dict() == y.to_dict(), "동일 입력에서 다른 샘플 생성됨"


def test_different_seeds_differ() -> None:
    a = sample_training_configs({}, "sfp", 5, 42)
    b = sample_training_configs({}, "sfp", 5, 43)
    assert a[0].to_dict() != b[0].to_dict(), "다른 seed인데 동일 출력"


def test_start_index_continuity() -> None:
    """start_index로 이어서 생성하면 앞 샘플과 정렬된 시퀀스가 이어진다."""
    full = sample_training_configs({}, "sfp", 10, 42)
    a = sample_training_configs({}, "sfp", 5, 42, start_index=0)
    b = sample_training_configs({}, "sfp", 5, 42, start_index=5)
    for i in range(5):
        assert a[i].to_dict() == full[i].to_dict()
        assert b[i].to_dict() == full[i + 5].to_dict()


# ---------------------------------------------------------------------------
# Sampler: 결정적 순환
# ---------------------------------------------------------------------------


def test_sfp_target_cycling_20samples() -> None:
    """20개 SFP 샘플 → 각 target 정확히 2번씩."""
    samples = sample_training_configs({}, "sfp", 20, 42)
    targets = Counter((s.target_rail, s.target_port_name) for s in samples)
    assert len(targets) == 10, f"10종이 아니라 {len(targets)}종 등장"
    for key, count in targets.items():
        assert count == 2, f"{key} 균등성 위반: {count}회"


def test_fixed_target_sfp_forces_same_target() -> None:
    """collection.fixed_target가 있으면 SFP target은 항상 그 값이어야 한다."""
    training_cfg = {
        "collection": {
            "seed": 42,
            "fixed_target": {"sfp": {"rail": 0, "port": "sfp_port_0"}},
        }
    }
    samples = sample_training_configs(training_cfg, "sfp", 8, 42)
    assert {(s.target_rail, s.target_port_name) for s in samples} == {(0, "sfp_port_0")}


def test_collection_without_fixed_target_preserves_default_sfp_cycling() -> None:
    """fixed_target가 없으면 collection 블록이 있어도 기존 SFP 순환이 유지돼야 한다."""
    training_cfg = {"collection": {"seed": 42}}
    samples = sample_training_configs(training_cfg, "sfp", 20, 42)
    targets = Counter((s.target_rail, s.target_port_name) for s in samples)
    assert len(targets) == 10, f"10종이 아니라 {len(targets)}종 등장"
    for key, count in targets.items():
        assert count == 2, f"{key} 균등성 위반: {count}회"


def test_fixed_target_does_not_change_seed_sequence() -> None:
    """fixed_target 유무는 per-sample seed sequence를 바꾸면 안 된다."""
    base_cfg = {"collection": {"seed": 42}}
    fixed_cfg = {
        "collection": {
            "seed": 42,
            "fixed_target": {"sfp": {"rail": 0, "port": "sfp_port_0"}},
        }
    }
    base_samples = sample_training_configs(base_cfg, "sfp", 8, 42)
    fixed_samples = sample_training_configs(fixed_cfg, "sfp", 8, 42)
    assert [s.seed for s in fixed_samples] == [s.seed for s in base_samples]


def test_sc_target_cycling_10samples() -> None:
    """10개 SC 샘플 → 각 target 5번씩."""
    samples = sample_training_configs({}, "sc", 10, 42)
    targets = Counter((s.target_rail, s.target_port_name) for s in samples)
    assert targets[(0, "sc_port_0")] == 5
    assert targets[(1, "sc_port_1")] == 5


def test_target_rail_included_in_active_rails() -> None:
    """Target rail은 반드시 활성 rail 목록에 포함된다."""
    for task in ("sfp", "sc"):
        samples = sample_training_configs({}, task, 20, 42)
        for s in samples:
            if task == "sfp":
                assert s.target_rail in s.nic_rails, (
                    f"SFP target rail {s.target_rail} ∉ nic_rails {s.nic_rails}"
                )
            else:
                assert s.target_rail in s.sc_rails, (
                    f"SC target rail {s.target_rail} ∉ sc_rails {s.sc_rails}"
                )


# ---------------------------------------------------------------------------
# Sampler: 범위 / 비복원
# ---------------------------------------------------------------------------


def test_nic_rail_count_range() -> None:
    """NIC rail 개수는 1~5."""
    samples = sample_training_configs({}, "sfp", 50, 42)
    for s in samples:
        n = len(s.nic_rails)
        assert 1 <= n <= 5, f"nic_rails 개수 {n} 범위 밖"
        assert len(set(s.nic_rails)) == n, f"비복원 위반: {s.nic_rails}"
        for r in s.nic_rails:
            assert 0 <= r <= 4, f"rail 번호 {r} 범위 밖"


def test_sc_rail_count_range() -> None:
    samples = sample_training_configs({}, "sc", 20, 42)
    for s in samples:
        n = len(s.sc_rails)
        assert 1 <= n <= 2, f"sc_rails 개수 {n} 범위 밖"
        for r in s.sc_rails:
            assert r in (0, 1), f"rail 번호 {r} 범위 밖"


def test_pose_in_ranges() -> None:
    """모든 pose가 AIC 공식 범위 내."""
    NIC_TR = (-0.0215, 0.0234)
    NIC_YAW = (-0.1745, 0.1745)
    SC_TR = (-0.06, 0.055)
    for task in ("sfp", "sc"):
        samples = sample_training_configs({}, task, 30, 42)
        for s in samples:
            for r, pose in s.nic_poses.items():
                assert NIC_TR[0] <= pose["translation"] <= NIC_TR[1]
                assert NIC_YAW[0] <= pose["yaw"] <= NIC_YAW[1]
            for r, pose in s.sc_poses.items():
                assert SC_TR[0] <= pose["translation"] <= SC_TR[1]
                assert pose["yaw"] == 0.0, f"SC yaw는 0 고정인데 {pose['yaw']}"


def test_gripper_offset_within_range() -> None:
    """Gripper offset은 nominal ± 범위 안."""
    from aic_collector.sampler import GRIPPER_NOMINAL_DEFAULT

    for task in ("sfp", "sc"):
        nom = GRIPPER_NOMINAL_DEFAULT[task]
        samples = sample_training_configs({}, task, 50, 42)
        for s in samples:
            assert abs(s.gripper["x"] - nom["x"]) <= 0.002 + 1e-9
            assert abs(s.gripper["y"] - nom["y"]) <= 0.002 + 1e-9
            assert abs(s.gripper["z"] - nom["z"]) <= 0.002 + 1e-9
            assert abs(s.gripper["roll"]  - nom["roll"])  <= 0.04 + 1e-9
            assert abs(s.gripper["pitch"] - nom["pitch"]) <= 0.04 + 1e-9
            assert abs(s.gripper["yaw"]   - nom["yaw"])   <= 0.04 + 1e-9


# ---------------------------------------------------------------------------
# Builder: config 생성
# ---------------------------------------------------------------------------


def test_build_sfp_config_has_required_fields() -> None:
    samples = sample_training_configs({}, "sfp", 1, 42)
    cfg = build_training_config(samples[0], TEMPLATE_PATH)
    assert "scoring" in cfg and "task_board_limits" in cfg and "robot" in cfg
    trial = cfg["trials"]["trial_1"]
    assert trial["scene"]["task_board"]["pose"]["yaw"] == 3.1415
    # target module name
    tm = trial["tasks"]["task_1"]["target_module_name"]
    assert tm.startswith("nic_card_mount_"), f"SFP target_module {tm} 이상"
    # 활성 rail이 present 여야
    for r in samples[0].nic_rails:
        assert trial["scene"]["task_board"][f"nic_rail_{r}"]["entity_present"] is True
    # 비활성 rail은 entity_present False
    inactive = set(range(5)) - set(samples[0].nic_rails)
    for r in inactive:
        assert trial["scene"]["task_board"][f"nic_rail_{r}"]["entity_present"] is False


def test_build_sc_config_target_module() -> None:
    samples = sample_training_configs({}, "sc", 2, 42)
    for s in samples:
        cfg = build_training_config(s, TEMPLATE_PATH)
        tm = cfg["trials"]["trial_1"]["tasks"]["task_1"]["target_module_name"]
        assert tm == f"sc_port_{s.target_rail}", f"SC target_module {tm} != sc_port_{s.target_rail}"
        assert cfg["trials"]["trial_1"]["scene"]["task_board"]["pose"]["yaw"] == 3.0


# ---------------------------------------------------------------------------
# Builder: 출력 / 번호 관리
# ---------------------------------------------------------------------------


def test_next_config_index_and_write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        assert next_config_index(out, "config_sfp") == 0

        samples = sample_training_configs({}, "sfp", 3, 42, start_index=0)
        written = write_training_configs(samples, out, TEMPLATE_PATH)
        assert len(written) == 3
        assert all(p.exists() for p in written)
        assert next_config_index(out, "config_sfp") == 3

        samples2 = sample_training_configs({}, "sfp", 2, 42, start_index=3)
        written2 = write_training_configs(samples2, out, TEMPLATE_PATH)
        assert [p.name for p in written2] == ["config_sfp_0003.yaml", "config_sfp_0004.yaml"]
        assert next_config_index(out, "config_sfp") == 5


# ---------------------------------------------------------------------------
# 샘플링 전략 (lhs)
# ---------------------------------------------------------------------------


def test_strategy_lhs_reproducibility() -> None:
    """LHS: 동일 seed·count·start_index → 동일 pose 시퀀스."""
    a = sample_training_configs({}, "sfp", 8, 42, strategy="lhs")
    b = sample_training_configs({}, "sfp", 8, 42, strategy="lhs")
    for x, y in zip(a, b):
        assert x.to_dict() == y.to_dict()


def test_strategy_lhs_independence_per_batch() -> None:
    """LHS는 배치 간 독립 — append 배치는 full 결과와 달라야 함 (의도된 동작)."""
    full = sample_training_configs({}, "sfp", 10, 42, strategy="lhs")
    b = sample_training_configs({}, "sfp", 5, 42, strategy="lhs", start_index=5)
    assert any(
        b[i].to_dict() != full[i + 5].to_dict() for i in range(5)
    ), "LHS가 배치 간 독립하지 않음"


def test_strategy_pose_within_ranges() -> None:
    """LHS도 pose 값이 설정된 range 안에 있어야 함."""
    nic_tr_lo, nic_tr_hi = -0.0215, 0.0234
    nic_yaw_lo, nic_yaw_hi = -0.1745, 0.1745
    sc_tr_lo, sc_tr_hi = -0.06, 0.055
    for task in ("sfp", "sc"):
        samples = sample_training_configs({}, task, 20, 42, strategy="lhs")
        for s in samples:
            for p in s.nic_poses.values():
                assert nic_tr_lo <= p["translation"] <= nic_tr_hi
                assert nic_yaw_lo <= p["yaw"] <= nic_yaw_hi
            for p in s.sc_poses.values():
                assert sc_tr_lo <= p["translation"] <= sc_tr_hi


def test_strategy_invalid_raises() -> None:
    try:
        sample_training_configs({}, "sfp", 1, 42, strategy="bogus")
    except ValueError:
        return
    raise AssertionError("bogus strategy에서 ValueError 안 남")


def test_target_cycling_distributes_uniformly_across_10_targets() -> None:
    """1000 SFP samples with target_cycling=True hit every (rail, port) 100 times."""
    cfg = {
        "scene": {
            "nic_count_range": [1, 1],
            "sc_count_range":  [1, 1],
            "target_cycling":  True,
        },
        "ranges": {},
        "param_strategy": "uniform",  # cycling logic is strategy-independent; uniform avoids scipy
    }
    samples = sample_training_configs(cfg, "sfp", count=1000, seed=42, strategy="uniform")
    counts: dict[tuple[int, str], int] = {}
    for s in samples:
        key = (s.target_rail, s.target_port_name)
        counts[key] = counts.get(key, 0) + 1
    assert len(counts) == 10
    assert all(v == 100 for v in counts.values()), counts


def test_lhs_pose_marginal_has_no_empty_bin() -> None:
    """LHS guarantees every dimension's 10-bin histogram is non-empty for n>=10."""
    import pytest

    try:
        from scipy.stats import qmc  # noqa: F401
    except ImportError as exc:
        pytest.skip(f"scipy.stats.qmc unavailable: {exc}")
    except ValueError as exc:
        msg = str(exc).lower()
        if "_ckdtree" in msg or "numpy" in msg or "dtype size changed" in msg:
            pytest.skip(f"scipy/numpy ABI mismatch: {exc}")
        raise
    cfg = {
        "scene": {
            "nic_count_range": [1, 1],
            "sc_count_range":  [1, 1],
            "target_cycling":  True,
        },
        "ranges": {
            "nic_translation": [-0.0215, 0.0234],
            "nic_yaw":         [-0.1745, 0.1745],
            "sc_translation":  [-0.06, 0.055],
            "gripper_xy": 0.002,
            "gripper_z":  0.002,
            "gripper_rpy": 0.04,
        },
        "param_strategy": "lhs",
    }
    samples = sample_training_configs(cfg, "sfp", count=100, seed=42, strategy="lhs")
    # Pull nic translation from the active rail of each sample.
    nic_values = [next(iter(s.nic_poses.values()))["translation"] for s in samples]
    bins = [0] * 10
    lo, hi = -0.0215, 0.0234
    for v in nic_values:
        # clamp to [lo, hi) defensively, then bucket into 10 bins.
        idx = min(9, max(0, int((v - lo) / (hi - lo) * 10)))
        bins[idx] += 1
    assert all(b > 0 for b in bins), bins


def test_fixed_target_legacy_path_unchanged_regression() -> None:
    """Sanity: fixed_target still collapses to a single (rail, port)."""
    cfg = {
        "scene": {
            "nic_count_range": [1, 1],
            "sc_count_range":  [1, 1],
            "target_cycling":  False,
        },
        "ranges": {},
        "collection": {"fixed_target": {"sfp": {"rail": 2, "port": "sfp_port_1"}}},
        "param_strategy": "uniform",
    }
    samples = sample_training_configs(cfg, "sfp", count=20, seed=42, strategy="uniform")
    assert all(s.target_rail == 2 and s.target_port_name == "sfp_port_1" for s in samples)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    tests = [
        ("재현성: 동일 seed", test_reproducibility_same_seed),
        ("재현성: 다른 seed → 다른 출력", test_different_seeds_differ),
        ("재현성: start_index 연속성", test_start_index_continuity),
        ("순환: SFP 20샘플 균등", test_sfp_target_cycling_20samples),
        ("순환: fixed SFP target", test_fixed_target_sfp_forces_same_target),
        ("순환: collection without fixed target", test_collection_without_fixed_target_preserves_default_sfp_cycling),
        ("순환: fixed target seed sequence", test_fixed_target_does_not_change_seed_sequence),
        ("순환: SC 10샘플 균등", test_sc_target_cycling_10samples),
        ("순환: target rail 포함 보장", test_target_rail_included_in_active_rails),
        ("범위: NIC rail 개수/비복원", test_nic_rail_count_range),
        ("범위: SC rail 개수/비복원", test_sc_rail_count_range),
        ("범위: pose 범위", test_pose_in_ranges),
        ("범위: gripper offset", test_gripper_offset_within_range),
        ("빌더: SFP config 필드", test_build_sfp_config_has_required_fields),
        ("빌더: SC target module", test_build_sc_config_target_module),
        ("빌더: next_index + write", test_next_config_index_and_write),
        ("전략: LHS 재현성", test_strategy_lhs_reproducibility),
        ("전략: LHS 배치 간 독립", test_strategy_lhs_independence_per_batch),
        ("전략: pose 범위 준수", test_strategy_pose_within_ranges),
        ("전략: 잘못된 이름 ValueError", test_strategy_invalid_raises),
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
