#
#  RunACTHybrid — ACT 모델로 포트까지 접근 + Spiral Search로 삽입 완료
#
#  Phase 1: ACT가 카메라를 보고 포트 상공까지 접근 (~0.06m)
#  Phase 2: F/T 센서 기반 Spiral Search + 하강으로 정밀 삽입
#
#  사용법:
#    ACT_MODEL_PATH=~/ws_aic/src/aic/outputs/train/act_aic_v1_backup/checkpoints/last/pretrained_model \
#    pixi run ros2 run aic_model aic_model \
#      --ros-args -p use_sim_time:=true \
#      -p policy:=aic_example_policies.ros.RunACTHybrid
#

import os
import time
import json
import math
import torch
import numpy as np
import cv2
import draccus
from pathlib import Path
from typing import Dict
from rclpy.node import Node
from geometry_msgs.msg import Twist, Vector3, Pose, Point, Quaternion, Wrench

# Rerun 시각화 (ACT_RERUN=1 설정 시 활성화)
RERUN_ENABLED = os.environ.get("ACT_RERUN", "") == "1"
if RERUN_ENABLED:
    import rerun as rr

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
from std_msgs.msg import Header

from lerobot.policies.act.modeling_act import ACTPolicy
from lerobot.policies.act.configuration_act import ACTConfig
from safetensors.torch import load_file


DEFAULT_MODEL_PATH = os.path.expanduser(
    "~/ws_aic/src/aic/outputs/train/act_aic_v1_backup/checkpoints/last/pretrained_model"
)

# Phase 2 파라미터
DESCENT_RATE = 0.0005          # z 하강 속도 (0.5mm/step) — 접촉까지 빠르게
MAX_DESCENT = 0.10             # 최대 하강 거리 (100mm) — ACT 거리 0.07m 커버 + 여유
STRAIGHT_DESCENT = 0.05        # 직선 하강 구간 (50mm) — ACT 위치 유지하며 하강
CONTACT_FORCE = 5.0            # 접촉 감지 힘 임계값 (N) — tare 후 노이즈 ±1N 마진 확보
FORCE_THRESHOLD = 15.0         # 안전 힘 임계값 (N) — tare 후 실제 힘 기준, 채점 20N 대비 마진
INSERTION_FORCE = 5.0          # 삽입 시 목표 z축 힘 (N) — feedforward wrench
INSERTION_DEPTH = 0.015        # 삽입 깊이 (15mm) — 접촉 후 이만큼 더 내려감
SPIRAL_RADIUS_START = 0.001    # Spiral 시작 반경 (1mm) — v7 설정
SPIRAL_RADIUS_MAX = 0.005      # Spiral 최대 반경 (5mm)
SPIRAL_RADIUS_INCREMENT = 0.0002
SPIRAL_ANGULAR_SPEED = 1.0     # rad/step
ACT_DURATION = 25.0            # ACT 접근 시간 (초)
PHASE2_DURATION = 30.0         # Phase 2 최대 시간 (초)


