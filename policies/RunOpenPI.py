#
#  RunOpenPI — OpenPI (pi0/pi05) 모델을 사용하는 AIC inner policy.
#
#  환경변수:
#    OPENPI_MODEL_PATH     — 로컬 체크포인트 폴더 경로 (기본: 프로젝트 내 checkpoints/ 심볼릭 링크)
#    OPENPI_CHECKPOINT     — HF 다운로드 시 사용할 체크포인트 폴더 이름 (기본: global_step_11040)
#    OPENPI_PROMPT         — 모델에 전달할 태스크 프롬프트
#    OPENPI_ACTION_DIM     — action 차원 수 (기본: 7)
#    OPENPI_ACTION_HORIZON — action chunk 크기 (기본: 5)
#    OPENPI_DEVICE         — cuda / cpu (기본: cuda if available)
#    OPENPI_ASSETS_DIR     — assets 경로 오버라이드 (_register.py 참고)
#
#  주의:
#    32GB 모델 로딩은 최초 insert_cable() 호출 시 수행됩니다 (lazy loading).
#    로딩 완료까지 수 분이 소요되므로 Worker의 --timeout 을 600초 이상으로 설정하세요.
#    예: aic-collector-worker --timeout 600 --policy openpi ...
#

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import torch
from geometry_msgs.msg import Point, Pose, Quaternion
from rclpy.node import Node

from aic_model.policy import (
    GetObservationCallback,
    MoveRobotCallback,
    Policy,
    SendFeedbackCallback,
)

# Default local checkpoint path (symlinked from HF cache by deploy step)
_DEFAULT_CHECKPOINT_DIR = (
    Path(__file__).parent.parent
    / "checkpoints"
    / "pi05_aic_cable_insert_ur5e"
    / "global_step_11040"
)
_HF_REPO_ID = (
    "Phy-lab/aic_cable_insert_sft_openpi_pi05_pretrained_v3_bs128_mb16_step11040"
)
_DEFAULT_CHECKPOINT = "global_step_11040"
_DEFAULT_PROMPT = "Insert the SFP-to-SC cable into the target port"
_DATASET_REPO = "Phy-lab/pretrained_dataset_v3_lerobot_v21_compat"


