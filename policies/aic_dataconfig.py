import dataclasses
import pathlib

import openpi.models.model as _model
import openpi.transforms as _transforms
from openpi.training.config import DataConfig, DataConfigFactory, ModelTransformFactory
from typing_extensions import override

from aic_example_policies.ros import aic_policy


@dataclasses.dataclass(frozen=True)
class LeRobotAICDataConfig(DataConfigFactory):
    """OpenPI data config for the AIC UR5e cable insertion LeRobot dataset."""

    default_prompt: str | None = "Insert the SFP-to-SC cable into the target port"
    output_action_dim: int = 7
    source_action_dim: int = 7

    @override
    def create(
        self, assets_dirs: pathlib.Path, model_config: _model.BaseModelConfig
    ) -> DataConfig:
        repack_transform = _transforms.Group(
            inputs=[
                _transforms.RepackTransform(
                    {
                        "observation/image_center": "observation.images.cam_center",
                        "observation/image_left": "observation.images.cam_left",
                        "observation/image_right": "observation.images.cam_right",
                        "observation/state": "observation.state",
                        "actions": "action",
                    }
                )
            ]
        )

        data_transforms = _transforms.Group(
            inputs=[
                aic_policy.AICInputs(
                    action_dim=model_config.action_dim,
                    source_action_dim=self.source_action_dim,
                    model_type=model_config.model_type,
                )
            ],
            outputs=[aic_policy.AICOutputs(output_action_dim=self.output_action_dim)],
        )

        model_transforms = ModelTransformFactory(default_prompt=self.default_prompt)(
            model_config
        )

        return dataclasses.replace(
            self.create_base_config(assets_dirs, model_config),
            repack_transforms=repack_transform,
            data_transforms=data_transforms,
            model_transforms=model_transforms,
            action_sequence_keys=("action",),
        )
