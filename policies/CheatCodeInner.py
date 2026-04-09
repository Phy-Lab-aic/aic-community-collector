#
#  CheatCodeInner — CollectWrapper/DispatchWrapper의 inner로 사용 가능한 CheatCode.
#
#  원본 CheatCode는 get_observation()을 호출하지 않아 CollectWrapper의
#  recording 메커니즘이 작동하지 않는다. 이 클래스는 원본을 상속하고
#  insert_cable 내 루프에서 get_observation()을 호출하여 데이터 수집이
#  되도록 한다.
#
#  단독 사용 시에는 원본 CheatCode 또는 CollectCheatCode를 사용할 것.
#  이 클래스는 오직 CollectWrapper/DispatchWrapper의 inner로만 의미 있음.
#

from aic_example_policies.ros.CheatCode import CheatCode
from aic_model.policy import GetObservationCallback, MoveRobotCallback, SendFeedbackCallback
from aic_task_interfaces.msg import Task
from rclpy.time import Time
from tf2_ros import TransformException


class CheatCodeInner(CheatCode):
    """get_observation()을 호출하는 CheatCode. CollectWrapper inner 전용."""

    def insert_cable(
        self,
        task: Task,
        get_observation: GetObservationCallback,
        move_robot: MoveRobotCallback,
        send_feedback: SendFeedbackCallback,
    ):
        self.get_logger().info(f"CheatCodeInner.insert_cable() task: {task}")
        self._task = task

        port_frame = f"task_board/{task.target_module_name}/{task.port_name}_link"
        cable_tip_frame = f"{task.cable_name}/{task.plug_name}_link"

        for frame in [port_frame, cable_tip_frame]:
            if not self._wait_for_tf("base_link", frame):
                return False

        try:
            port_tf_stamped = self._parent_node._tf_buffer.lookup_transform(
                "base_link", port_frame, Time(),
            )
        except TransformException as ex:
            self.get_logger().error(f"Could not look up port transform: {ex}")
            return False
        port_transform = port_tf_stamped.transform

        z_offset = 0.2

        # Phase 1: 포트 상공으로 보간 이동
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
                get_observation()  # ← CollectWrapper recording 트리거
            except TransformException as ex:
                self.get_logger().warn(f"TF lookup failed during interpolation: {ex}")
            self.sleep_for(0.05)

        # Phase 2: 하강 삽입
        while True:
            if z_offset < -0.015:
                break
            z_offset -= 0.0005
            try:
                pose = self.calc_gripper_pose(port_transform, z_offset=z_offset)
                self.set_pose_target(move_robot=move_robot, pose=pose)
                get_observation()  # ← CollectWrapper recording 트리거
            except TransformException as ex:
                self.get_logger().warn(f"TF lookup failed during insertion: {ex}")
            self.sleep_for(0.05)

        self.get_logger().info("Waiting for connector to stabilize...")
        self.sleep_for(5.0)

        self.get_logger().info("CheatCodeInner.insert_cable() exiting...")
        return True
