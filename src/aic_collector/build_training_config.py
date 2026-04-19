#!/usr/bin/env python3
"""
Training용 엔진 config YAML 빌더 (Phase 1 이후: scene_builder 위임).

공개 API는 기존과 동일하게 유지한다:
  - build_training_config(sample, template_path) -> dict
  - dump_training_config(cfg) -> str
  - next_config_index(out_dir, prefix) -> int
  - write_training_configs(samples, out_dir, template_path, index_width=4) -> list[Path]

내부 구현은 `scene_plan.ScenePlan` + `scene_builder.build_scene_config`로 위임.
Phase 2+에서 webapp UI가 scene_builder를 직접 호출하게 되면서 이 모듈은
`TrainingSample` 기반 호출자를 위한 얇은 wrapper로 남는다.

Usage:
    from aic_collector.sampler import sample_training_configs
    from aic_collector.build_training_config import (
        build_training_config, next_config_index, write_training_configs,
    )

    samples = sample_training_configs(training_cfg, "sfp", count=50, seed=42)
    write_training_configs(samples, out_dir=Path("configs/train/sfp"))
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml  # noqa: F401  # 하위 호환 (기존 호출자가 이 모듈에서 yaml 사용 기대)
except ImportError:
    sys.stderr.write("pyyaml not installed. pip install pyyaml\n")
    sys.exit(1)

from aic_collector.sampler import TrainingSample, training_sample_to_scene_plan
from aic_collector.scene_builder import build_scene_config, dump_config


# ---------------------------------------------------------------------------
# 공개 API (wrapper)
# ---------------------------------------------------------------------------


def build_training_config(
    sample: TrainingSample,
    template_path: Path,
) -> dict[str, Any]:
    """Training 샘플 → 엔진 config dict (1-trial).

    Phase 1부터 내부적으로 ScenePlan/scene_builder에 위임.
    기존 호출자 동작은 완전 보존 (behavior preservation 테스트로 검증).
    """
    plan = training_sample_to_scene_plan(sample)
    return build_scene_config(plan, template_path)


def dump_training_config(cfg: dict[str, Any]) -> str:
    """dict을 YAML 텍스트로 직렬화 (scene_builder.dump_config와 동일)."""
    return dump_config(cfg)


# ---------------------------------------------------------------------------
# 출력 디렉토리 / 번호 관리
# ---------------------------------------------------------------------------


def next_config_index(out_dir: Path, prefix: str) -> int:
    """out_dir에서 `{prefix}_NNNN.yaml` 중 가장 큰 NNNN + 1 반환.

    기존 파일이 없으면 0. append 모드로 이어서 생성할 때 사용.
    """
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)\.yaml$")
    if not out_dir.exists():
        return 0
    maxn = -1
    for f in out_dir.iterdir():
        if not f.is_file():
            continue
        m = pattern.match(f.name)
        if m:
            maxn = max(maxn, int(m.group(1)))
    return maxn + 1


def write_training_configs(
    samples: list[TrainingSample],
    out_dir: Path,
    template_path: Path,
    index_width: int = 4,
) -> list[Path]:
    """samples를 out_dir/{prefix}_NNNN.yaml로 기록.

    prefix는 sample.task_type에서 자동 결정 ("config_sfp" / "config_sc").
    파일명의 NNNN은 sample.sample_index를 index_width 자리로 포맷.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for sample in samples:
        prefix = f"config_{sample.task_type}"
        fname = f"{prefix}_{sample.sample_index:0{index_width}d}.yaml"
        out_path = out_dir / fname
        cfg = build_training_config(sample, template_path)
        out_path.write_text(dump_training_config(cfg))
        written.append(out_path)
    return written
