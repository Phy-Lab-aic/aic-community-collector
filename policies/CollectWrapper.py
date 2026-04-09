#
#  CollectWrapper — 범용 데이터 수집 래퍼. 어떤 Policy든 감싸서 데이터를 저장한다.
#
#  환경변수:
#    AIC_DEMO_DIR       — 저장 경로 (기본: ~/aic_demos)
#    AIC_INNER_POLICY   — 감쌀 Policy 클래스 경로 (기본: RunACTHybrid)
#    ACT_MODEL_PATH     — ACT 모델 경로 (inner policy가 ACT 사용 시)
#    AIC_F5_ENABLED     — F5 조기 종료 활성화 ("1"=on, "0"=off, 기본: on)
#
#  사용법:
#    AIC_DEMO_DIR=~/aic_community_demos_compressed \
#    AIC_INNER_POLICY=aic_example_policies.ros.RunACTHybrid \
#    ACT_MODEL_PATH=~/ws_aic/src/aic/outputs/train/act_aic_v1_backup/checkpoints/last/pretrained_model \
#    pixi run ros2 run aic_model aic_model \
#      --ros-args -p use_sim_time:=true \
#      -p policy:=aic_example_policies.ros.CollectWrapper
#

import os
import time
import json
import importlib
import numpy as np
import cv2
from pathlib import Path

from aic_model.policy import (
    GetObservationCallback,
    MoveRobotCallback,
    Policy,
    SendFeedbackCallback,
)
from aic_model_interfaces.msg import Observation
from aic_task_interfaces.msg import Task
from geometry_msgs.msg import Pose
from rclpy.time import Time
from std_msgs.msg import String
from tf2_ros import TransformException


class _InsertionCompleteSignal(Exception):
    """Inner policy의 루프를 조기 탈출하기 위한 내부 신호."""
    pass


