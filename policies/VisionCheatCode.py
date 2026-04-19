"""CheatCode with vision-based port pose estimation + force-feedback insertion.

Replaces the ground-truth PORT TF lookup with DINOv2 vision prediction.
For the cable plug TF, tries TF lookup first (works under ground_truth:=true)
and falls back to gripper-relative synthesis using measured constants
(works under ground_truth:=false for SFP plugs; SC plug is dangling end so
synthesis is unreliable).

Insertion strategy (replaces CheatCode's open-loop descent):
  Phase A — CheatCode interpolation (vision target, 100 steps to ~20cm above port)
  Phase B — Force-aware descent + small spiral until contact (F_z > threshold)
  Phase C — At contact, spiral xy until force drops (= found hole)
  Phase D — Push down compliantly until insertion depth reached
Pattern adapted from RunACTHybrid (EXP-006, 157.9pt). Compliance is required
to absorb residual ~5-10mm vision error without generating excessive force.

Model: trained on v3-fixed labels (aic_vision_labels_v3.json) — proper
base_link frame with the 180° z-rotation correctly applied.
"""

import math
import time
import torch
import torch.nn as nn
from pathlib import Path
from torchvision import transforms
from PIL import Image
import numpy as np

from aic_model.policy import (
    GetObservationCallback,
    MoveRobotCallback,
    SendFeedbackCallback,
)
from aic_model_interfaces.msg import Observation
from aic_task_interfaces.msg import Task
from aic_control_interfaces.msg import MotionUpdate, TrajectoryGenerationMode
from geometry_msgs.msg import Point, Pose, Quaternion, Transform, Wrench, Vector3
from std_msgs.msg import Header
from rclpy.time import Time
from tf2_ros import TransformException
from transforms3d._gohlketransforms import (
    quaternion_from_euler,
    quaternion_multiply,
)

from aic_example_policies.ros.CheatCode import CheatCode


# Measured constants from LATE bag samples (last 30%, cable fully settled).
# SFP values from trial_1 + trial_2 bags, n=64, std <20mm.
# SC tip dangles even after grab — synthesis unreliable for SC.
PLUG_OFFSET_BY_NAME = {
    "sfp_tip": {
        "pos": [-0.0139, -0.0656, 0.1565],
        "rpy": [-0.3548, -0.0205, -0.0522],
    },
    "sc_tip": {
        "pos": [-0.0777, 0.5766, 0.0812],
        "rpy": [-0.3810, 0.0927, 1.5765],
    },
}


# --- Force-feedback insertion parameters (Phase B/C/D) ---
DESCENT_RATE = 0.0005          # 0.5mm per step
MAX_DESCENT = 0.10             # 10cm max descent in Phase B
CONTACT_FORCE = 5.0            # N (after tare)
HOLE_DROP_FORCE = 4.0          # N — phase C→D trigger (raised from 2.5; was too strict)
FORCE_THRESHOLD = 18.0         # N — safety retreat
INSERTION_DEPTH = 0.015        # m — phase D push depth
# Attempted tuning (2026-04-15):
#   - ff_force_z in Phase D: gripper inverted (roll=π) → "down" force
#     pushed plug UP away from port. Catastrophic (total=3). Disabled.
#   - INSERTION_DEPTH=25mm: marginal/no improvement over 15mm. Reverted.
PHASE_C_MAX_TIME = 10.0        # s — force B→D if Phase C stalls (hole maybe below plug)
SPIRAL_RADIUS_START = 0.001
SPIRAL_RADIUS_MAX_B = 0.005
SPIRAL_RADIUS_MAX_C = 0.010
SPIRAL_RADIUS_INC = 0.0002
SPIRAL_ANGULAR_STEP = 1.0
PHASE2_TIMEOUT = 30.0
LOOP_DT = 0.05


class PortPoseEstimator(nn.Module):
    """DINOv2 (frozen) + MLP → 4D pose. Must match training architecture."""

    def __init__(self):
        super().__init__()
        self.backbone = torch.hub.load(
            "facebookresearch/dinov2",
            "dinov2_vits14",
            verbose=False,
            trust_repo=True,
        )
        for p in self.backbone.parameters():
            p.requires_grad = False
        feat_dim = self.backbone.embed_dim  # 384

        self.head = nn.Sequential(
            nn.Linear(3 * feat_dim, 512),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(512, 256),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(256, 4),
        )

    def forward(self, images):
        feats = [self.backbone(images[:, i]) for i in range(3)]
        combined = torch.cat(feats, dim=1)
        return self.head(combined)


