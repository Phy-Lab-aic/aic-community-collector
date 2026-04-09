#
#  Data Collection Policy — wraps CheatCode to record observation+action pairs.
#
#  Usage:
#    Terminal 1: /entrypoint.sh ground_truth:=true start_aic_engine:=true
#    Terminal 2: cd ~/ws_aic/src/aic && pixi run ros2 run aic_model aic_model \
#      --ros-args -p use_sim_time:=true -p policy:=aic_example_policies.ros.CollectCheatCode
#
#  Saves episodes to ~/aic_demos/ in LeRobot-compatible structure.
#

import os
import time
import json
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
from geometry_msgs.msg import Point, Pose, Quaternion, Transform
from rclpy.duration import Duration
from rclpy.time import Time
from std_msgs.msg import String
from tf2_ros import TransformException
from transforms3d._gohlketransforms import quaternion_multiply, quaternion_slerp

QuaternionTuple = tuple[float, float, float, float]


class CollectCheatCode(Policy):
    """CheatCode + 데이터 수집. 매 timestep의 observation과 action을 저장한다.

    F5 (EXP-009): 삽입 완료 감지 시 루프를 조기 탈출.
      - `/scoring/insertion_event` 토픽 구독 (시뮬레이터 ground truth, 유일한 신호)
      - 환경변수 AIC_F5_ENABLED="0"이면 비활성화 (baseline 측정용)

    Note: 이전 버전은 TF plug-port 거리 폴백을 사용했으나, false positive로
          부분 삽입 상태에서 탈출하는 문제가 있어 제거됨 (EXP-009 검증).
    """

    def __init__(self, parent_node):
        self._tip_x_error_integrator = 0.0
        self._tip_y_error_integrator = 0.0
        self._max_integrator_windup = 0.05
        self._task = None

        # 저장 경로
        self._save_dir = Path(os.environ.get("AIC_DEMO_DIR", os.path.expanduser("~/aic_demos")))
        self._save_dir.mkdir(parents=True, exist_ok=True)

        # 에피소드 번호 자동 증가
        existing = [d for d in self._save_dir.iterdir() if d.is_dir() and d.name.startswith("episode_")]
        self._episode_counter = len(existing)

        super().__init__(parent_node)

        # F5: 조기 종료 플래그 & insertion_event 구독
        self._f5_enabled = os.environ.get("AIC_F5_ENABLED", "1").strip() not in ("0", "false", "False", "")
        self._insertion_complete = False
        self._insertion_complete_source = None
        self._insertion_event_sub = parent_node.create_subscription(
            String,
            "/scoring/insertion_event",
            self._on_insertion_event,
            10,
        )
        status = "enabled" if self._f5_enabled else "DISABLED (baseline mode)"
        self.get_logger().info(
            f"[CollectCheatCode] F5 early-termination {status} (insertion_event only)"
        )

    def _on_insertion_event(self, msg):
        """시뮬레이터가 케이블 삽입 완료를 알리는 토픽 콜백."""
        self._insertion_complete = True
        self._insertion_complete_source = "insertion_event"
        self.get_logger().info(
            f"[CollectCheatCode] 삽입 완료 신호 수신 (insertion_event): {msg.data}"
        )

    def _f5_should_terminate(self, task: Task) -> tuple[bool, str | None]:
        """F5 조기 종료 판단. AIC_F5_ENABLED=0이면 항상 False.

        insertion_event 토픽만 신뢰. TF 폴백은 false positive 때문에 제거됨.
        """
        if not self._f5_enabled:
            return False, None
        if self._insertion_complete:
            return True, self._insertion_complete_source or "insertion_event"
        return False, None

    # =========================================================================
    # 데이터 수집 유틸리티
    # =========================================================================

    def _init_episode(self, task: Task):
        """에피소드 디렉토리 생성 및 버퍼 초기화."""
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
        # insert_cable 진입 시각을 기록, _save_episode에서 차를 계산
        self._trial_start_time = time.time()

        self._task_meta = {
            "episode_id": self._episode_counter,
            "cable_name": task.cable_name,
            "plug_name": task.plug_name,
            "target_module": task.target_module_name,
            "port_name": task.port_name,
            "cable_type": task.cable_type,
            "plug_type": task.plug_type,
            "port_type": task.port_type,
        }

        # trial 번호 추적
        if not hasattr(self, "_trial_counter"):
            self._trial_counter = 0
        self._trial_counter += 1
        self._task_meta["trial"] = self._trial_counter

        # task board / target module 자세를 TF에서 읽기
        self._read_scene_poses(task)

        self.get_logger().info(f"[Collect] Episode {self._episode_counter} started: {ep_name} (trial {self._trial_counter})")

    def _read_scene_poses(self, task: Task):
        """TF에서 task board 및 target module의 자세를 읽어 메타데이터에 기록."""
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
                self.get_logger().warn(f"[Collect] Could not read TF for {frame}")

    def _record_step(self, obs: Observation, action_pose: Pose):
        """한 timestep의 observation과 action을 기록한다."""
        # 카메라 이미지 저장 (PNG)
        for cam_name, img_msg in [("left", obs.left_image), ("center", obs.center_image), ("right", obs.right_image)]:
            img_np = np.frombuffer(img_msg.data, dtype=np.uint8).reshape(img_msg.height, img_msg.width, 3)
            img_path = self._ep_dir / "images" / cam_name / f"{self._step:04d}.png"
            cv2.imwrite(str(img_path), cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR))

        # 로봇 상태 (RunACT와 동일한 26차원)
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

        # Action: TCP 목표 pose (7차원: x, y, z, qx, qy, qz, qw)
        action = np.array([
            action_pose.position.x, action_pose.position.y, action_pose.position.z,
            action_pose.orientation.x, action_pose.orientation.y, action_pose.orientation.z, action_pose.orientation.w,
        ], dtype=np.float32)
        self._actions.append(action)

        # F/T 센서 (6차원: fx, fy, fz, tx, ty, tz)
        w = obs.wrist_wrench.wrench
        wrench = np.array([
            w.force.x, w.force.y, w.force.z,
            w.torque.x, w.torque.y, w.torque.z,
        ], dtype=np.float32)
        self._wrenches.append(wrench)

        # Joint velocity, effort
        jv = list(obs.joint_states.velocity[:7]) if obs.joint_states.velocity else [0.0] * 7
        je = list(obs.joint_states.effort[:7]) if obs.joint_states.effort else [0.0] * 7
        self._joint_velocities.append(np.array(jv, dtype=np.float32))
        self._joint_efforts.append(np.array(je, dtype=np.float32))

        self._timestamps.append(time.time())
        self._step += 1

    def _save_episode(self, success: bool):
        """에피소드 데이터를 디스크에 저장한다."""
        # 상태, 행동, 센서 데이터를 numpy 배열로 저장
        np.save(str(self._ep_dir / "states.npy"), np.array(self._states))
        np.save(str(self._ep_dir / "actions.npy"), np.array(self._actions))
        np.save(str(self._ep_dir / "wrenches.npy"), np.array(self._wrenches))
        np.save(str(self._ep_dir / "joint_velocities.npy"), np.array(self._joint_velocities))
        np.save(str(self._ep_dir / "joint_efforts.npy"), np.array(self._joint_efforts))
        np.save(str(self._ep_dir / "timestamps.npy"), np.array(self._timestamps))

        # 메타데이터 저장
        self._task_meta["success"] = success
        self._task_meta["num_steps"] = self._step
        self._task_meta["duration_sec"] = self._timestamps[-1] - self._timestamps[0] if self._timestamps else 0
        # Trial 실행 시간: insert_cable 진입~_save_episode 호출 시점까지 (F5 P2)
        self._task_meta["trial_duration_sec"] = round(time.time() - self._trial_start_time, 3)
        with open(self._ep_dir / "metadata.json", "w") as f:
            json.dump(self._task_meta, f, indent=2)

        self.get_logger().info(
            f"[Collect] Episode {self._episode_counter} saved: "
            f"{self._step} steps, success={success}, "
            f"dir={self._ep_dir}"
        )
        self._episode_counter += 1

    # =========================================================================
    # CheatCode 로직 (원본에서 복사, 데이터 수집 코드 추가)
    # =========================================================================

    def _wait_for_tf(self, target_frame: str, source_frame: str, timeout_sec: float = 10.0) -> bool:
        start = self.time_now()
        timeout = Duration(seconds=timeout_sec)
        attempt = 0
        while (self.time_now() - start) < timeout:
            try:
                self._parent_node._tf_buffer.lookup_transform(target_frame, source_frame, Time())
                return True
            except TransformException:
                if attempt % 20 == 0:
                    self.get_logger().info(
                        f"Waiting for transform '{source_frame}' -> '{target_frame}'... "
                        "-- are you running eval with `ground_truth:=true`?"
                    )
                attempt += 1
                self.sleep_for(0.1)
        self.get_logger().error(f"Transform '{source_frame}' not available after {timeout_sec}s")
        return False

    def calc_gripper_pose(
        self, port_transform: Transform,
        slerp_fraction: float = 1.0, position_fraction: float = 1.0,
        z_offset: float = 0.1, reset_xy_integrator: bool = False,
    ) -> Pose:
        q_port = (
            port_transform.rotation.w, port_transform.rotation.x,
            port_transform.rotation.y, port_transform.rotation.z,
        )
        plug_tf_stamped = self._parent_node._tf_buffer.lookup_transform(
            "base_link", f"{self._task.cable_name}/{self._task.plug_name}_link", Time(),
        )
        q_plug = (
            plug_tf_stamped.transform.rotation.w, plug_tf_stamped.transform.rotation.x,
            plug_tf_stamped.transform.rotation.y, plug_tf_stamped.transform.rotation.z,
        )
        q_plug_inv = (-q_plug[0], q_plug[1], q_plug[2], q_plug[3])
        q_diff = quaternion_multiply(q_port, q_plug_inv)

        gripper_tf_stamped = self._parent_node._tf_buffer.lookup_transform("base_link", "gripper/tcp", Time())
        q_gripper = (
            gripper_tf_stamped.transform.rotation.w, gripper_tf_stamped.transform.rotation.x,
            gripper_tf_stamped.transform.rotation.y, gripper_tf_stamped.transform.rotation.z,
        )
        q_gripper_target = quaternion_multiply(q_diff, q_gripper)
        q_gripper_slerp = quaternion_slerp(q_gripper, q_gripper_target, slerp_fraction)

        gripper_xyz = (
            gripper_tf_stamped.transform.translation.x,
            gripper_tf_stamped.transform.translation.y,
            gripper_tf_stamped.transform.translation.z,
        )
        port_xy = (port_transform.translation.x, port_transform.translation.y)
        plug_xyz = (
            plug_tf_stamped.transform.translation.x,
            plug_tf_stamped.transform.translation.y,
            plug_tf_stamped.transform.translation.z,
        )
        plug_tip_gripper_offset = (
            gripper_xyz[0] - plug_xyz[0],
            gripper_xyz[1] - plug_xyz[1],
            gripper_xyz[2] - plug_xyz[2],
        )

        tip_x_error = port_xy[0] - plug_xyz[0]
        tip_y_error = port_xy[1] - plug_xyz[1]

        if reset_xy_integrator:
            self._tip_x_error_integrator = 0.0
            self._tip_y_error_integrator = 0.0
        else:
            self._tip_x_error_integrator = np.clip(
                self._tip_x_error_integrator + tip_x_error,
                -self._max_integrator_windup, self._max_integrator_windup,
            )
            self._tip_y_error_integrator = np.clip(
                self._tip_y_error_integrator + tip_y_error,
                -self._max_integrator_windup, self._max_integrator_windup,
            )

        i_gain = 0.15
        target_x = port_xy[0] + i_gain * self._tip_x_error_integrator
        target_y = port_xy[1] + i_gain * self._tip_y_error_integrator
        target_z = port_transform.translation.z + z_offset - plug_tip_gripper_offset[2]

        blend_xyz = (
            position_fraction * target_x + (1.0 - position_fraction) * gripper_xyz[0],
            position_fraction * target_y + (1.0 - position_fraction) * gripper_xyz[1],
            position_fraction * target_z + (1.0 - position_fraction) * gripper_xyz[2],
        )

        return Pose(
            position=Point(x=blend_xyz[0], y=blend_xyz[1], z=blend_xyz[2]),
            orientation=Quaternion(
                w=q_gripper_slerp[0], x=q_gripper_slerp[1],
                y=q_gripper_slerp[2], z=q_gripper_slerp[3],
            ),
        )

    def insert_cable(
        self, task: Task,
        get_observation: GetObservationCallback,
        move_robot: MoveRobotCallback,
        send_feedback: SendFeedbackCallback,
    ):
        self.get_logger().info(f"CollectCheatCode.insert_cable() task: {task}")
        self._task = task
        self._init_episode(task)

        # F5: 매 trial 시작 시 플래그 리셋 (이전 trial 이월 방지)
        self._insertion_complete = False
        self._insertion_complete_source = None
        early_terminated = False
        early_term_reason = None

        port_frame = f"task_board/{task.target_module_name}/{task.port_name}_link"
        cable_tip_frame = f"{task.cable_name}/{task.plug_name}_link"

        for frame in [port_frame, cable_tip_frame]:
            if not self._wait_for_tf("base_link", frame):
                self._save_episode(success=False)
                return False

        try:
            port_tf_stamped = self._parent_node._tf_buffer.lookup_transform("base_link", port_frame, Time())
        except TransformException as ex:
            self.get_logger().error(f"Could not look up port transform: {ex}")
            self._save_episode(success=False)
            return False
        port_transform = port_tf_stamped.transform

        z_offset = 0.2

        # Phase 1: 포트 상공으로 이동 (보간)
        for t in range(0, 100):
            interp_fraction = t / 100.0
            try:
                pose = self.calc_gripper_pose(
                    port_transform,
                    slerp_fraction=interp_fraction,
                    position_fraction=interp_fraction,
                    z_offset=z_offset,
                    reset_xy_integrator=True,
                )
                self.set_pose_target(move_robot=move_robot, pose=pose)

                # 데이터 수집
                obs = get_observation()
                if obs is not None:
                    self._record_step(obs, pose)

            except TransformException as ex:
                self.get_logger().warn(f"TF lookup failed during interpolation: {ex}")
            self.sleep_for(0.05)

        # Phase 2: 하강하며 삽입
        while True:
            if z_offset < -0.015:
                break
            # F5: 삽입 완료 감지 시 루프 탈출
            terminated, reason = self._f5_should_terminate(task)
            if terminated:
                early_terminated = True
                early_term_reason = reason
                self.get_logger().info(
                    f"[CollectCheatCode] F5 조기 종료 (Phase 2): source={reason}"
                )
                break
            z_offset -= 0.0005
            try:
                pose = self.calc_gripper_pose(port_transform, z_offset=z_offset)
                self.set_pose_target(move_robot=move_robot, pose=pose)

                # 데이터 수집
                obs = get_observation()
                if obs is not None:
                    self._record_step(obs, pose)

            except TransformException as ex:
                self.get_logger().warn(f"TF lookup failed during insertion: {ex}")
            self.sleep_for(0.05)

        # F5: Phase 2에서 조기 종료됐으면 안정화 대기 건너뜀
        if not early_terminated:
            self.get_logger().info("Waiting for connector to stabilize...")
            self.sleep_for(5.0)

        # F5 메타 기록
        self._task_meta["early_terminated"] = early_terminated
        if early_terminated:
            self._task_meta["early_term_source"] = early_term_reason

        self._save_episode(success=True)
        self.get_logger().info("CollectCheatCode.insert_cable() exiting...")
        return True
