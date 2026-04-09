#
#  RunACTv1 — 자체 학습된 ACT 모델을 로드하여 실행하는 Policy
#  RunACT와 동일한 구조이되, HuggingFace Hub 대신 로컬 모델 경로를 사용한다.
#

import os
import time
import json
import torch
import numpy as np
import cv2
import draccus
from pathlib import Path
from typing import Dict
from rclpy.node import Node
from geometry_msgs.msg import Twist, Vector3

from aic_model.policy import (
    GetObservationCallback,
    MoveRobotCallback,
    Policy,
    SendFeedbackCallback,
)
from aic_model_interfaces.msg import Observation
from aic_task_interfaces.msg import Task

from aic_control_interfaces.msg import (
    MotionUpdate,
    TrajectoryGenerationMode,
)
from geometry_msgs.msg import Wrench

from lerobot.policies.act.modeling_act import ACTPolicy
from lerobot.policies.act.configuration_act import ACTConfig
from safetensors.torch import load_file


# 로컬 모델 경로 (환경변수로 오버라이드 가능)
DEFAULT_MODEL_PATH = os.path.expanduser(
    "~/ws_aic/src/aic/outputs/train/act_aic_v1/checkpoints/last/pretrained_model"
)


class RunACTv1(Policy):
    def __init__(self, parent_node: Node):
        super().__init__(parent_node)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        # 이미지 캡처 설정 (환경변수 ACT_CAPTURE_DIR 설정 시 활성화)
        capture_dir = os.environ.get("ACT_CAPTURE_DIR", "")
        self._capture_enabled = bool(capture_dir)
        self._capture_dir = Path(capture_dir) if capture_dir else None
        self._capture_step = 0
        self._capture_trial = 0
        if self._capture_enabled:
            self.get_logger().info(f"Image capture enabled: {self._capture_dir}")

        # 1. Configuration & Weights Loading (로컬 경로)
        policy_path = Path(os.environ.get("ACT_MODEL_PATH", DEFAULT_MODEL_PATH))
        self.get_logger().info(f"Loading ACT model from: {policy_path}")

        with open(policy_path / "config.json", "r") as f:
            config_dict = json.load(f)
            if "type" in config_dict:
                del config_dict["type"]

        config = draccus.decode(ACTConfig, config_dict)

        self.policy = ACTPolicy(config)
        self.policy.load_state_dict(load_file(policy_path / "model.safetensors"))
        self.policy.eval()
        self.policy.to(self.device)

        self.get_logger().info(f"ACT Policy loaded on {self.device}")

        # 2. Normalization Stats Loading
        stats_path = (
            policy_path / "policy_preprocessor_step_3_normalizer_processor.safetensors"
        )
        stats = load_file(stats_path)

        def get_stat(key, shape):
            return stats[key].to(self.device).view(*shape)

        self.img_stats = {
            "left": {
                "mean": get_stat("observation.images.left_camera.mean", (1, 3, 1, 1)),
                "std": get_stat("observation.images.left_camera.std", (1, 3, 1, 1)),
            },
            "center": {
                "mean": get_stat("observation.images.center_camera.mean", (1, 3, 1, 1)),
                "std": get_stat("observation.images.center_camera.std", (1, 3, 1, 1)),
            },
            "right": {
                "mean": get_stat("observation.images.right_camera.mean", (1, 3, 1, 1)),
                "std": get_stat("observation.images.right_camera.std", (1, 3, 1, 1)),
            },
        }

        self.state_mean = get_stat("observation.state.mean", (1, -1))
        self.state_std = get_stat("observation.state.std", (1, -1))
        self.action_mean = get_stat("action.mean", (1, -1))
        self.action_std = get_stat("action.std", (1, -1))

        self.image_scaling = 0.25
        self.get_logger().info("Normalization statistics loaded successfully.")

    @staticmethod
    def _img_to_tensor(raw_img, device, scale, mean, std):
        img_np = np.frombuffer(raw_img.data, dtype=np.uint8).reshape(
            raw_img.height, raw_img.width, 3
        )
        if scale != 1.0:
            img_np = cv2.resize(img_np, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        tensor = (
            torch.from_numpy(img_np).permute(2, 0, 1).float().div(255.0).unsqueeze(0).to(device)
        )
        return (tensor - mean) / std

    def prepare_observations(self, obs_msg: Observation) -> Dict[str, torch.Tensor]:
        obs = {
            "observation.images.left_camera": self._img_to_tensor(
                obs_msg.left_image, self.device, self.image_scaling,
                self.img_stats["left"]["mean"], self.img_stats["left"]["std"],
            ),
            "observation.images.center_camera": self._img_to_tensor(
                obs_msg.center_image, self.device, self.image_scaling,
                self.img_stats["center"]["mean"], self.img_stats["center"]["std"],
            ),
            "observation.images.right_camera": self._img_to_tensor(
                obs_msg.right_image, self.device, self.image_scaling,
                self.img_stats["right"]["mean"], self.img_stats["right"]["std"],
            ),
        }

        tcp_pose = obs_msg.controller_state.tcp_pose
        tcp_vel = obs_msg.controller_state.tcp_velocity
        state_np = np.array([
            tcp_pose.position.x, tcp_pose.position.y, tcp_pose.position.z,
            tcp_pose.orientation.x, tcp_pose.orientation.y, tcp_pose.orientation.z, tcp_pose.orientation.w,
            tcp_vel.linear.x, tcp_vel.linear.y, tcp_vel.linear.z,
            tcp_vel.angular.x, tcp_vel.angular.y, tcp_vel.angular.z,
            *obs_msg.controller_state.tcp_error,
            *obs_msg.joint_states.position[:7],
        ], dtype=np.float32)

        raw_state_tensor = torch.from_numpy(state_np).float().unsqueeze(0).to(self.device)
        obs["observation.state"] = (raw_state_tensor - self.state_mean) / self.state_std
        return obs

    def _capture_images(self, obs_msg):
        """평가 중 카메라 이미지를 저장 (ACT_CAPTURE_DIR 설정 시)."""
        if not self._capture_enabled:
            return
        trial_dir = self._capture_dir / f"trial_{self._capture_trial:02d}"
        for cam_name, img_msg in [("left", obs_msg.left_image), ("center", obs_msg.center_image), ("right", obs_msg.right_image)]:
            cam_dir = trial_dir / cam_name
            cam_dir.mkdir(parents=True, exist_ok=True)
            img_np = np.frombuffer(img_msg.data, dtype=np.uint8).reshape(img_msg.height, img_msg.width, 3)
            img_path = cam_dir / f"{self._capture_step:04d}.png"
            cv2.imwrite(str(img_path), cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR))
        self._capture_step += 1

    def insert_cable(self, task, get_observation, move_robot, send_feedback, **kwargs):
        self.policy.reset()
        self.get_logger().info(f"RunACTv1.insert_cable() enter. Task: {task}")
        self._capture_step = 0

        start_time = time.time()
        while time.time() - start_time < 30.0:
            loop_start = time.time()

            observation_msg = get_observation()
            if observation_msg is None:
                self.get_logger().info("No observation received.")
                continue

            # 이미지 캡처 (활성화 시)
            self._capture_images(observation_msg)

            obs_tensors = self.prepare_observations(observation_msg)

            with torch.inference_mode():
                normalized_action = self.policy.select_action(obs_tensors)

            raw_action_tensor = (normalized_action * self.action_std) + self.action_mean
            action = raw_action_tensor[0].cpu().numpy()

            self.get_logger().info(f"Action: {action}")

            # action은 pose 명령 (CheatCode의 set_pose_target과 동일)
            from geometry_msgs.msg import Pose, Point, Quaternion
            pose = Pose(
                position=Point(x=float(action[0]), y=float(action[1]), z=float(action[2])),
                orientation=Quaternion(
                    x=float(action[3]), y=float(action[4]),
                    z=float(action[5]), w=float(action[6]),
                ),
            )
            self.set_pose_target(move_robot=move_robot, pose=pose)
            send_feedback("in progress...")

            elapsed = time.time() - loop_start
            time.sleep(max(0, 0.25 - elapsed))

        self.get_logger().info("RunACTv1.insert_cable() exiting...")
        self._capture_trial += 1
        return True

    def set_cartesian_twist_target(self, twist, frame_id="base_link"):
        motion_update_msg = MotionUpdate()
        motion_update_msg.velocity = twist
        motion_update_msg.header.frame_id = frame_id
        motion_update_msg.header.stamp = self.get_clock().now().to_msg()
        motion_update_msg.target_stiffness = np.diag([100.0, 100.0, 100.0, 50.0, 50.0, 50.0]).flatten()
        motion_update_msg.target_damping = np.diag([40.0, 40.0, 40.0, 15.0, 15.0, 15.0]).flatten()
        motion_update_msg.feedforward_wrench_at_tip = Wrench(
            force=Vector3(x=0.0, y=0.0, z=0.0), torque=Vector3(x=0.0, y=0.0, z=0.0)
        )
        motion_update_msg.wrench_feedback_gains_at_tip = [0.5, 0.5, 0.5, 0.0, 0.0, 0.0]
        motion_update_msg.trajectory_generation_mode.mode = TrajectoryGenerationMode.MODE_VELOCITY
        return motion_update_msg