class CollectWrapper(Policy):
    """범용 수집 래퍼. inner policy의 동작을 그대로 실행하면서 데이터를 저장한다.

    F5 (EXP-009): 삽입 완료 감지 시 조기 종료.
      - `/scoring/insertion_event` 토픽 구독 (시뮬레이터 ground truth, 유일한 신호)
      - 트리거되면 inner policy의 루프를 예외로 탈출 → insert_cable 반환

    Note: 이전 버전은 TF plug-port 거리 폴백을 사용했으나, false positive로
          부분 삽입 상태에서 탈출하는 문제가 있어 제거됨 (EXP-009 검증).
    """

    def __init__(self, parent_node):
        super().__init__(parent_node)

        # inner policy 로드
        # 형식: "aic_example_policies.ros.RunACTHybrid" → 모듈과 클래스명이 동일
        inner_policy_path = os.environ.get(
            "AIC_INNER_POLICY",
            "aic_example_policies.ros.RunACTHybrid"
        )
        # 파일명 = 클래스명이므로 전체 경로로 import 후 마지막 부분을 클래스명으로 사용
        class_name = inner_policy_path.rsplit(".", 1)[-1]
        module = importlib.import_module(inner_policy_path)
        inner_class = getattr(module, class_name)
        self._inner = inner_class(parent_node)
        self.get_logger().info(f"[CollectWrapper] Inner policy: {inner_policy_path}")

        # 저장 경로
        self._save_dir = Path(os.environ.get("AIC_DEMO_DIR", os.path.expanduser("~/aic_demos")))
        self._save_dir.mkdir(parents=True, exist_ok=True)

        # 에피소드 번호 자동 증가
        existing = [d for d in self._save_dir.iterdir() if d.is_dir() and d.name.startswith("episode_")]
        self._episode_counter = len(existing)
        self._trial_counter = 0

        # F5: 조기 종료 제어 플래그 & insertion_event 구독
        # 환경변수 AIC_F5_ENABLED="0"이면 비활성화 (baseline 측정용)
        self._f5_enabled = os.environ.get("AIC_F5_ENABLED", "1").strip() not in ("0", "false", "False", "")
        self._insertion_complete = False  # 매 trial 시작 시 리셋 (중요)
        self._insertion_complete_source = None  # "insertion_event"
        self._insertion_event_sub = parent_node.create_subscription(
            String,
            "/scoring/insertion_event",
            self._on_insertion_event,
            10,
        )
        status = "enabled" if self._f5_enabled else "DISABLED (baseline mode)"
        self.get_logger().info(
            f"[CollectWrapper] F5 early-termination {status} (insertion_event only)"
        )

    def _on_insertion_event(self, msg):
        """시뮬레이터가 케이블 삽입 완료를 알리는 토픽 콜백."""
        self._insertion_complete = True
        self._insertion_complete_source = "insertion_event"
        self.get_logger().info(
            f"[CollectWrapper] 삽입 완료 신호 수신 (insertion_event): {msg.data}"
        )

    # =========================================================================
    # 데이터 수집 유틸리티
    # =========================================================================

    def _init_episode(self, task: Task):
        ep_name = f"episode_{self._episode_counter:04d}"
        self._ep_dir = self._save_dir / ep_name
        (self._ep_dir / "images" / "left").mkdir(parents=True, exist_ok=True)
        (self._ep_dir / "images" / "center").mkdir(parents=True, exist_ok=True)
        (self._ep_dir / "images" / "right").mkdir(parents=True, exist_ok=True)

        self._states = []
        self._actions = []
        self._wrenches = []
        self._joint_velocities = []
        self._joint_efforts = []
        self._timestamps = []
        self._step = 0

        # Trial 실행 시간 측정 (F5 Primary 지표 P2)
        # insert_cable 진입 시각을 기록하고, _save_episode에서 종료 시각과 차를 계산
        self._trial_start_time = time.time()

        self._trial_counter += 1
        self._task_meta = {
            "episode_id": self._episode_counter,
            "cable_name": task.cable_name,
            "cable_type": task.cable_type,
            "plug_name": task.plug_name,
            "plug_type": task.plug_type,
            "port_name": task.port_name,
            "port_type": task.port_type,
            "target_module": task.target_module_name,
            "trial": self._trial_counter,
            "inner_policy": os.environ.get("AIC_INNER_POLICY", "RunACTHybrid"),
        }

        # scene pose 읽기
        self._read_scene_poses(task)

        self.get_logger().info(f"[CollectWrapper] Episode {self._episode_counter} started: {ep_name} (trial {self._trial_counter})")

    def _read_scene_poses(self, task: Task):
        tf_frames = {
            "task_board": "task_board/task_board_base_link",
            "target_module": f"task_board/{task.target_module_name}/{task.port_name}_link",
        }
        for key, frame in tf_frames.items():
            try:
                tf_stamped = self._parent_node._tf_buffer.lookup_transform("base_link", frame, Time())
                t = tf_stamped.transform.translation
                r = tf_stamped.transform.rotation
                self._task_meta[f"{key}_pose"] = {
                    "x": float(t.x), "y": float(t.y), "z": float(t.z),
                    "qx": float(r.x), "qy": float(r.y), "qz": float(r.z), "qw": float(r.w),
                }
            except TransformException:
                self.get_logger().warn(f"[CollectWrapper] Could not read TF for {frame}")

    def _record_step(self, obs: Observation, action_pose: Pose):
        # 카메라 이미지 (PNG)
        for cam_name, img_msg in [("left", obs.left_image), ("center", obs.center_image), ("right", obs.right_image)]:
            img_np = np.frombuffer(img_msg.data, dtype=np.uint8).reshape(img_msg.height, img_msg.width, 3)
            img_path = self._ep_dir / "images" / cam_name / f"{self._step:04d}.png"
            cv2.imwrite(str(img_path), cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR))

        # 로봇 상태 (26D)
        tcp_pose = obs.controller_state.tcp_pose
        tcp_vel = obs.controller_state.tcp_velocity
        state = np.array([
            tcp_pose.position.x, tcp_pose.position.y, tcp_pose.position.z,
            tcp_pose.orientation.x, tcp_pose.orientation.y, tcp_pose.orientation.z, tcp_pose.orientation.w,
            tcp_vel.linear.x, tcp_vel.linear.y, tcp_vel.linear.z,
            tcp_vel.angular.x, tcp_vel.angular.y, tcp_vel.angular.z,
            *obs.controller_state.tcp_error,
            *obs.joint_states.position[:7],
        ], dtype=np.float32)
        self._states.append(state)

        # Action (7D)
        action = np.array([
            action_pose.position.x, action_pose.position.y, action_pose.position.z,
            action_pose.orientation.x, action_pose.orientation.y, action_pose.orientation.z, action_pose.orientation.w,
        ], dtype=np.float32)
        self._actions.append(action)

        # F/T 센서 (6D)
        w = obs.wrist_wrench.wrench
        wrench = np.array([
            w.force.x, w.force.y, w.force.z,
            w.torque.x, w.torque.y, w.torque.z,
        ], dtype=np.float32)
        self._wrenches.append(wrench)

        # Joint velocity, effort (7D each)
        jv = list(obs.joint_states.velocity[:7]) if obs.joint_states.velocity else [0.0] * 7
        je = list(obs.joint_states.effort[:7]) if obs.joint_states.effort else [0.0] * 7
        self._joint_velocities.append(np.array(jv, dtype=np.float32))
        self._joint_efforts.append(np.array(je, dtype=np.float32))

        self._timestamps.append(time.time())
        self._step += 1

    def _save_episode(self, success: bool):
        if self._step == 0:
            self.get_logger().warn(f"[CollectWrapper] Episode {self._episode_counter} empty — skipping save")
            return

        np.save(str(self._ep_dir / "states.npy"), np.array(self._states))
        np.save(str(self._ep_dir / "actions.npy"), np.array(self._actions))
        np.save(str(self._ep_dir / "wrenches.npy"), np.array(self._wrenches))
        np.save(str(self._ep_dir / "joint_velocities.npy"), np.array(self._joint_velocities))
        np.save(str(self._ep_dir / "joint_efforts.npy"), np.array(self._joint_efforts))
        np.save(str(self._ep_dir / "timestamps.npy"), np.array(self._timestamps))

        self._task_meta["success"] = success
        self._task_meta["num_steps"] = self._step
        self._task_meta["duration_sec"] = self._timestamps[-1] - self._timestamps[0] if self._timestamps else 0
        # Trial 실행 시간: insert_cable 진입~_save_episode 호출 시점까지 (F5 P2)
        self._task_meta["trial_duration_sec"] = round(time.time() - self._trial_start_time, 3)
        with open(self._ep_dir / "metadata.json", "w") as f:
            json.dump(self._task_meta, f, indent=2)

        self.get_logger().info(
            f"[CollectWrapper] Episode {self._episode_counter} saved: "
            f"{self._step} steps, success={success}, dir={self._ep_dir}"
        )
        self._episode_counter += 1

    # =========================================================================
    # Policy 인터페이스 — inner policy에 위임 + 데이터 수집
    # =========================================================================

    def insert_cable(self, task, get_observation, move_robot, send_feedback, **kwargs):
        self._init_episode(task)

        # F5: 매 trial 시작 시 조기 종료 플래그 리셋 (이전 trial 이월 방지)
        self._insertion_complete = False
        self._insertion_complete_source = None

        # 마지막으로 관찰한 obs와 action을 캡처하기 위한 래퍼
        last_obs = [None]
        last_action = [None]

        original_get_obs = get_observation

        def recording_get_observation():
            obs = original_get_obs()
            if obs is not None:
                last_obs[0] = obs
                # 이전 action이 있으면 기록
                if last_action[0] is not None:
                    self._record_step(obs, last_action[0])

            # F5: 조기 종료 조건 체크 (AIC_F5_ENABLED=0이면 건너뜀)
            # insertion_event 토픽 수신 시 즉시 탈출 (유일한 신호)
            if self._f5_enabled and self._insertion_complete:
                raise _InsertionCompleteSignal(
                    self._insertion_complete_source or "insertion_event"
                )

            return obs

        original_move_robot = move_robot

        def recording_move_robot(motion_update):
            # action(pose target) 캡처
            if hasattr(motion_update, 'pose') and motion_update.pose:
                last_action[0] = motion_update.pose
            elif hasattr(motion_update, 'target_pose'):
                last_action[0] = motion_update.target_pose
            return original_move_robot(motion_update)

        # inner policy 실행
        early_terminated = False
        early_term_reason = None
        try:
            result = self._inner.insert_cable(
                task, recording_get_observation, recording_move_robot, send_feedback, **kwargs
            )
            # inner policy의 반환값 대신 TF 기반 삽입 판정
            success = self._check_insertion_success(task)
        except _InsertionCompleteSignal as ex:
            # F5: 조기 종료 — 삽입 완료 감지
            early_terminated = True
            early_term_reason = str(ex)
            self.get_logger().info(
                f"[CollectWrapper] 조기 종료 (F5): source={early_term_reason}"
            )
            # 삽입이 완료됐다고 감지됐으므로 성공으로 간주
            # (TF 기반 재판정은 보조 확인)
            success = self._check_insertion_success(task) or True
            result = True
        except Exception as e:
            self.get_logger().error(f"[CollectWrapper] Inner policy error: {e}")
            self._save_episode(success=False)
            return False

        # 조기 종료 메타 기록 (있으면)
        self._task_meta["early_terminated"] = early_terminated
        if early_terminated:
            self._task_meta["early_term_source"] = early_term_reason

        self._save_episode(success=success)
        return result

    def _check_insertion_success(self, task: Task, threshold: float = 0.02) -> bool:
        """TF에서 plug-port 거리를 측정하여 삽입 성공 여부를 판정한다.

        Args:
            task: 현재 태스크 정보
            threshold: 삽입 성공 거리 임계값 (m). 기본 20mm 이하면 성공.

        Returns:
            True if plug-port distance <= threshold
        """
        plug_frame = f"{task.cable_name}/{task.plug_name}_link"
        port_frame = f"task_board/{task.target_module_name}/{task.port_name}_link"

        try:
            tf_stamped = self._parent_node._tf_buffer.lookup_transform(port_frame, plug_frame, Time())
            t = tf_stamped.transform.translation
            distance = (t.x**2 + t.y**2 + t.z**2) ** 0.5
            self._task_meta["plug_port_distance"] = round(float(distance), 4)
            success = distance <= threshold
            self.get_logger().info(
                f"[CollectWrapper] Insertion check: distance={distance:.4f}m, "
                f"threshold={threshold}m, success={success}"
            )
            return success
        except TransformException as ex:
            self.get_logger().warn(f"[CollectWrapper] Could not check insertion: {ex}")
            self._task_meta["plug_port_distance"] = None
            return False