class RunACTHybrid(Policy):
    def __init__(self, parent_node: Node):
        super().__init__(parent_node)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        # 이미지 캡처 설정
        capture_dir = os.environ.get("ACT_CAPTURE_DIR", "")
        self._capture_enabled = bool(capture_dir)
        self._capture_dir = Path(capture_dir) if capture_dir else None
        self._capture_step = 0
        self._capture_trial = 0
        if self._capture_enabled:
            self.get_logger().info(f"Image capture enabled: {self._capture_dir}")

        # ACT 모델 로드
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

        # 정규화 통계 로드
        stats = load_file(policy_path / "policy_preprocessor_step_3_normalizer_processor.safetensors")

        def get_stat(key, shape):
            return stats[key].to(self.device).view(*shape)

        self.img_stats = {
            cam: {
                "mean": get_stat(f"observation.images.{cam}_camera.mean", (1, 3, 1, 1)),
                "std": get_stat(f"observation.images.{cam}_camera.std", (1, 3, 1, 1)),
            }
            for cam in ["left", "center", "right"]
        }

        self.state_mean = get_stat("observation.state.mean", (1, -1))
        self.state_std = get_stat("observation.state.std", (1, -1))
        self.action_mean = get_stat("action.mean", (1, -1))
        self.action_std = get_stat("action.std", (1, -1))

        self.image_scaling = 0.25
        self._rr_step = 0

        # Rerun 초기화
        if RERUN_ENABLED:
            rr.init("aic_hybrid_eval")
            rr.serve_web(open_browser=False, web_port=9090)
            self._rrd_path = os.path.expanduser("~/aic_eval_rerun/eval.rrd")
            os.makedirs(os.path.dirname(self._rrd_path), exist_ok=True)
            rr.save(self._rrd_path)
            self.get_logger().info(f"Rerun: web → http://<IP>:9090 | file → {self._rrd_path}")

        self.get_logger().info("ACT Hybrid Policy loaded.")

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

    def _capture_images(self, obs_msg):
        """평가 중 카메라 이미지를 저장."""
        if not self._capture_enabled:
            return
        trial_dir = self._capture_dir / f"trial_{self._capture_trial:02d}"
        for cam_name, img_msg in [("left", obs_msg.left_image), ("center", obs_msg.center_image), ("right", obs_msg.right_image)]:
            cam_dir = trial_dir / cam_name
            cam_dir.mkdir(parents=True, exist_ok=True)
            img_np = np.frombuffer(img_msg.data, dtype=np.uint8).reshape(img_msg.height, img_msg.width, 3)
            cv2.imwrite(str(cam_dir / f"{self._capture_step:04d}.png"), cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR))
        self._capture_step += 1

    def _log_rerun(self, obs_msg, phase="", z_current=None, action=None):
        """Rerun에 전체 센서/상태 데이터를 로깅."""
        if not RERUN_ENABLED:
            return
        rr.set_time_sequence("step", self._rr_step)
        self._rr_step += 1

        # 카메라 이미지 3대
        for cam_name, img_msg in [("left", obs_msg.left_image), ("center", obs_msg.center_image), ("right", obs_msg.right_image)]:
            img_np = np.frombuffer(img_msg.data, dtype=np.uint8).reshape(img_msg.height, img_msg.width, 3)
            img_small = cv2.resize(img_np, (288, 256))
            rr.log(f"camera/{cam_name}", rr.Image(img_small))

        # TCP 위치 (3)
        tcp = obs_msg.controller_state.tcp_pose
        rr.log("robot/tcp_x", rr.Scalars([tcp.position.x]))
        rr.log("robot/tcp_y", rr.Scalars([tcp.position.y]))
        rr.log("robot/tcp_z", rr.Scalars([tcp.position.z]))

        # TCP 방향 (4)
        rr.log("robot/tcp_qx", rr.Scalars([tcp.orientation.x]))
        rr.log("robot/tcp_qy", rr.Scalars([tcp.orientation.y]))
        rr.log("robot/tcp_qz", rr.Scalars([tcp.orientation.z]))
        rr.log("robot/tcp_qw", rr.Scalars([tcp.orientation.w]))

        # TCP 속도 (6)
        vel = obs_msg.controller_state.tcp_velocity
        rr.log("robot/vel_linear_x", rr.Scalars([vel.linear.x]))
        rr.log("robot/vel_linear_y", rr.Scalars([vel.linear.y]))
        rr.log("robot/vel_linear_z", rr.Scalars([vel.linear.z]))
        rr.log("robot/vel_angular_x", rr.Scalars([vel.angular.x]))
        rr.log("robot/vel_angular_y", rr.Scalars([vel.angular.y]))
        rr.log("robot/vel_angular_z", rr.Scalars([vel.angular.z]))

        # TCP 오차 (6)
        for i, label in enumerate(["err_x", "err_y", "err_z", "err_rx", "err_ry", "err_rz"]):
            rr.log(f"robot/{label}", rr.Scalars([obs_msg.controller_state.tcp_error[i]]))

        # 관절 위치 (7)
        for i, pos in enumerate(obs_msg.joint_states.position[:7]):
            rr.log(f"joints/joint_{i}", rr.Scalars([pos]))

        # F/T 센서 — 힘 (3) + 토크 (3) + magnitude
        w = obs_msg.wrist_wrench.wrench
        rr.log("sensor/force_x", rr.Scalars([w.force.x]))
        rr.log("sensor/force_y", rr.Scalars([w.force.y]))
        rr.log("sensor/force_z", rr.Scalars([w.force.z]))
        rr.log("sensor/force_magnitude", rr.Scalars([float(np.linalg.norm([w.force.x, w.force.y, w.force.z]))]))
        rr.log("sensor/torque_x", rr.Scalars([w.torque.x]))
        rr.log("sensor/torque_y", rr.Scalars([w.torque.y]))
        rr.log("sensor/torque_z", rr.Scalars([w.torque.z]))

        # ACT 출력 action (7) — 전달된 경우만
        if action is not None:
            for i, label in enumerate(["act_x", "act_y", "act_z", "act_qx", "act_qy", "act_qz", "act_qw"]):
                rr.log(f"action/{label}", rr.Scalars([float(action[i])]))

        # 제어 상태
        if z_current is not None:
            rr.log("control/z_target", rr.Scalars([z_current]))
        rr.log("control/phase", rr.TextLog(f"Trial {self._capture_trial} / {phase}"))

    def prepare_observations(self, obs_msg: Observation) -> Dict[str, torch.Tensor]:
        obs = {}
        for cam_name, img_msg in [("left", obs_msg.left_image), ("center", obs_msg.center_image), ("right", obs_msg.right_image)]:
            obs[f"observation.images.{cam_name}_camera"] = self._img_to_tensor(
                img_msg, self.device, self.image_scaling,
                self.img_stats[cam_name]["mean"], self.img_stats[cam_name]["std"],
            )

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

    def _get_tcp_pose(self, obs_msg: Observation):
        """현재 TCP 위치와 방향을 반환."""
        tcp = obs_msg.controller_state.tcp_pose
        return (
            tcp.position.x, tcp.position.y, tcp.position.z,
            tcp.orientation.x, tcp.orientation.y, tcp.orientation.z, tcp.orientation.w,
        )

    def _get_force(self, obs_msg: Observation):
        """F/T 센서에서 힘 벡터를 반환."""
        w = obs_msg.wrist_wrench.wrench
        return np.array([w.force.x, w.force.y, w.force.z])

    def _send_pose_with_stiffness(self, move_robot, pose, stiffness_xy, stiffness_z, stiffness_rot, damping_factor=0.5):
        """커스텀 stiffness로 pose 명령을 전송."""
        stiffness = np.diag([
            stiffness_xy, stiffness_xy, stiffness_z,
            stiffness_rot, stiffness_rot, stiffness_rot,
        ]).flatten()
        damping = np.diag([
            stiffness_xy * damping_factor, stiffness_xy * damping_factor, stiffness_z * damping_factor,
            stiffness_rot * damping_factor, stiffness_rot * damping_factor, stiffness_rot * damping_factor,
        ]).flatten()

        motion_update = MotionUpdate(
            header=Header(
                frame_id="base_link",
                stamp=self.get_clock().now().to_msg(),
            ),
            pose=pose,
            target_stiffness=stiffness,
            target_damping=damping,
            feedforward_wrench_at_tip=Wrench(
                force=Vector3(x=0.0, y=0.0, z=0.0),
                torque=Vector3(x=0.0, y=0.0, z=0.0),
            ),
            wrench_feedback_gains_at_tip=[0.5, 0.5, 0.5, 0.0, 0.0, 0.0],
            trajectory_generation_mode=TrajectoryGenerationMode(
                mode=TrajectoryGenerationMode.MODE_POSITION,
            ),
        )
        try:
            move_robot(motion_update=motion_update)
        except Exception as ex:
            self.get_logger().warn(f"move_robot exception: {ex}")

    def phase1_act_approach(self, task, get_observation, move_robot, send_feedback):
        """Phase 1: ACT 모델로 포트 상공까지 접근."""
        self.get_logger().info("[Phase 1] ACT 접근 시작")
        self.policy.reset()

        start_time = time.time()
        last_tcp = None

        while time.time() - start_time < ACT_DURATION:
            loop_start = time.time()

            observation_msg = get_observation()
            if observation_msg is None:
                continue

            self._capture_images(observation_msg)
            obs_tensors = self.prepare_observations(observation_msg)

            with torch.inference_mode():
                normalized_action = self.policy.select_action(obs_tensors)

            raw_action_tensor = (normalized_action * self.action_std) + self.action_mean
            action = raw_action_tensor[0].cpu().numpy()

            self._log_rerun(observation_msg, phase="ACT",
                            z_current=observation_msg.controller_state.tcp_pose.position.z,
                            action=action)

            pose = Pose(
                position=Point(x=float(action[0]), y=float(action[1]), z=float(action[2])),
                orientation=Quaternion(
                    x=float(action[3]), y=float(action[4]),
                    z=float(action[5]), w=float(action[6]),
                ),
            )
            self.set_pose_target(move_robot=move_robot, pose=pose)
            send_feedback("Phase 1: ACT approach")

            last_tcp = self._get_tcp_pose(observation_msg)

            elapsed = time.time() - loop_start
            time.sleep(max(0, 0.25 - elapsed))

        self.get_logger().info(f"[Phase 1] ACT 접근 완료. TCP: {last_tcp}")
        return last_tcp

    def phase2_compliant_insertion(self, get_observation, move_robot, send_feedback, initial_tcp):
        """Phase 2: control.md 설계 기반 삽입 전략.

        Phase B: 하강 + 나선 탐색 동시 (접촉까지) — ACT의 x-y 오차 커버
        Phase C: 접촉 감지 → Spiral Search (정렬 보정)
        Phase D: 순응적 삽입 (접촉 유지하며 밀어넣기)
        """
        cx, cy, cz = initial_tcp[0], initial_tcp[1], initial_tcp[2]
        qx, qy, qz, qw = initial_tcp[3], initial_tcp[4], initial_tcp[5], initial_tcp[6]

        z_current = cz
        contact_detected = False
        contact_z = None
        angle = 0.0
        radius = SPIRAL_RADIUS_START
        step = 0
        phase = "B"  # B: 하강, C: spiral, D: 삽입

        # 소프트웨어 tare: Phase 2 시작 시 F/T 기준값 측정
        obs_for_tare = get_observation()
        if obs_for_tare is not None:
            self._force_baseline = self._get_force(obs_for_tare)
            self.get_logger().info(f"[Tare] F/T 기준값: [{self._force_baseline[0]:.1f}, {self._force_baseline[1]:.1f}, {self._force_baseline[2]:.1f}]N (magnitude={np.linalg.norm(self._force_baseline):.1f}N)")
        else:
            self._force_baseline = np.zeros(3)

        start_time = time.time()

        self.get_logger().info(f"[Phase B] 하강+나선 탐색 시작. TCP=({cx:.4f},{cy:.4f},{cz:.4f})")

        while time.time() - start_time < PHASE2_DURATION:
            loop_start = time.time()

            observation_msg = get_observation()
            if observation_msg is None:
                continue

            self._capture_images(observation_msg)

            # F/T 센서 읽기 (소프트웨어 tare 적용)
            raw_force = self._get_force(observation_msg)
            force = raw_force - self._force_baseline  # 기준값 제거
            force_z = abs(force[2])  # z축 힘 (접촉 감지)
            force_lateral = np.linalg.norm(force[:2])  # x-y 측면 힘
            force_magnitude = np.linalg.norm(force)

            # 안전 체크
            if force_magnitude > FORCE_THRESHOLD:
                self.get_logger().warn(f"[Phase {phase}] 안전 후퇴: {force_magnitude:.1f}N")
                z_current += 0.001  # 1mm 후퇴
                phase = "B"  # 다시 하강으로
                contact_detected = False

            elif phase == "B":
                # Phase B: 하강 + 미세 나선 탐색 동시 수행
                z_current -= DESCENT_RATE

                # 미세 나선 좌표 (ACT 위치 주변을 작은 범위로 탐색)
                search_x = cx + radius * math.cos(angle)
                search_y = cy + radius * math.sin(angle)

                angle += SPIRAL_ANGULAR_SPEED
                if radius < SPIRAL_RADIUS_MAX:
                    radius += SPIRAL_RADIUS_INCREMENT

                # 최대 하강 거리 초과
                if abs(cz - z_current) > MAX_DESCENT:
                    self.get_logger().info(f"[Phase B] 최대 하강 거리 도달: {abs(cz - z_current):.4f}m")
                    break

                # 접촉 감지
                if force_z > CONTACT_FORCE:
                    contact_detected = True
                    contact_z = z_current
                    cx, cy = search_x, search_y
                    phase = "C"
                    radius = SPIRAL_RADIUS_START
                    angle = 0.0
                    self.get_logger().info(f"[Phase B→C] 접촉 감지! F_z={force_z:.1f}N, z={z_current:.4f}, pos=({search_x:.4f},{search_y:.4f})")

                pose = Pose(
                    position=Point(x=search_x, y=search_y, z=z_current),
                    orientation=Quaternion(x=qx, y=qy, z=qz, w=qw),
                )
                self._send_pose_with_stiffness(
                    move_robot, pose,
                    stiffness_xy=300.0,  # v7 설정 — 나선 탐색 + 중간 강성
                    stiffness_z=100.0,
                    stiffness_rot=50.0,
                    damping_factor=0.6,
                )

            elif phase == "C":
                # Phase C: Spiral Search — 접촉 상태에서 나선 탐색
                spiral_x = cx + radius * math.cos(angle)
                spiral_y = cy + radius * math.sin(angle)

                # 나선하며 약간 하강
                z_current -= DESCENT_RATE * 0.2

                angle += SPIRAL_ANGULAR_SPEED
                if radius < SPIRAL_RADIUS_MAX:
                    radius += SPIRAL_RADIUS_INCREMENT

                # 힘이 줄어들면 (포트 구멍 발견) → 삽입 단계로
                if force_z < CONTACT_FORCE * 0.5 and contact_detected:
                    phase = "D"
                    cx, cy = spiral_x, spiral_y  # 포트 위치로 중심 갱신
                    self.get_logger().info(f"[Phase C→D] 포트 발견! F_z={force_z:.1f}N, 삽입 시작")

                pose = Pose(
                    position=Point(x=spiral_x, y=spiral_y, z=z_current),
                    orientation=Quaternion(x=qx, y=qy, z=qz, w=qw),
                )
                self._send_pose_with_stiffness(
                    move_robot, pose,
                    stiffness_xy=200.0,
                    stiffness_z=100.0,
                    stiffness_rot=30.0,
                    damping_factor=0.7,
                )

            elif phase == "D":
                # Phase D: 순응적 삽입 — 포트에 들어갔으니 밀어넣기
                z_current -= DESCENT_RATE

                if contact_z and abs(contact_z - z_current) > INSERTION_DEPTH:
                    self.get_logger().info(f"[Phase D] 삽입 깊이 도달: {abs(contact_z - z_current):.4f}m")
                    break

                pose = Pose(
                    position=Point(x=cx, y=cy, z=z_current),
                    orientation=Quaternion(x=qx, y=qy, z=qz, w=qw),
                )
                self._send_pose_with_stiffness(
                    move_robot, pose,
                    stiffness_xy=500.0,   # 정렬 유지
                    stiffness_z=50.0,     # 매우 낮은 z 강성
                    stiffness_rot=50.0,
                    damping_factor=0.5,
                )

            send_feedback(f"Phase {phase}: z={z_current:.4f} Fz={force_z:.1f} Fl={force_lateral:.1f}")
            self._log_rerun(observation_msg, phase=phase, z_current=z_current)

            step += 1
            if step % 20 == 0:
                self.get_logger().info(
                    f"[Phase {phase}] step={step} z={z_current:.4f} "
                    f"Fz={force_z:.1f} Fl={force_lateral:.1f} F={force_magnitude:.1f}"
                )

            elapsed = time.time() - loop_start
            time.sleep(max(0, 0.05 - elapsed))

        self.get_logger().info(f"[Phase 2] 종료. phase={phase}, z={z_current:.4f}, contact={contact_detected}")
        self.sleep_for(3.0)

    def insert_cable(self, task, get_observation, move_robot, send_feedback, **kwargs):
        self.get_logger().info(f"RunACTHybrid.insert_cable() enter. Task: {task}")
        self._capture_step = 0
        # _rr_step은 리셋하지 않음 — trial 간 누적하여 모든 trial이 보이도록

        # Phase 1: ACT 접근
        last_tcp = self.phase1_act_approach(task, get_observation, move_robot, send_feedback)

        if last_tcp is None:
            self.get_logger().error("[Hybrid] ACT 접근 실패 — TCP 없음")
            return False

        # Phase 2: 순응적 삽입 (하강 → 접촉 → Spiral → 삽입)
        self.phase2_compliant_insertion(get_observation, move_robot, send_feedback, last_tcp)

        self.get_logger().info("RunACTHybrid.insert_cable() exiting...")
        self._capture_trial += 1
        return True