def ros_image_to_pil(ros_img) -> Image.Image:
    """Convert sensor_msgs/Image to PIL Image (RGB)."""
    if ros_img.encoding == "rgb8":
        img = Image.frombytes(
            "RGB", (ros_img.width, ros_img.height), bytes(ros_img.data)
        )
    elif ros_img.encoding == "bgr8":
        arr = np.frombuffer(ros_img.data, dtype=np.uint8).reshape(
            ros_img.height, ros_img.width, 3
        )
        img = Image.fromarray(arr[:, :, ::-1])
    else:
        arr = np.frombuffer(ros_img.data, dtype=np.uint8).reshape(
            ros_img.height, ros_img.width, -1
        )
        img = Image.fromarray(arr[:, :, :3])
    return img


def base_link_pose_to_transform(pose) -> Transform:
    """Convert model's [x, y, z, yaw] (base_link frame) to Transform message.

    Ports face upward with their local X axis flipped (roll≈180°), so we apply
    roll=π here. The model only predicts yaw; roll/pitch are assumed constant
    based on actual port TF inspection (SFP: roll=179.28°, SC: roll=-180°).
    """
    x, y, z, yaw = pose
    t = Transform()
    t.translation.x = float(x)
    t.translation.y = float(y)
    t.translation.z = float(z)
    q = quaternion_from_euler(math.pi, 0, float(yaw))  # (w, x, y, z)
    t.rotation.w = q[0]
    t.rotation.x = q[1]
    t.rotation.y = q[2]
    t.rotation.z = q[3]
    return t


def _compose_transforms(parent_t: Transform, child_pos, child_quat_wxyz) -> Transform:
    """Return child in world = parent * child_in_parent.

    parent_t: Transform (parent frame relative to world)
    child_pos: (x, y, z) tuple — child origin in parent frame
    child_quat_wxyz: (w, x, y, z) — child rotation in parent frame
    """
    pq = (
        parent_t.rotation.w,
        parent_t.rotation.x,
        parent_t.rotation.y,
        parent_t.rotation.z,
    )
    # Rotate child_pos by parent quaternion: pq * (0, child_pos) * pq_conj
    cv = (0.0,) + tuple(child_pos)
    pq_conj = (pq[0], -pq[1], -pq[2], -pq[3])
    rot_v = quaternion_multiply(quaternion_multiply(pq, cv), pq_conj)
    new_t = Transform()
    new_t.translation.x = parent_t.translation.x + rot_v[1]
    new_t.translation.y = parent_t.translation.y + rot_v[2]
    new_t.translation.z = parent_t.translation.z + rot_v[3]
    nq = quaternion_multiply(pq, child_quat_wxyz)
    new_t.rotation.w = nq[0]
    new_t.rotation.x = nq[1]
    new_t.rotation.y = nq[2]
    new_t.rotation.z = nq[3]
    return new_t


class _SyntheticPlugTf:
    """Mimics tf2 TransformStamped just enough for CheatCode.calc_gripper_pose."""

    def __init__(self, transform: Transform):
        self.transform = transform


