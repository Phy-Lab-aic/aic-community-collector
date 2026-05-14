import dataclasses

import einops
import numpy as np
import torch
from openpi import transforms
from openpi.models import model as _model


def _parse_image(image) -> np.ndarray:
    image = np.asarray(image)
    image = np.squeeze(image)
    if np.issubdtype(image.dtype, np.floating):
        image = (255 * image).astype(np.uint8)
    if image.shape[0] == 3:
        image = einops.rearrange(image, "c h w -> h w c")
    return image


def _fit_to_dim(x, target_dim: int, source_dim: int | None = None):
    if source_dim is not None:
        x = x[..., :source_dim]
    if x.shape[-1] > target_dim:
        return x[..., :target_dim]
    return transforms.pad_to_dim(x, target_dim)


@dataclasses.dataclass(frozen=True)
class AICInputs(transforms.DataTransformFn):
    """Convert AIC UR5e observations into OpenPI model inputs."""

    action_dim: int
    source_action_dim: int = 7
    model_type: _model.ModelType = _model.ModelType.PI05

    def __call__(self, data: dict) -> dict:
        if isinstance(data["observation/state"], np.ndarray):
            data["observation/state"] = torch.from_numpy(
                data["observation/state"]
            ).float()

        state = _fit_to_dim(
            data["observation/state"], self.action_dim, self.source_action_dim
        )
        center_image = _parse_image(data["observation/image_center"])
        left_image = _parse_image(data["observation/image_left"])
        right_image = _parse_image(data["observation/image_right"])

        if self.model_type in (_model.ModelType.PI0, _model.ModelType.PI05):
            image_names = ("base_0_rgb", "left_wrist_0_rgb", "right_wrist_0_rgb")
        elif self.model_type == _model.ModelType.PI0_FAST:
            image_names = ("base_0_rgb", "base_1_rgb", "wrist_0_rgb")
        else:
            raise ValueError(f"Unsupported model type: {self.model_type}")

        inputs = {
            "state": state,
            "image": dict(zip(image_names, (center_image, left_image, right_image), strict=True)),
            "image_mask": dict.fromkeys(image_names, np.True_),
        }

        if "actions" in data:
            inputs["actions"] = _fit_to_dim(
                data["actions"], self.action_dim, self.source_action_dim
            )

        if "prompt" in data:
            prompt = data["prompt"]
            if isinstance(prompt, bytes):
                prompt = prompt.decode("utf-8")
            inputs["prompt"] = prompt

        return inputs


@dataclasses.dataclass(frozen=True)
class AICOutputs(transforms.DataTransformFn):
    """Convert OpenPI outputs back to AIC action format."""

    output_action_dim: int = 7

    def __call__(self, data: dict) -> dict:
        return {"actions": np.asarray(data["actions"][:, : self.output_action_dim])}
