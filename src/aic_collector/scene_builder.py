#!/usr/bin/env python3
"""
ScenePlan → 엔진 config dict 통합 빌더.

입력: `ScenePlan` (1개 이상의 `TrialPlan` 포함) + 템플릿 경로
출력: 엔진이 바로 실행 가능한 완전한 config dict

기존 `build_training_config.py`(1-trial 한정)의 조립 로직을 일반화하여,
`trials_per_config=1` (학습) / `=3` (평가 묶음) 모두 지원한다.

Phase 1에서는 내부 리팩토링 대상으로 도입. Phase 2+ UI에서 직접 호출.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.stderr.write("pyyaml not installed. pip install pyyaml\n")
    sys.exit(1)

from aic_collector.scene_plan import ScenePlan, TrialPlan


# ---------------------------------------------------------------------------
# AIC 공식 규칙 기반 고정값 (기존 build_training_config.py에서 이동)
# ---------------------------------------------------------------------------

TASK_BOARD_POSE: dict[str, dict[str, float]] = {
    "sfp": {"x": 0.15, "y": -0.2, "z": 1.14, "roll": 0.0, "pitch": 0.0, "yaw": 3.1415},
    "sc":  {"x": 0.17, "y": 0.0,  "z": 1.14, "roll": 0.0, "pitch": 0.0, "yaw": 3.0},
}
"""Task board pose — FOV/도달범위 보장용 고정값 (config_피드백.md 제약)."""


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
# 템플릿 로드
# ---------------------------------------------------------------------------


def load_fixed_sections(template_path: Path) -> dict[str, Any]:
    """템플릿에서 scoring, task_board_limits, robot만 추출.

    trials 섹션은 ScenePlan으로부터 동적 생성하므로 무시한다.
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
# Scene 빌더 — TrialPlan 기반
# ---------------------------------------------------------------------------


def _build_nic_rails(trial: TrialPlan) -> dict[str, dict[str, Any]]:
    rails: dict[str, dict[str, Any]] = {}
    for r in range(5):
        key = f"nic_rail_{r}"
        if r in trial.nic_rails:
            pose = trial.nic_poses[r]
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


def _build_sc_rails(trial: TrialPlan) -> dict[str, dict[str, Any]]:
    rails: dict[str, dict[str, Any]] = {}
    for r in (0, 1):
        key = f"sc_rail_{r}"
        if r in trial.sc_rails:
            pose = trial.sc_poses[r]
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


def _build_scene(trial: TrialPlan) -> dict[str, Any]:
    """`trials.trial_N.scene` dict 생성."""
    return {
        "task_board": {
            "pose": TASK_BOARD_POSE[trial.task_type],
            **_build_nic_rails(trial),
            **_build_sc_rails(trial),
            **MOUNT_RAILS_TRIAL1,
        },
        "cables": {
            "cable_0": {
                "pose": {
                    "gripper_offset": {
                        "x": trial.gripper["x"],
                        "y": trial.gripper["y"],
                        "z": trial.gripper["z"],
                    },
                    "roll":  trial.gripper["roll"],
                    "pitch": trial.gripper["pitch"],
                    "yaw":   trial.gripper["yaw"],
                },
                "attach_cable_to_gripper": True,
                "cable_type": CABLE_TYPE_BY_TASK[trial.task_type],
            }
        },
    }


def _build_tasks(trial: TrialPlan) -> dict[str, Any]:
    """`trials.trial_N.tasks` dict 생성."""
    plug = TASK_PLUG_BY_TYPE[trial.task_type]
    if trial.task_type == "sfp":
        target_module = f"nic_card_mount_{trial.target_rail}"
    else:
        target_module = f"sc_port_{trial.target_rail}"
    return {
        "task_1": {
            "cable_type": "sfp_sc",
            "cable_name": "cable_0",
            "plug_type": plug["plug_type"],
            "plug_name": plug["plug_name"],
            "port_type": plug["port_type"],
            "port_name": trial.target_port_name,
            "target_module_name": target_module,
            "time_limit": TIME_LIMIT,
        }
    }


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def build_scene_config(plan: ScenePlan, template_path: Path) -> dict[str, Any]:
    """ScenePlan으로부터 완전한 엔진 config dict 생성.

    - plan.trials[i]가 config의 trial_{i+1}이 된다.
    - scoring/task_board_limits/robot은 템플릿에서 그대로 가져옴.
    """
    if not plan.trials:
        raise ValueError("ScenePlan.trials가 비어있습니다")

    fixed = load_fixed_sections(template_path)
    trials_dict: dict[str, Any] = {}
    for i, trial in enumerate(plan.trials, start=1):
        trials_dict[f"trial_{i}"] = {
            "scene": _build_scene(trial),
            "tasks": _build_tasks(trial),
        }
    return {**fixed, "trials": trials_dict}


def dump_config(cfg: dict[str, Any]) -> str:
    """dict → YAML 텍스트 (엔진이 기대하는 키 순서 유지)."""
    return yaml.safe_dump(
        cfg, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
