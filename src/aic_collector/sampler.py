#!/usr/bin/env python3
"""
EXP-009 파라미터 샘플링 유틸.

입력:
  - 파라미터 범위 dict: {"nic0_translation": (-0.0215, 0.0234), ...}
  - 전략: uniform | lhs | sobol
  - seed, runs

출력:
  - 리스트 of dict: [{"nic0_translation": 0.01, ...}, ...]
  - 각 dict가 하나의 run에 해당

재현성:
  동일 seed + 동일 범위 + 동일 전략 → 동일 출력 보장 (단위 테스트로 확인)

Usage (CLI):
    python sampler.py --strategy lhs --runs 10 --seed 42 \\
        --config configs/e2e_default.yaml

    # 출력: JSON 배열 (stdout)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

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
        strategy: "uniform" | "lhs" | "sobol"
        runs: 생성할 샘플 수
        seed: 재현용 seed

    Returns:
        List of dicts, 각 dict는 {파라미터 이름: 값}
    """
    if strategy not in STRATEGIES:
        raise ValueError(
            f"알 수 없는 샘플링 전략: {strategy}. "
            f"사용 가능: {list(STRATEGIES.keys())}"
        )
    keys, bounds = build_bounds(params_cfg)
    arr = STRATEGIES[strategy](bounds, runs, seed)
    return [
        {k: round(float(arr[i, d]), 4) for d, k in enumerate(keys)}
        for i in range(runs)
    ]


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
