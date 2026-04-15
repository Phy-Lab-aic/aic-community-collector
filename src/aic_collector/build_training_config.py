#!/usr/bin/env python3
"""
Training용 엔진 config YAML 빌더.

입력:
  - TrainingSample (sampler.py가 생성)
  - 템플릿 경로 (scoring/task_board_limits/robot 등 고정 섹션 추출용)
  - gripper nominal 값 (task_type별)

출력:
  - 완전한 엔진 config YAML 텍스트 (엔진이 바로 실행 가능)

build_engine_config.py(sweep 전용, 플레이스홀더 치환 방식)와 별개로 동작한다.
Training은 scene 엔티티 개수·rail 선택이 가변이라 문자열 치환이 어렵기 때문이다.

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
    import yaml
except ImportError:
    sys.stderr.write("pyyaml not installed. pip install pyyaml\n")
    sys.exit(1)

from aic_collector.sampler import TrainingSample


# ---------------------------------------------------------------------------
# 상수: 피드백 문서 기준의 고정 값
# ---------------------------------------------------------------------------

TASK_BOARD_POSE: dict[str, dict[str, float]] = {
    "sfp": {"x": 0.15, "y": -0.2, "z": 1.14, "roll": 0.0, "pitch": 0.0, "yaw": 3.1415},
    "sc":  {"x": 0.17, "y": 0.0,  "z": 1.14, "roll": 0.0, "pitch": 0.0, "yaw": 3.0},
}
"""Task board pose — FOV/도달범위 충족되는 고정값 (피드백 문서 ⚠️ 제한)."""

# trial_1 mount rail 패턴 (피드백 문서: "Mount rails — sample trial_1 패턴 고정")
MOUNT_RAILS_TRIAL1: dict[str, dict[str, Any]] = {
    "lc_mount_rail_0": {
        "entity_present": True,
        "entity_name": "lc_mount_0",
        "entity_pose": {"translation": 0.02, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
    },
    "sfp_mount_rail_0": {
        "entity_present": True,
        "entity_name": "sfp_mount_0",
        "entity_pose": {"translation": 0.03, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
    },
    "sc_mount_rail_0": {
        "entity_present": True,
        "entity_name": "sc_mount_0",
        "entity_pose": {"translation": -0.02, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
    },
    "lc_mount_rail_1": {
        "entity_present": True,
        "entity_name": "lc_mount_1",
        "entity_pose": {"translation": -0.01, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
    },
    "sfp_mount_rail_1": {"entity_present": False},
    "sc_mount_rail_1": {"entity_present": False},
}

CABLE_TYPE_BY_TASK = {
    "sfp": "sfp_sc_cable",
    "sc":  "sfp_sc_cable_reversed",
}

TASK_PLUG_BY_TYPE = {
    "sfp": {"plug_type": "sfp", "plug_name": "sfp_tip", "port_type": "sfp"},
    "sc":  {"plug_type": "sc",  "plug_name": "sc_tip",  "port_type": "sc"},
}

TIME_LIMIT = 180


# ---------------------------------------------------------------------------
# 템플릿 로드 (고정 섹션 추출)
# ---------------------------------------------------------------------------


def load_fixed_sections(template_path: Path) -> dict[str, Any]:
    """템플릿에서 scoring, task_board_limits, robot만 추출.

    trials 섹션은 training에서 동적으로 생성하므로 무시한다.
    """
    if not template_path.exists():
        raise FileNotFoundError(f"템플릿 없음: {template_path}")
    with open(template_path) as f:
        cfg = yaml.safe_load(f) or {}
    keys = ["scoring", "task_board_limits", "robot"]
    missing = [k for k in keys if k not in cfg]
    if missing:
        raise ValueError(f"템플릿에 필수 섹션 누락: {missing}")
    return {k: cfg[k] for k in keys}


# ---------------------------------------------------------------------------
# Scene 빌더
# ---------------------------------------------------------------------------


def _build_nic_rails(sample: TrainingSample) -> dict[str, dict[str, Any]]:
    """nic_rail_0 ~ nic_rail_4 각각에 대해 entity_present/pose를 구성."""
    rails: dict[str, dict[str, Any]] = {}
    for r in range(5):
        key = f"nic_rail_{r}"
        if r in sample.nic_rails:
            pose = sample.nic_poses[r]
            rails[key] = {
                "entity_present": True,
                "entity_name": f"nic_card_{r}",
                "entity_pose": {
                    "translation": pose["translation"],
                    "roll": 0.0,
                    "pitch": 0.0,
                    "yaw": pose["yaw"],
                },
            }
        else:
            rails[key] = {"entity_present": False}
    return rails


def _build_sc_rails(sample: TrainingSample) -> dict[str, dict[str, Any]]:
    """sc_rail_0, sc_rail_1 구성."""
    rails: dict[str, dict[str, Any]] = {}
    for r in (0, 1):
        key = f"sc_rail_{r}"
        if r in sample.sc_rails:
            pose = sample.sc_poses[r]
            rails[key] = {
                "entity_present": True,
                "entity_name": f"sc_mount_{r}",
                "entity_pose": {
                    "translation": pose["translation"],
                    "roll": 0.0,
                    "pitch": 0.0,
                    "yaw": pose["yaw"],
                },
            }
        else:
            rails[key] = {"entity_present": False}
    return rails


def _build_scene(sample: TrainingSample) -> dict[str, Any]:
    """`trials.trial_1.scene` dict 생성."""
    scene = {
        "task_board": {
            "pose": TASK_BOARD_POSE[sample.task_type],
            **_build_nic_rails(sample),
            **_build_sc_rails(sample),
            **MOUNT_RAILS_TRIAL1,
        },
        "cables": {
            "cable_0": {
                "pose": {
                    "gripper_offset": {
                        "x": sample.gripper["x"],
                        "y": sample.gripper["y"],
                        "z": sample.gripper["z"],
                    },
                    "roll":  sample.gripper["roll"],
                    "pitch": sample.gripper["pitch"],
                    "yaw":   sample.gripper["yaw"],
                },
                "attach_cable_to_gripper": True,
                "cable_type": CABLE_TYPE_BY_TASK[sample.task_type],
            }
        },
    }
    return scene


def _build_tasks(sample: TrainingSample) -> dict[str, Any]:
    """`trials.trial_1.tasks` dict 생성."""
    plug = TASK_PLUG_BY_TYPE[sample.task_type]
    if sample.task_type == "sfp":
        target_module = f"nic_card_mount_{sample.target_rail}"
    else:
        target_module = f"sc_port_{sample.target_rail}"
    return {
        "task_1": {
            "cable_type": "sfp_sc",
            "cable_name": "cable_0",
            "plug_type": plug["plug_type"],
            "plug_name": plug["plug_name"],
            "port_type": plug["port_type"],
            "port_name": sample.target_port_name,
            "target_module_name": target_module,
            "time_limit": TIME_LIMIT,
        }
    }


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def build_training_config(
    sample: TrainingSample,
    template_path: Path,
) -> dict[str, Any]:
    """Training 샘플 하나로부터 완전한 엔진 config dict를 생성.

    Args:
        sample: sampler.sample_training_configs()의 한 항목
        template_path: 고정 섹션(scoring/limits/robot)을 가진 YAML

    Returns:
        엔진 config dict (yaml.safe_dump로 바로 파일에 쓸 수 있음)
    """
    fixed = load_fixed_sections(template_path)
    cfg = {
        **fixed,
        "trials": {
            "trial_1": {
                "scene": _build_scene(sample),
                "tasks": _build_tasks(sample),
            }
        },
    }
    return cfg


def dump_training_config(cfg: dict[str, Any]) -> str:
    """dict을 YAML 텍스트로 직렬화 (엔진이 기대하는 키 순서 유지)."""
    return yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, default_flow_style=False)


# ---------------------------------------------------------------------------
# 출력 디렉토리 / 번호 관리
# ---------------------------------------------------------------------------


def next_config_index(out_dir: Path, prefix: str) -> int:
    """out_dir에서 `{prefix}_NNNN.yaml` 중 가장 큰 NNNN + 1 반환.

    기존 파일 없으면 0 반환. append 모드로 이어서 생성할 때 사용.
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

    prefix는 sample.task_type에서 자동 결정 (예: "config_sfp").
    파일명의 NNNN은 sample.sample_index를 index_width(기본 4)자리로 포맷.

    Returns:
        작성된 파일 경로 리스트.
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
