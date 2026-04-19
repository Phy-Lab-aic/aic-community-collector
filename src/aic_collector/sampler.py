#!/usr/bin/env python3
"""
파라미터 샘플링 유틸.

두 가지 모드를 지원한다:

1. Sweep 모드 (`sample_parameters`)
   - 입력: 파라미터 범위 dict + 전략(uniform/lhs/sobol/static) + runs/seed
   - 출력: [{"nic0_translation": 0.01, ...}, ...] (각 dict = 한 run)

2. Training 모드 (`sample_training_configs`)
   - 입력: training 섹션 dict + task_type("sfp"|"sc") + count/seed
   - 출력: List[TrainingSample] — 각 항목은 NIC/SC rail 선택, pose, target, gripper
   - 결정적 순환(target cycling)으로 SFP 10종 / SC 2종 균등 분포 보장

재현성:
  동일 seed + 동일 입력 → 동일 출력 보장 (단위 테스트로 확인)

Usage (CLI, sweep 모드):
    python sampler.py --strategy lhs --runs 10 --seed 42 \\
        --config configs/e2e_default.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    import numpy as np
except ImportError:
    sys.stderr.write("numpy 필요: pip install numpy\n")
    sys.exit(1)

try:
    import yaml
except ImportError:
    sys.stderr.write("pyyaml 필요: pip install pyyaml\n")
    sys.exit(1)

if TYPE_CHECKING:
    from aic_collector.scene_plan import ScenePlan


# ---------------------------------------------------------------------------
# AIC 기본값 — task_board_description.md 공식 범위 내로 클램핑
# 참고: sample_config.yaml의 nic translation(0.036)은 공식 문서 범위
#       [-0.0215, 0.0234]를 초과하므로, 문서 기준 최대값으로 보정.
# ---------------------------------------------------------------------------

AIC_DEFAULT_PARAMS: dict[str, float] = {
    "nic0_translation": 0.0234,   # trial_1 nic_rail_0 (공식 max)
    "nic0_yaw": 0.0,
    "nic1_translation": 0.0234,   # trial_2 nic_rail_1 (공식 max)
    "nic1_yaw": 0.0,
    "sc0_translation": 0.042,     # sc_rail_0 (trial 1/2 배경)
    "sc0_yaw": 0.1,
    "sc1_translation": -0.055,    # trial_3 sc_rail_1
    "sc1_yaw": 0.0,
}


# ---------------------------------------------------------------------------
# 샘플링 전략
# ---------------------------------------------------------------------------


def sample_uniform(
    bounds: list[tuple[float, float]],
    runs: int,
    seed: int,
) -> np.ndarray:
    """독립 uniform random (EXP-007 baseline).

    Args:
        bounds: 차원별 (min, max) 튜플 리스트
        runs: 샘플 수
        seed: 재현용 seed

    Returns:
        shape=(runs, len(bounds)) 실수 배열
    """
    rng = np.random.default_rng(seed)
    n_dims = len(bounds)
    out = np.empty((runs, n_dims), dtype=np.float64)
    for d, (lo, hi) in enumerate(bounds):
        out[:, d] = rng.uniform(lo, hi, size=runs)
    return out


def sample_lhs(
    bounds: list[tuple[float, float]],
    runs: int,
    seed: int,
) -> np.ndarray:
    """Latin Hypercube Sampling (층화 샘플링).

    각 차원을 runs개의 균등 구간으로 나누고 한 구간당 하나씩 샘플.
    권장 기본 전략 — F4-a.
    """
    try:
        from scipy.stats import qmc
    except ImportError:
        raise ImportError(
            "LHS는 scipy 필요: pip install scipy. "
            "또는 --strategy uniform 사용"
        )
    n_dims = len(bounds)
    sampler = qmc.LatinHypercube(d=n_dims, seed=seed)
    unit = sampler.random(n=runs)  # shape=(runs, n_dims), [0, 1]^n_dims
    lo = np.array([b[0] for b in bounds])
    hi = np.array([b[1] for b in bounds])
    return lo + unit * (hi - lo)


def sample_sobol(
    bounds: list[tuple[float, float]],
    runs: int,
    seed: int,
) -> np.ndarray:
    """Sobol 저불일치 시퀀스.

    고차원에서 uniform보다 균등한 커버리지. runs는 2의 거듭제곱 권장.
    """
    try:
        from scipy.stats import qmc
    except ImportError:
        raise ImportError("Sobol은 scipy 필요: pip install scipy")
    n_dims = len(bounds)
    sampler = qmc.Sobol(d=n_dims, scramble=True, seed=seed)
    unit = sampler.random(n=runs)
    lo = np.array([b[0] for b in bounds])
    hi = np.array([b[1] for b in bounds])
    return lo + unit * (hi - lo)


STRATEGIES = {
    "uniform": sample_uniform,
    "lhs": sample_lhs,
    "sobol": sample_sobol,
}


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def build_bounds(params_cfg: dict[str, dict]) -> tuple[list[str], list[tuple[float, float]]]:
    """config의 parameters 섹션을 키 리스트 + bounds 리스트로 변환.

    dict 순서를 유지 (파이썬 3.7+ 보장).
    """
    keys = list(params_cfg.keys())
    bounds: list[tuple[float, float]] = []
    for k in keys:
        entry = params_cfg[k]
        if not isinstance(entry, dict) or "min" not in entry or "max" not in entry:
            raise ValueError(f"parameters.{k}은 {{min, max}} 형식이어야 합니다")
        lo, hi = float(entry["min"]), float(entry["max"])
        if lo >= hi:
            raise ValueError(f"parameters.{k}: min({lo}) >= max({hi})")
        bounds.append((lo, hi))
    return keys, bounds


def sample_parameters(
    params_cfg: dict[str, dict],
    strategy: str,
    runs: int,
    seed: int,
) -> list[dict[str, float]]:
    """파라미터 dict을 runs개의 샘플로 변환.

    Args:
        params_cfg: e2e_default.yaml의 `parameters` 섹션
        strategy: "static" | "uniform" | "lhs" | "sobol"
        runs: 생성할 샘플 수
        seed: 재현용 seed

    Returns:
        List of dicts, 각 dict는 {파라미터 이름: 값}
    """
    # static 전략: AIC 공식 고정값을 runs번 복제 (bounds 무시)
    if strategy == "static":
        keys = list(params_cfg.keys())
        sample = {}
        for k in keys:
            if k in AIC_DEFAULT_PARAMS:
                sample[k] = round(AIC_DEFAULT_PARAMS[k], 4)
            else:
                # AIC 기본값에 없는 커스텀 파라미터는 0으로
                sample[k] = 0.0
        return [dict(sample) for _ in range(runs)]

    if strategy not in STRATEGIES:
        raise ValueError(
            f"알 수 없는 샘플링 전략: {strategy}. "
            f"사용 가능: static, {list(STRATEGIES.keys())}"
        )
    keys, bounds = build_bounds(params_cfg)
    arr = STRATEGIES[strategy](bounds, runs, seed)
    return [
        {k: round(float(arr[i, d]), 4) for d, k in enumerate(keys)}
        for i in range(runs)
    ]


# ---------------------------------------------------------------------------
# Training 샘플러 (학습 데이터 수집용)
# ---------------------------------------------------------------------------
#
# Sweep과 달리 각 샘플이 하나의 완전한 scene을 서술한다:
#   - NIC card 1~5개 (rail 0~4 중 비복원)
#   - SC port 1~2개 (rail 0,1 중)
#   - Target은 결정적 순환 (SFP 10종 / SC 2종 균등)
#   - Gripper offset은 nominal ± 범위로 랜덤
#
# Target cycling 보장: sample_index로 target이 결정되고,
# 해당 target rail은 활성 rail 목록에 강제로 포함된다.


SFP_TARGET_CYCLE: list[tuple[int, str]] = [
    (rail, port)
    for rail in range(5)
    for port in ("sfp_port_0", "sfp_port_1")
]
"""SFP 10종 target 순환 (5 rail × 2 port)."""

SC_TARGET_CYCLE: list[tuple[int, str]] = [
    (0, "sc_port_0"),
    (1, "sc_port_1"),
]
"""SC 2종 target 순환."""


GRIPPER_NOMINAL_DEFAULT: dict[str, dict[str, float]] = {
    "sfp": {
        "x": 0.0, "y": 0.015385, "z": 0.04245,
        "roll": 0.4432, "pitch": -0.4838, "yaw": 1.3303,
    },
    "sc": {
        "x": 0.0, "y": 0.015385, "z": 0.04045,
        "roll": 0.4432, "pitch": -0.4838, "yaw": 1.3303,
    },
}
"""Gripper nominal 값 (task_board_description.md 기준)."""


RANGES_DEFAULT: dict[str, Any] = {
    "nic_translation": (-0.0215, 0.0234),
    "nic_yaw":         (-0.1745, 0.1745),
    "sc_translation":  (-0.06,   0.055),
    "gripper_xy":      0.002,
    "gripper_z":       0.002,
    "gripper_rpy":     0.04,
}


@dataclass
class TrainingSample:
    """Training scene 한 개의 완전한 서술.

    build_training_config(sample)이 이 값만 가지고 엔진 config YAML을 생성한다.
    """

    task_type: str                    # "sfp" | "sc"
    sample_index: int                 # 0-based, target cycling 인덱스
    seed: int                         # 이 샘플 생성 per-sample seed

    nic_rails: list[int] = field(default_factory=list)
    """활성 NIC rail 번호 목록 (오름차순). 길이 1~5."""

    nic_poses: dict[int, dict[str, float]] = field(default_factory=dict)
    """{rail_idx: {translation, yaw}} — nic_rails 각각에 대응."""

    sc_rails: list[int] = field(default_factory=list)
    """활성 SC rail 번호 목록. 길이 1~2."""

    sc_poses: dict[int, dict[str, float]] = field(default_factory=dict)
    """{rail_idx: {translation, yaw}} — sc_poses[r]['yaw']는 항상 0."""

    target_rail: int = 0
    """타겟 rail 번호 (SFP: 0~4 / SC: 0~1). 반드시 활성 목록에 포함됨."""

    target_port_name: str = ""
    """타겟 port 이름 — 엔진의 tasks.task_1.port_name에 주입."""

    gripper: dict[str, float] = field(default_factory=dict)
    """{x, y, z, roll, pitch, yaw} — nominal ± 랜덤."""

    def to_dict(self) -> dict:
        """JSON 직렬화용 dict 변환 (int 키 → str)."""
        d = asdict(self)
        d["nic_poses"] = {str(k): v for k, v in self.nic_poses.items()}
        d["sc_poses"] = {str(k): v for k, v in self.sc_poses.items()}
        return d


def _resolve_range(cfg_ranges: dict, key: str) -> tuple[float, float]:
    """cfg.training.ranges에서 (lo, hi)를 읽어오거나 기본값 반환."""
    v = cfg_ranges.get(key, RANGES_DEFAULT[key])
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return float(v[0]), float(v[1])
    raise ValueError(f"ranges.{key}은 [min, max] 형식이어야 합니다 (받음: {v!r})")


def _resolve_scalar(cfg_ranges: dict, key: str) -> float:
    v = cfg_ranges.get(key, RANGES_DEFAULT[key])
    return float(v)


# 설계행렬(LHS) 컬럼 매핑 — pose 값에 주입되는 18차원 [0, 1]^18.
#   cols  0..4  : NIC rail_0..4 translation
#   cols  5..9  : NIC rail_0..4 yaw
#   cols 10..11 : SC  rail_0..1 translation
#   cols 12..17 : gripper (x, y, z, roll, pitch, yaw)
# 미활성 rail에 해당하는 컬럼은 무시 (LHS 층화 특성은 유지).
_POSE_DESIGN_DIMS = 18


def _generate_pose_design(
    strategy: str,
    count: int,
    seed: int,
    start_index: int,
) -> np.ndarray:
    """(count, 18) 설계행렬 생성 — [0, 1]^18.

    - "lhs": 배치마다 새로 층화 샘플링. start_index를 seed에 섞어
             배치 간 독립 재추첨 (LHS는 확장 불가).
    """
    try:
        from scipy.stats import qmc
    except ImportError as e:
        raise ImportError(
            f"{strategy}는 scipy 필요: uv add scipy. 또는 strategy='uniform' 사용."
        ) from e

    if strategy == "lhs":
        batch_seed = int(np.uint64(seed) ^ np.uint64(start_index * 0x9E3779B97F4A7C15 & 0xFFFFFFFFFFFFFFFF))
        sampler = qmc.LatinHypercube(d=_POSE_DESIGN_DIMS, seed=batch_seed)
        return sampler.random(n=count)
    raise ValueError(f"pose 설계행렬 미지원 strategy: {strategy!r}")


def sample_training_configs(
    training_cfg: dict,
    task_type: str,
    count: int,
    seed: int,
    start_index: int = 0,
    strategy: str = "uniform",
) -> list[TrainingSample]:
    """Training용 scene 샘플을 count개 생성.

    Args:
        training_cfg: e2e config의 `training` 섹션 (scene, ranges, gripper_nominal 포함)
        task_type: "sfp" | "sc"
        count: 생성할 샘플 수
        seed: 재현용 base seed. per-sample seed는 `seed + start_index + i`로 파생.
        start_index: append 모드에서 기존 번호 다음부터 이어서 생성할 때의 시작 인덱스.
                     target cycling과 seed 파생에 영향.
        strategy: pose 값 샘플링 전략.
                  - "uniform" (기본): 각 샘플마다 독립 uniform.
                  - "lhs":     Latin Hypercube. 공간 채움 좋음, 샘플 수 적을 때 유리.
                               배치(append) 간 독립 재추첨.

    Returns:
        List[TrainingSample] — 길이 count
    """
    if task_type not in ("sfp", "sc"):
        raise ValueError(f"task_type은 'sfp' 또는 'sc'여야 합니다 (받음: {task_type!r})")
    if count < 0:
        raise ValueError(f"count는 0 이상이어야 합니다 (받음: {count})")
    if strategy not in ("uniform", "lhs"):
        raise ValueError(
            f"strategy는 'uniform' | 'lhs' 중 하나 (받음: {strategy!r})"
        )

    scene_cfg = training_cfg.get("scene", {}) or {}
    ranges_cfg = training_cfg.get("ranges", {}) or {}
    gripper_nominal_cfg = training_cfg.get("gripper_nominal", {}) or GRIPPER_NOMINAL_DEFAULT
    collection_cfg = training_cfg.get("collection", {}) or {}
    nominal = gripper_nominal_cfg.get(task_type, GRIPPER_NOMINAL_DEFAULT[task_type])

    nic_count_range = scene_cfg.get("nic_count_range", [1, 5])
    sc_count_range = scene_cfg.get("sc_count_range", [1, 2])
    target_cycling = scene_cfg.get("target_cycling", True)

    nic_tr = _resolve_range(ranges_cfg, "nic_translation")
    nic_yaw_r = _resolve_range(ranges_cfg, "nic_yaw")
    sc_tr = _resolve_range(ranges_cfg, "sc_translation")
    g_xy = _resolve_scalar(ranges_cfg, "gripper_xy")
    g_z = _resolve_scalar(ranges_cfg, "gripper_z")
    g_rpy = _resolve_scalar(ranges_cfg, "gripper_rpy")

    fixed_target = (collection_cfg.get("fixed_target", {}) or {}).get(task_type)
    cycle = (
        [(int(fixed_target["rail"]), str(fixed_target["port"]))]
        if fixed_target is not None
        else (SFP_TARGET_CYCLE if task_type == "sfp" else SC_TARGET_CYCLE)
    )
    # pose 설계행렬 (lhs 전용). uniform은 per-sample rng로 그때그때 추첨.
    design: np.ndarray | None = None
    if strategy == "lhs" and count > 0:
        design = _generate_pose_design(strategy, count, seed, start_index)

    def _design_map(i: int, col: int, lo: float, hi: float) -> float:
        """설계행렬 [0,1] 값을 [lo, hi]로 affine."""
        return float(lo + design[i, col] * (hi - lo))  # type: ignore[index]

    samples: list[TrainingSample] = []
    for i in range(count):
        global_index = start_index + i
        per_seed = seed + global_index
        rng = np.random.default_rng(per_seed)

        # 1) Target (결정적 순환)
        if target_cycling:
            target_rail, target_port = cycle[global_index % len(cycle)]
        else:
            idx = int(rng.integers(0, len(cycle)))
            target_rail, target_port = cycle[idx]

        # 2) NIC 개수/선택 (task가 sfp면 target rail 포함 필수)
        n_nic_lo, n_nic_hi = int(nic_count_range[0]), int(nic_count_range[1])
        n_nic = int(rng.integers(n_nic_lo, n_nic_hi + 1))
        all_nic = list(range(5))
        if task_type == "sfp":
            others = [r for r in all_nic if r != target_rail]
            others_pick = rng.choice(len(others), size=max(0, n_nic - 1), replace=False)
            selected_nic = sorted([target_rail] + [others[i] for i in others_pick])
        else:
            pick = rng.choice(5, size=n_nic, replace=False)
            selected_nic = sorted(int(r) for r in pick)

        # 3) SC 개수/선택 (task가 sc면 target rail 포함)
        n_sc_lo, n_sc_hi = int(sc_count_range[0]), int(sc_count_range[1])
        n_sc = int(rng.integers(n_sc_lo, n_sc_hi + 1))
        all_sc = [0, 1]
        if task_type == "sc":
            others = [r for r in all_sc if r != target_rail]
            if n_sc == 1:
                selected_sc = [target_rail]
            else:
                selected_sc = sorted([target_rail] + others)
        else:
            if n_sc >= 2:
                selected_sc = [0, 1]
            else:
                selected_sc = [int(rng.integers(0, 2))]

        # 4) Pose 샘플링 — uniform: per-sample rng / lhs: 설계행렬 룩업
        nic_poses: dict[int, dict[str, float]] = {}
        sc_poses: dict[int, dict[str, float]] = {}
        if design is None:
            for r in selected_nic:
                nic_poses[r] = {
                    "translation": round(float(rng.uniform(*nic_tr)), 4),
                    "yaw":         round(float(rng.uniform(*nic_yaw_r)), 4),
                }
            for r in selected_sc:
                sc_poses[r] = {
                    "translation": round(float(rng.uniform(*sc_tr)), 4),
                    "yaw":         0.0,
                }
            # 5) Gripper offset (nominal ± 범위)
            gripper = {
                "x":     round(nominal["x"]     + float(rng.uniform(-g_xy, g_xy)), 6),
                "y":     round(nominal["y"]     + float(rng.uniform(-g_xy, g_xy)), 6),
                "z":     round(nominal["z"]     + float(rng.uniform(-g_z,  g_z)),  6),
                "roll":  round(nominal["roll"]  + float(rng.uniform(-g_rpy, g_rpy)), 6),
                "pitch": round(nominal["pitch"] + float(rng.uniform(-g_rpy, g_rpy)), 6),
                "yaw":   round(nominal["yaw"]   + float(rng.uniform(-g_rpy, g_rpy)), 6),
            }
        else:
            for r in selected_nic:
                nic_poses[r] = {
                    "translation": round(_design_map(i, r,     nic_tr[0],    nic_tr[1]),    4),
                    "yaw":         round(_design_map(i, 5 + r, nic_yaw_r[0], nic_yaw_r[1]), 4),
                }
            for r in selected_sc:
                sc_poses[r] = {
                    "translation": round(_design_map(i, 10 + r, sc_tr[0], sc_tr[1]), 4),
                    "yaw":         0.0,
                }
            gripper = {
                "x":     round(nominal["x"]     + _design_map(i, 12, -g_xy,  g_xy),  6),
                "y":     round(nominal["y"]     + _design_map(i, 13, -g_xy,  g_xy),  6),
                "z":     round(nominal["z"]     + _design_map(i, 14, -g_z,   g_z),   6),
                "roll":  round(nominal["roll"]  + _design_map(i, 15, -g_rpy, g_rpy), 6),
                "pitch": round(nominal["pitch"] + _design_map(i, 16, -g_rpy, g_rpy), 6),
                "yaw":   round(nominal["yaw"]   + _design_map(i, 17, -g_rpy, g_rpy), 6),
            }

        samples.append(TrainingSample(
            task_type=task_type,
            sample_index=global_index,
            seed=per_seed,
            nic_rails=selected_nic,
            nic_poses=nic_poses,
            sc_rails=selected_sc,
            sc_poses=sc_poses,
            target_rail=int(target_rail),
            target_port_name=target_port,
            gripper=gripper,
        ))

    return samples


# ---------------------------------------------------------------------------
# 통합 샘플러 — ScenePlan 기반 (Phase 1)
# ---------------------------------------------------------------------------
#
# 기존 sample_training_configs/sample_parameters는 wrapper로 유지하고,
# sample_scenes()가 미래의 단일 진입점. Phase 1에서는 trials_per_config=1만
# 지원하며, =3은 Phase 2+에서 추가된다.


def training_sample_to_scene_plan(s: TrainingSample) -> ScenePlan:
    """TrainingSample → ScenePlan (1-trial) 변환.

    내부에서 scene_plan을 import (순환 import 회피용 lazy import 불필요 —
    scene_plan은 sampler에 의존하지 않음).
    """
    from aic_collector.scene_plan import ScenePlan, TrialPlan

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
    return ScenePlan(
        sample_index=s.sample_index,
        seed=s.seed,
        trials=[trial],
    )


def sample_scenes(
    cfg: dict,
    task_type: str,
    count: int,
    seed: int,
    start_index: int = 0,
    trials_per_config: int = 1,
) -> list[ScenePlan]:
    """통합 샘플링 — ScenePlan 리스트 반환.

    Phase 1: trials_per_config=1만 지원. 내부적으로 sample_training_configs를
             호출한 뒤 TrainingSample → ScenePlan으로 변환.

    Args:
        cfg: e2e config 전체 dict. `training` 섹션만 현재 사용.
        task_type: "sfp" | "sc"
        count: 생성할 샘플 수
        seed: 재현용 base seed
        start_index: append 모드에서 이어서 생성할 시작 인덱스
        trials_per_config: 1(기본) / 3은 Phase 2+

    Returns:
        List[ScenePlan] — 길이 count. 각 ScenePlan은 길이 1의 trials.
    """
    if trials_per_config != 1:
        raise NotImplementedError(
            f"trials_per_config={trials_per_config}은 Phase 2+에서 지원. "
            "Phase 1은 1만 가능."
        )

    training_cfg = cfg.get("training", {}) if isinstance(cfg, dict) else {}
    strategy = str(training_cfg.get("param_strategy", "uniform"))
    samples = sample_training_configs(
        training_cfg, task_type, count, seed,
        start_index=start_index, strategy=strategy,
    )
    return [training_sample_to_scene_plan(s) for s in samples]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="e2e config 파일 경로")
    parser.add_argument("--strategy", default=None, help="config의 sampling.strategy 오버라이드")
    parser.add_argument("--runs", type=int, default=None, help="collection.runs 오버라이드")
    parser.add_argument("--seed", type=int, default=None, help="collection.seed 오버라이드")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="JSON을 들여쓰기해서 출력 (기본: 한 줄)",
    )
    args = parser.parse_args()

    if not args.config.exists():
        sys.stderr.write(f"[error] config 없음: {args.config}\n")
        return 1

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    strategy = args.strategy or cfg.get("sampling", {}).get("strategy", "uniform")
    runs = args.runs if args.runs is not None else cfg.get("collection", {}).get("runs", 10)
    seed = args.seed if args.seed is not None else cfg.get("collection", {}).get("seed", 42)
    params_cfg = cfg.get("parameters", {})

    if not params_cfg:
        sys.stderr.write("[error] config에 parameters 섹션이 없습니다\n")
        return 1

    try:
        samples = sample_parameters(params_cfg, strategy, runs, seed)
    except Exception as e:
        sys.stderr.write(f"[error] 샘플링 실패: {e}\n")
        return 1

    if args.pretty:
        print(json.dumps(samples, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(samples, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