class VisionCheatCode(CheatCode):
    """CheatCode with vision-based port localization + spiral search."""

    MODEL_PATH = "~/aic_vision_model/best_model.pt"

    def __init__(self, parent_node):
        super().__init__(parent_node)

        self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._vision_model = PortPoseEstimator().to(self._device)

        model_path = str(Path(self.MODEL_PATH).expanduser())
        checkpoint = torch.load(
            model_path, map_location=self._device, weights_only=False
        )
        state_dict = checkpoint.get("model_state_dict", checkpoint)
        self._vision_model.load_state_dict(state_dict)
        self._vision_model.eval()
        self.get_logger().info(f"Vision model loaded from {model_path}")

        self._transform = transforms.Compose(
            [
                transforms.Resize((224, 224)),
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
                ),
            ]
        )

    # ------------------------------------------------------------------
    # Vision

    def _predict_port_transform(self, observation: Observation) -> Transform:
        """Camera images → port pose in base_link frame → Transform message."""
        images = []
        for ros_img in [
            observation.left_image,
            observation.center_image,
            observation.right_image,
        ]:
            pil_img = ros_image_to_pil(ros_img)
            images.append(self._transform(pil_img))

        batch = torch.stack(images).unsqueeze(0).to(self._device)
        with torch.no_grad():
            pred = self._vision_model(batch)[0].cpu().tolist()  # [x, y, z, yaw]

        self.get_logger().info(
            f"Vision pred (base_link): "
            f"x={pred[0]:.4f} y={pred[1]:.4f} z={pred[2]:.4f} "
            f"yaw={pred[3]:.3f} ({np.degrees(pred[3]):.1f}°)"
        )
        return base_link_pose_to_transform(pred)

    # ------------------------------------------------------------------
    # Plug TF: lookup or synthesize

    def _lookup_or_synthesize_plug_tf(self):
        """Return TransformStamped-like for plug in base_link.

        Tries /tf lookup first (available under ground_truth:=true via the
        scoring/tf relay). Falls back to gripper TF + measured local offset
        (from LATE bag samples; SFP reliable, SC unreliable).
        """
        plug_frame = f"{self._task.cable_name}/{self._task.plug_name}_link"
        try:
            return self._parent_node._tf_buffer.lookup_transform(
                "base_link", plug_frame, Time()
            )
        except TransformException:
            pass

        offset = PLUG_OFFSET_BY_NAME.get(self._task.plug_name)
        if offset is None:
            self.get_logger().error(
                f"No plug offset constant for {self._task.plug_name!r}"
            )
            raise

        gripper_tf = self._parent_node._tf_buffer.lookup_transform(
            "base_link", "gripper/tcp", Time()
        )
        plug_quat_wxyz = quaternion_from_euler(*offset["rpy"])
        synth = _compose_transforms(
            gripper_tf.transform, offset["pos"], plug_quat_wxyz
        )
        return _SyntheticPlugTf(synth)

    def calc_gripper_pose(
        self,
        port_transform,
        slerp_fraction=1.0,
        position_fraction=1.0,
        z_offset=0.1,
        reset_xy_integrator=False,
    ):
        """Override CheatCode.calc_gripper_pose: same logic, but plug TF
        comes from `_lookup_or_synthesize_plug_tf()` instead of a hard
        TF lookup that would fail under ground_truth:=false.
        """
        from transforms3d._gohlketransforms import quaternion_slerp

        q_port = (
            port_transform.rotation.w,
            port_transform.rotation.x,
            port_transform.rotation.y,
            port_transform.rotation.z,
        )

        plug_tf_stamped = self._lookup_or_synthesize_plug_tf()
        q_plug = (
            plug_tf_stamped.transform.rotation.w,
            plug_tf_stamped.transform.rotation.x,
            plug_tf_stamped.transform.rotation.y,
            plug_tf_stamped.transform.rotation.z,
        )
        q_plug_inv = (-q_plug[0], q_plug[1], q_plug[2], q_plug[3])
        q_diff = quaternion_multiply(q_port, q_plug_inv)

        gripper_tf_stamped = self._parent_node._tf_buffer.lookup_transform(
            "base_link", "gripper/tcp", Time()
        )
        q_gripper = (
            gripper_tf_stamped.transform.rotation.w,
            gripper_tf_stamped.transform.rotation.x,
            gripper_tf_stamped.transform.rotation.y,
            gripper_tf_stamped.transform.rotation.z,
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
            self._tip_x_error_integrator = float(np.clip(
                self._tip_x_error_integrator + tip_x_error,
                -self._max_integrator_windup,
                self._max_integrator_windup,
            ))
            self._tip_y_error_integrator = float(np.clip(
                self._tip_y_error_integrator + tip_y_error,
                -self._max_integrator_windup,
                self._max_integrator_windup,
            ))

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
                w=q_gripper_slerp[0],
                x=q_gripper_slerp[1],
                y=q_gripper_slerp[2],
                z=q_gripper_slerp[3],
            ),
        )

    # ------------------------------------------------------------------
    # Force-feedback insertion (replaces CheatCode's open-loop descent)

    @staticmethod
    def _get_force(obs):
        w = obs.wrist_wrench.wrench
        return np.array([w.force.x, w.force.y, w.force.z])

    def _send_compliant_pose(
        self, move_robot, pose, stiffness_xy, stiffness_z, stiffness_rot,
        damping_factor=0.5, ff_force_z=0.0,
    ):
        """Compliant pose command via MotionUpdate with custom stiffness.

        ff_force_z: feedforward downward force in N (negative for downward
        push since z+ is up). Useful in Phase D for pushing plug into hole.
        """
        stiffness = np.diag([
            stiffness_xy, stiffness_xy, stiffness_z,
            stiffness_rot, stiffness_rot, stiffness_rot,
        ]).flatten()
        damping = np.diag([
            stiffness_xy * damping_factor, stiffness_xy * damping_factor,
            stiffness_z * damping_factor,
            stiffness_rot * damping_factor, stiffness_rot * damping_factor,
            stiffness_rot * damping_factor,
        ]).flatten()
        motion_update = MotionUpdate(
            header=Header(
                frame_id="base_link",
                stamp=self._parent_node.get_clock().now().to_msg(),
            ),
            pose=pose,
            target_stiffness=stiffness,
            target_damping=damping,
            feedforward_wrench_at_tip=Wrench(
                force=Vector3(x=0.0, y=0.0, z=-ff_force_z),  # negative = down
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

    def _force_insertion(self, port_transform, get_observation, move_robot, send_feedback):
        """Phase B/C/D: descend with spiral until contact, then find hole, then push.

        Returns True if Phase D completed (likely insertion).
        """
        # Anchor xy on predicted port. Keep the orientation that calc_gripper_pose
        # produced (read live from gripper TF).
        try:
            grip = self._parent_node._tf_buffer.lookup_transform(
                "base_link", "gripper/tcp", Time()
            )
        except TransformException as ex:
            self.get_logger().warn(f"Force insertion skipped (gripper TF): {ex}")
            return False

        # Anchor Phase B on predicted port xy directly. (Earlier tried to
        # shift by gripper-plug offset, but for SC cables the plug is the
        # dangling 58cm-away end — offsetting the anchor sends Phase B
        # miles off. The gripper itself is near port after Phase A, so just
        # use port xy.)
        cx = port_transform.translation.x
        cy = port_transform.translation.y
        z_current = grip.transform.translation.z
        q = grip.transform.rotation
        ori = Quaternion(x=q.x, y=q.y, z=q.z, w=q.w)
        self.get_logger().info(
            f"[Phase B prep] port=({cx:.4f},{cy:.4f}) "
            f"gripper=({grip.transform.translation.x:.4f},{grip.transform.translation.y:.4f}) "
            f"z={z_current:.4f}"
        )

        # Tare F/T baseline
        obs = get_observation()
        if obs is None:
            self.get_logger().warn("Force insertion: no observation for tare")
            return False
        force_baseline = self._get_force(obs)
        self.get_logger().info(
            f"[Tare] baseline F=[{force_baseline[0]:.1f},{force_baseline[1]:.1f},"
            f"{force_baseline[2]:.1f}]N |F|={np.linalg.norm(force_baseline):.1f}"
        )

        z_start = z_current
        radius = SPIRAL_RADIUS_START
        angle = 0.0
        phase = "B"
        contact_z = None
        phase_c_start = None
        start_time = time.time()

        self.get_logger().info(
            f"[Phase B] start at xy=({cx:.4f},{cy:.4f}) z={z_current:.4f}"
        )

        while time.time() - start_time < PHASE2_TIMEOUT:
            obs = get_observation()
            if obs is None:
                continue
            force = self._get_force(obs) - force_baseline
            f_z = abs(force[2])
            f_mag = float(np.linalg.norm(force))

            # Safety retreat
            if f_mag > FORCE_THRESHOLD:
                self.get_logger().warn(
                    f"[Phase {phase}] safety retreat |F|={f_mag:.1f}N"
                )
                z_current += 0.001
                phase = "B"
                contact_z = None

            elif phase == "B":
                z_current -= DESCENT_RATE
                search_x = cx + radius * math.cos(angle)
                search_y = cy + radius * math.sin(angle)
                angle += SPIRAL_ANGULAR_STEP
                if radius < SPIRAL_RADIUS_MAX_B:
                    radius += SPIRAL_RADIUS_INC
                if abs(z_start - z_current) > MAX_DESCENT:
                    self.get_logger().info(
                        f"[Phase B] max descent reached "
                        f"({abs(z_start - z_current)*1000:.0f}mm) — no contact, exit"
                    )
                    return False
                if f_z > CONTACT_FORCE:
                    contact_z = z_current
                    cx, cy = search_x, search_y
                    phase = "C"
                    phase_c_start = time.time()
                    radius = SPIRAL_RADIUS_START
                    angle = 0.0
                    self.get_logger().info(
                        f"[B→C] contact F_z={f_z:.1f}N at z={z_current:.4f} "
                        f"xy=({cx:.4f},{cy:.4f})"
                    )
                pose = Pose(position=Point(x=search_x, y=search_y, z=z_current), orientation=ori)
                self._send_compliant_pose(
                    move_robot, pose,
                    stiffness_xy=300.0, stiffness_z=100.0, stiffness_rot=50.0,
                    damping_factor=0.6,
                )

            elif phase == "C":
                spiral_x = cx + radius * math.cos(angle)
                spiral_y = cy + radius * math.sin(angle)
                z_current -= DESCENT_RATE * 0.2
                angle += SPIRAL_ANGULAR_STEP
                if radius < SPIRAL_RADIUS_MAX_C:
                    radius += SPIRAL_RADIUS_INC
                phase_c_elapsed = time.time() - phase_c_start if phase_c_start else 0
                if f_z < HOLE_DROP_FORCE:
                    cx, cy = spiral_x, spiral_y
                    phase = "D"
                    # NOTE (2026-04-15): contact_z reset was tried here —
                    # GT=true 평균 138→140 + trial_1 insertion 15%→60%
                    # but GT=false 115→82 (synthesis 부정확할 때 강제 push가
                    # 엉뚱한 곳으로 가 -30점). 실전(GT=false)이 더 중요.
                    self.get_logger().info(
                        f"[C→D] hole found! F_z={f_z:.1f}N at xy=({cx:.4f},{cy:.4f})"
                    )
                elif phase_c_elapsed > PHASE_C_MAX_TIME:
                    # Spiraling didn't find force drop — try pushing through
                    # anyway. Reset contact_z to current so D pushes a fresh
                    # 15mm (not instantly exiting because we already descended
                    # during Phase C).
                    phase = "D"
                    contact_z = z_current
                    self.get_logger().info(
                        f"[C→D] timeout ({phase_c_elapsed:.1f}s), force push from xy=({cx:.4f},{cy:.4f})"
                    )
                pose = Pose(position=Point(x=spiral_x, y=spiral_y, z=z_current), orientation=ori)
                self._send_compliant_pose(
                    move_robot, pose,
                    stiffness_xy=200.0, stiffness_z=100.0, stiffness_rot=30.0,
                    damping_factor=0.7,
                )

            elif phase == "D":
                z_current -= DESCENT_RATE
                if contact_z is not None and abs(contact_z - z_current) > INSERTION_DEPTH:
                    self.get_logger().info(
                        f"[Phase D] insertion depth reached "
                        f"({abs(contact_z - z_current)*1000:.0f}mm)"
                    )
                    return True
                pose = Pose(position=Point(x=cx, y=cy, z=z_current), orientation=ori)
                self._send_compliant_pose(
                    move_robot, pose,
                    stiffness_xy=500.0, stiffness_z=50.0,
                    stiffness_rot=50.0, damping_factor=0.5,
                )

            send_feedback(f"Phase {phase} z={z_current:.4f} Fz={f_z:.1f}")
            time.sleep(LOOP_DT)

        self.get_logger().info(f"[Phase {phase}] timeout")
        return phase == "D"

    # ------------------------------------------------------------------
    # Main entry

    def insert_cable(
        self,
        task: Task,
        get_observation: GetObservationCallback,
        move_robot: MoveRobotCallback,
        send_feedback: SendFeedbackCallback,
    ):
        self.get_logger().info(f"VisionCheatCode.insert_cable() task: {task}")
        self._task = task

        # Vision prediction — replaces the ground-truth port TF lookup.
        observation = get_observation()
        self.get_logger().info(
            f"Observation: left_image {observation.left_image.width}x"
            f"{observation.left_image.height} encoding={observation.left_image.encoding}"
        )
        port_transform = self._predict_port_transform(observation)

        # Phase A — CheatCode-style interpolation to ~20cm above port.
        z_offset = 0.2
        for t in range(0, 100):
            interp_fraction = t / 100.0
            try:
                self.set_pose_target(
                    move_robot=move_robot,
                    pose=self.calc_gripper_pose(
                        port_transform,
                        slerp_fraction=interp_fraction,
                        position_fraction=interp_fraction,
                        z_offset=z_offset,
                        reset_xy_integrator=True,
                    ),
                )
            except TransformException as ex:
                self.get_logger().warn(f"TF lookup failed during interpolation: {ex}")
            self.sleep_for(0.05)

        # Phase B/C/D — force-feedback descent + spiral + insertion.
        # Note: 3-attempt retry with 3mm offsets was tried (2026-04-15)
        # but regressed to mean 115 (vs 125 single-attempt) due to
        # duration bonus loss. Retries don't help much at this vision
        # accuracy level — first attempt is the best shot.
        self._force_insertion(port_transform, get_observation, move_robot, send_feedback)

        self.get_logger().info("Waiting for connector to stabilize...")
        self.sleep_for(2.0)

        self.get_logger().info("VisionCheatCode.insert_cable() exiting...")
        return True