class RunOpenPI(Policy):
    """OpenPI (pi05) 기반 케이블 삽입 inner policy.

    CollectWrapper/CollectDispatchWrapper의 inner policy로 사용하도록 설계됨.
    매 step마다 get_observation()을 호출하므로 데이터 수집이 정상 동작한다.

    Model loading: __init__ 시점이 아닌 첫 insert_cable() 호출 시 lazy loading.
    32GB 모델 로딩은 수 분이 소요되므로 Worker --timeout을 600초 이상으로 설정할 것.

    action chunking: 모델은 한 번 호출 시 action_horizon(기본 5)개의 action을
    반환한다. 이를 순서대로 실행하고, 소진 시 다시 모델을 호출한다.
    """

    def __init__(self, parent_node: Node):
        super().__init__(parent_node)
        os.environ.setdefault("USE_TF", "0")

        # Store config for lazy model loading (actual load deferred to first insert_cable)
        self._action_dim = int(os.environ.get("OPENPI_ACTION_DIM", "7"))
        self._action_horizon = int(os.environ.get("OPENPI_ACTION_HORIZON", "5"))
        self._device_str = os.environ.get(
            "OPENPI_DEVICE", "cuda" if torch.cuda.is_available() else "cpu"
        )
        self._prompt = os.environ.get("OPENPI_PROMPT", _DEFAULT_PROMPT)
        self._model_path_env = os.environ.get(
            "OPENPI_MODEL_PATH", str(_DEFAULT_CHECKPOINT_DIR)
        )
        self._model = None  # loaded lazily on first insert_cable()

        self.get_logger().info(
            f"[RunOpenPI] Initialized (lazy). Model will load on first trial. "
            f"checkpoint={self._model_path_env}"
        )

    def _load_model(self) -> None:
        """32GB 모델을 GPU에 로드. 최초 insert_cable() 호출 시 한 번만 실행."""
        from aic_example_policies.ros._register import ensure_registered
        ensure_registered()

        # Resolve checkpoint directory
        local_path = Path(self._model_path_env).expanduser()
        if local_path.exists():
            checkpoint_dir = local_path.resolve()
        else:
            self.get_logger().warn(
                f"[RunOpenPI] Local checkpoint not found at {local_path}. "
                "Downloading from HuggingFace..."
            )
            from huggingface_hub import snapshot_download
            checkpoint_name = os.environ.get("OPENPI_CHECKPOINT", _DEFAULT_CHECKPOINT)
            snap_dir = snapshot_download(
                repo_id=_HF_REPO_ID,
                repo_type="model",
                allow_patterns=[f"{checkpoint_name}/**"],
            )
            checkpoint_dir = Path(snap_dir) / checkpoint_name
            if not checkpoint_dir.exists():
                raise FileNotFoundError(
                    f"Checkpoint '{checkpoint_name}' not found in {snap_dir}"
                )

        self.get_logger().info(
            f"[RunOpenPI] Loading model from {checkpoint_dir} onto {self._device_str} ..."
        )
        t0 = time.time()

        from omegaconf import OmegaConf
        cfg = OmegaConf.create(
            {
                "model_path": str(checkpoint_dir),
                "model_type": "openpi",
                "action_dim": self._action_dim,
                "num_action_chunks": self._action_horizon,
                "num_steps": self._action_horizon,
                "add_value_head": False,
                "openpi_data": {"repo_id": _DATASET_REPO},
                "openpi": {
                    "config_name": "pi05_aic_cable_insert_ur5e",
                    "num_images_in_input": 3,
                    "noise_level": 0.5,
                    "action_chunk": self._action_horizon,
                    "num_steps": self._action_horizon,
                    "train_expert_only": False,
                    "action_env_dim": self._action_dim,
                    "noise_method": "flow_sde",
                    "add_value_head": False,
                    "value_after_vlm": False,
                    "value_vlm_mode": "mean_token",
                    "detach_critic_input": True,
                },
            }
        )

        from rlinf.models.embodiment.openpi import get_model
        self._model = get_model(cfg).to(torch.device(self._device_str))
        self._model.eval()

        elapsed = time.time() - t0
        self.get_logger().info(
            f"[RunOpenPI] Model ready in {elapsed:.1f}s. "
            f"device={self._device_str}, action_dim={self._action_dim}, "
            f"action_horizon={self._action_horizon}"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _img_from_ros(img_msg) -> np.ndarray:
        """ROS sensor_msgs/Image → (H, W, 3) uint8 numpy array."""
        channels = img_msg.step // img_msg.width if img_msg.width > 0 else 3
        arr = np.frombuffer(img_msg.data, dtype=np.uint8).reshape(
            img_msg.height, img_msg.width, channels
        )
        if channels == 4:
            arr = arr[:, :, :3]
        return arr

    def _build_env_obs(self, obs_msg, prompt: str) -> dict:
        tcp = obs_msg.controller_state.tcp_pose
        state = np.array(
            [
                tcp.position.x,
                tcp.position.y,
                tcp.position.z,
                tcp.orientation.x,
                tcp.orientation.y,
                tcp.orientation.z,
                tcp.orientation.w,
            ],
            dtype=np.float32,
        )
        return {
            "main_images": np.expand_dims(self._img_from_ros(obs_msg.center_image), 0),
            "wrist_images": np.expand_dims(self._img_from_ros(obs_msg.left_image), 0),
            "extra_view_images": np.expand_dims(
                self._img_from_ros(obs_msg.right_image), 0
            ),
            "states": torch.from_numpy(state).unsqueeze(0),
            "task_descriptions": [prompt],
        }

    # ------------------------------------------------------------------
    # Policy interface
    # ------------------------------------------------------------------

    def insert_cable(
        self,
        task,
        get_observation: GetObservationCallback,
        move_robot: MoveRobotCallback,
        send_feedback: SendFeedbackCallback,
        **kwargs,
    ):
        # Lazy model loading — happens here so __init__ returns fast.
        # First call takes ~4 min for 32GB model; subsequent calls are instant.
        if self._model is None:
            self._load_model()

        prompt = os.environ.get("OPENPI_PROMPT", self._prompt)
        device = torch.device(self._device_str)
        self.get_logger().info(
            f"[RunOpenPI] insert_cable() start. prompt='{prompt}'"
        )

        start_time = time.time()
        action_chunk: np.ndarray | None = None  # (horizon, action_dim)
        chunk_idx = 0
        step = 0

        while time.time() - start_time < 30.0:
            loop_start = time.time()

            obs_msg = get_observation()
            if obs_msg is None:
                time.sleep(0.05)
                continue

            # Re-query model when action chunk is exhausted
            if action_chunk is None or chunk_idx >= len(action_chunk):
                env_obs = self._build_env_obs(obs_msg, prompt)
                with torch.no_grad():
                    actions, _ = self._model.predict_action_batch(
                        env_obs, mode="eval", compute_values=False
                    )
                # actions: (1, horizon, action_dim) → (horizon, action_dim)
                action_chunk = actions[0].detach().cpu().numpy()
                chunk_idx = 0
                self.get_logger().info(
                    f"[RunOpenPI] New action chunk. step={step}, "
                    f"first_action={action_chunk[0]}"
                )

            action = action_chunk[chunk_idx]
            chunk_idx += 1
            step += 1

            pose = Pose(
                position=Point(
                    x=float(action[0]), y=float(action[1]), z=float(action[2])
                ),
                orientation=Quaternion(
                    x=float(action[3]),
                    y=float(action[4]),
                    z=float(action[5]),
                    w=float(action[6]),
                ),
            )
            self.set_pose_target(move_robot=move_robot, pose=pose)
            send_feedback("in progress...")

            elapsed = time.time() - loop_start
            time.sleep(max(0, 0.05 - elapsed))

        self.get_logger().info(
            f"[RunOpenPI] insert_cable() done. total_steps={step}"
        )
        return True
