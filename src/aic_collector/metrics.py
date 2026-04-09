#!/usr/bin/env python3
"""
EXP-009 Primary 지표 계산 유틸.

지표:
  P1 — Episodes per hour (유효 성공): 성공 episode / 전체 wall-clock 시간(h)
  P2 — 평균 trial 실행 시간(초): metadata.json의 trial_duration_sec 평균
  P3 — 파라미터 공간 L2 star discrepancy: scipy.stats.qmc

보조:
  - 수집 성공률
  - 축별 히스토그램 변동 계수 (CV)

Usage:
    # 기본 EXP-007 경로 스캔
    python metrics.py

    # 특정 데모/bag 경로 지정
    python metrics.py \\
        --demo-dir ~/aic_community_demos_compressed \\
        --bag-dir ~/aic_community_bags_compressed \\
        --label baseline_20260408

    # Primary 지표만 (집계 표)
    python metrics.py --summary-only
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.stderr.write("pyyaml 필요: pip install pyyaml\n")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    sys.stderr.write("numpy 필요: pip install numpy\n")
    sys.exit(1)


# 파라미터 키 → (min, max) — L2 discrepancy 정규화용
PARAM_RANGES: dict[str, tuple[float, float]] = {
    "NIC0_TRANSLATION": (-0.0215, 0.0234),
    "NIC0_YAW": (-0.1745, 0.1745),
    "NIC1_TRANSLATION": (-0.0215, 0.0234),
    "NIC1_YAW": (-0.1745, 0.1745),
    "SC0_TRANSLATION": (-0.06, 0.055),
    "SC0_YAW": (-0.1745, 0.1745),
    "SC1_TRANSLATION": (-0.06, 0.055),
    "SC1_YAW": (-0.1745, 0.1745),
}


# ---------------------------------------------------------------------------
# 수집 데이터 스캔
# ---------------------------------------------------------------------------


def scan_episodes(demo_dir: Path) -> list[dict[str, Any]]:
    """episode_*/metadata.json 전부 로드."""
    eps = []
    if not demo_dir.exists():
        return eps
    for ep in sorted(demo_dir.glob("episode_*")):
        meta = ep / "metadata.json"
        if not meta.exists():
            continue
        try:
            with open(meta) as f:
                d = json.load(f)
            d["_path"] = str(ep)
            eps.append(d)
        except Exception as e:
            sys.stderr.write(f"[warn] skip {meta}: {e}\n")
    return eps


def scan_run_configs(bag_dir: Path) -> list[dict[str, float]]:
    """
    bag_dir/run_*/config.yaml을 스캔해 파라미터 값을 추출.

    config는 템플릿이 치환된 상태라 yaml.safe_load로 읽어 구조 탐색.
    파라미터는 다음 경로에 존재:
        trials.trial_1.scene.task_board.nic_rail_0.entity_pose.translation
        trials.trial_1.scene.task_board.nic_rail_0.entity_pose.yaw
        trials.trial_2.scene.task_board.nic_rail_1.entity_pose.translation / yaw
        trials.trial_1.scene.task_board.sc_rail_0.entity_pose.translation / yaw
        trials.trial_3.scene.task_board.sc_rail_1.entity_pose.translation / yaw
    """
    samples = []
    if not bag_dir.exists():
        return samples
    for run in sorted(bag_dir.glob("run_*")):
        cfg_path = run / "config.yaml"
        if not cfg_path.exists():
            continue
        try:
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f)
        except Exception as e:
            sys.stderr.write(f"[warn] skip {cfg_path}: {e}\n")
            continue

        trials = cfg.get("trials", {}) if cfg else {}
        sample: dict[str, float] = {}

        def pose(trial_key: str, rail_key: str):
            try:
                return trials[trial_key]["scene"]["task_board"][rail_key]["entity_pose"]
            except (KeyError, TypeError):
                return None

        for trial_key, rail_key, out_prefix in [
            ("trial_1", "nic_rail_0", "NIC0"),
            ("trial_2", "nic_rail_1", "NIC1"),
            ("trial_1", "sc_rail_0", "SC0"),
            ("trial_3", "sc_rail_1", "SC1"),
        ]:
            p = pose(trial_key, rail_key)
            if p is None:
                continue
            t = p.get("translation")
            y = p.get("yaw")
            if isinstance(t, (int, float)):
                sample[f"{out_prefix}_TRANSLATION"] = float(t)
            if isinstance(y, (int, float)):
                sample[f"{out_prefix}_YAW"] = float(y)
        if sample:
            sample["_run"] = str(run)
            samples.append(sample)
    return samples


def parse_run_wallclock(bag_dir: Path) -> tuple[float, int]:
    """
    run_*/bag_trial_*/ 디렉토리명에 기록된 timestamp를 활용해
    run별 시작~끝 시간 근사. 정확한 시작 시간은 run 디렉토리명 `run_N_YYYYMMDD_HHMMSS` 사용.

    Returns:
        (total_wallclock_hours, num_runs)
    """
    total_seconds = 0.0
    n_runs = 0
    pat = re.compile(r"run_\d+_(\d{8}_\d{6})$")
    for run in sorted(bag_dir.glob("run_*")):
        if not run.is_dir():
            continue
        m = pat.search(run.name)
        if not m:
            continue
        # run 하나당 wall-clock은 제작 시각~마지막 bag_trial 시각 차 + 여유.
        # 간단히: run 디렉토리 최신 mtime - run 디렉토리 생성 시각(디렉토리명)
        try:
            from datetime import datetime
            start = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").timestamp()
            latest_mtime = max(
                (os.path.getmtime(str(p)) for p in run.rglob("*") if p.is_file()),
                default=start,
            )
            total_seconds += max(0.0, latest_mtime - start)
            n_runs += 1
        except Exception:
            continue
    return total_seconds / 3600.0, n_runs


# ---------------------------------------------------------------------------
# 지표 계산
# ---------------------------------------------------------------------------


def compute_p1_episodes_per_hour(episodes: list[dict], total_hours: float) -> float | None:
    if total_hours <= 0:
        return None
    success_count = sum(1 for e in episodes if e.get("success") is True)
    return success_count / total_hours


def compute_p2_mean_trial_time(episodes: list[dict]) -> tuple[float | None, int]:
    """
    metadata.json의 trial_duration_sec이 있으면 우선 사용.
    없으면 duration_sec(첫 step~마지막 step)로 폴백.
    """
    values = []
    for e in episodes:
        v = e.get("trial_duration_sec")
        if v is None:
            v = e.get("duration_sec")
        if isinstance(v, (int, float)) and v > 0:
            values.append(float(v))
    if not values:
        return None, 0
    return sum(values) / len(values), len(values)


def normalize_samples(samples: list[dict[str, float]]) -> tuple[np.ndarray, list[str]]:
    """
    샘플 dict 리스트를 [0, 1] 정규화된 2D array로 변환.
    PARAM_RANGES에 정의된 키 순서를 기준으로 각 차원을 정규화.

    Returns:
        (array shape=(n_samples, n_dims), dim_keys)
    """
    dim_keys = [k for k in PARAM_RANGES.keys()]
    rows = []
    for s in samples:
        row = []
        for k in dim_keys:
            lo, hi = PARAM_RANGES[k]
            v = s.get(k)
            if v is None:
                row.append(math.nan)
                continue
            row.append((v - lo) / (hi - lo))
        rows.append(row)
    arr = np.array(rows, dtype=np.float64)
    return arr, dim_keys


def compute_p3_l2_discrepancy(arr: np.ndarray) -> float | None:
    """
    scipy.stats.qmc.discrepancy with method='L2-star'.
    NaN 있는 행은 제거.
    """
    try:
        from scipy.stats import qmc
    except ImportError:
        sys.stderr.write("[warn] scipy 미설치 — P3 계산 생략 (pip install scipy)\n")
        return None
    if arr.size == 0:
        return None
    mask = ~np.isnan(arr).any(axis=1)
    clean = arr[mask]
    if clean.shape[0] < 2:
        return None
    # clip to [0, 1] in case of slight out-of-range values
    clean = np.clip(clean, 0.0, 1.0)
    try:
        return float(qmc.discrepancy(clean, method="L2-star"))
    except Exception as e:
        sys.stderr.write(f"[warn] discrepancy 계산 실패: {e}\n")
        return None


def compute_axis_cv(arr: np.ndarray, bins: int = 10) -> list[float]:
    """각 축별 히스토그램의 변동 계수 (CV = σ/μ). 낮을수록 균등."""
    cvs = []
    for d in range(arr.shape[1]):
        col = arr[:, d]
        col = col[~np.isnan(col)]
        if col.size == 0:
            cvs.append(float("nan"))
            continue
        col = np.clip(col, 0.0, 1.0)
        counts, _ = np.histogram(col, bins=bins, range=(0.0, 1.0))
        mean = counts.mean()
        std = counts.std()
        cvs.append(float(std / mean) if mean > 0 else float("nan"))
    return cvs


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------


def print_summary(
    label: str,
    episodes: list[dict],
    samples: list[dict],
    total_hours: float,
    n_runs: int,
) -> None:
    print(f"\n=== EXP-009 Metrics: {label} ===")
    print(f"demo episodes: {len(episodes)}")
    print(f"run configs:   {len(samples)}")
    print(f"total wall-clock (h): {total_hours:.3f} ({n_runs} runs)")

    # Primary
    p1 = compute_p1_episodes_per_hour(episodes, total_hours)
    p2, n_p2 = compute_p2_mean_trial_time(episodes)
    arr, dim_keys = normalize_samples(samples)
    p3 = compute_p3_l2_discrepancy(arr)

    success = sum(1 for e in episodes if e.get("success") is True)
    failed = sum(1 for e in episodes if e.get("success") is False)
    success_rate = (success / len(episodes)) if episodes else 0.0

    print("")
    print("Primary:")
    print(f"  P1 Episodes per hour (유효 성공): {p1:.2f}" if p1 is not None else "  P1: N/A")
    print(f"  P2 평균 trial 실행 시간 (초): {p2:.2f}  (n={n_p2})" if p2 is not None else "  P2: N/A")
    print(f"  P3 L2 star discrepancy: {p3:.6f}" if p3 is not None else "  P3: N/A (scipy 없음 또는 샘플 부족)")

    print("")
    print("Secondary:")
    print(f"  수집 성공률: {success_rate*100:.1f}%  (success={success}, failed={failed})")
    print(f"  파라미터 샘플 수: {arr.shape[0]}  dims={arr.shape[1]}")
    cvs = compute_axis_cv(arr)
    print("  축별 히스토그램 CV (낮을수록 균등):")
    for k, c in zip(dim_keys, cvs):
        print(f"    {k:18s}: {c:.3f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--demo-dir",
        type=Path,
        default=Path(os.path.expanduser("~/aic_community_demos_compressed")),
    )
    parser.add_argument(
        "--bag-dir",
        type=Path,
        default=Path(os.path.expanduser("~/aic_community_bags_compressed")),
    )
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument(
        "--wallclock-hours",
        type=float,
        default=None,
        help="전체 수집 wall-clock 시간(h)을 수동 지정. "
             "미지정 시 run 디렉토리 mtime 기반 추정(부정확할 수 있음).",
    )
    args = parser.parse_args()

    if not args.demo_dir.exists():
        sys.stderr.write(f"[error] demo dir 없음: {args.demo_dir}\n")
        return 1
    if not args.bag_dir.exists():
        sys.stderr.write(f"[warn] bag dir 없음: {args.bag_dir} (P3/wall-clock 부정확)\n")

    episodes = scan_episodes(args.demo_dir)
    samples = scan_run_configs(args.bag_dir)
    auto_hours, n_runs = parse_run_wallclock(args.bag_dir)
    total_hours = args.wallclock_hours if args.wallclock_hours is not None else auto_hours

    print_summary(args.label, episodes, samples, total_hours, n_runs)
    if args.wallclock_hours is None:
        print("")
        print("[note] wall-clock이 부정확할 수 있습니다. 정확한 측정은:")
        print("       time ./scripts/collect_community.sh cheatcode N")
        print("       python scripts/metrics.py --wallclock-hours <X>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
