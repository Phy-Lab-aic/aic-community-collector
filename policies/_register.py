"""Register pi05_aic_cable_insert_ur5e config into the rlinf openpi config registry.

Call ensure_registered() before get_model() when using this inference package
outside of the RLinf source tree that already includes the config.
"""
from __future__ import annotations

import os
from pathlib import Path

_CONFIG_NAME = "pi05_aic_cable_insert_ur5e"

# Assets dir: prefer env var override, then path relative to this file.
# After deploy_policies() copies this file + assets/ to aic_example_policies/ros/,
# the relative path still resolves correctly.
_ASSETS_DIR = Path(
    os.environ.get(
        "OPENPI_ASSETS_DIR",
        str(Path(__file__).parent / "assets" / "pi05_aic_cable_insert_ur5e"),
    )
)


def ensure_registered() -> None:
    from rlinf.models.embodiment.openpi.dataconfig import _CONFIGS_DICT

    if _CONFIG_NAME in _CONFIGS_DICT:
        return

    import dataclasses

    import openpi.models.pi0_config as pi0_config
    import openpi.training.weight_loaders as weight_loaders
    from openpi.training.config import AssetsConfig, DataConfig, TrainConfig

    from aic_example_policies.ros.aic_dataconfig import LeRobotAICDataConfig

    config = TrainConfig(
        name=_CONFIG_NAME,
        model=pi0_config.Pi0Config(pi05=True, action_horizon=5, discrete_state_input=False),
        data=LeRobotAICDataConfig(
            repo_id="Phy-lab/pretrained_dataset_v3_lerobot_v21_compat",
            base_config=DataConfig(prompt_from_task=True),
            assets=AssetsConfig(assets_dir=str(_ASSETS_DIR)),
        ),
        weight_loader=weight_loaders.CheckpointWeightLoader("checkpoints/jax/pi05_base/params"),
        pytorch_weight_path="checkpoints/torch/pi05_base",
    )

    from rlinf.models.embodiment.openpi.dataconfig import _CONFIGS

    _CONFIGS.append(config)
    _CONFIGS_DICT[_CONFIG_NAME] = config
