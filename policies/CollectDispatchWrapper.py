#
#  CollectDispatchWrapper — trial별로 다른 inner policy를 위임하는 Dispatcher.
#
#  F2-b (EXP-009): 한 run 안에서 trial마다 다른 policy를 적용한다.
#  CollectWrapper를 기반으로 하되, trial 카운터에 따라 inner policy를 교체.
#
#  환경변수:
#    AIC_DEMO_DIR                — 저장 경로 (기본: ~/aic_demos)
#    AIC_INNER_POLICY_TRIAL_1    — trial 1용 inner policy 클래스 경로
#    AIC_INNER_POLICY_TRIAL_2    — trial 2용 inner policy 클래스 경로
#    AIC_INNER_POLICY_TRIAL_3    — trial 3용 inner policy 클래스 경로
#    AIC_INNER_POLICY            — 위 개별 지정이 없는 trial의 폴백 (기본: RunACTHybrid)
#    ACT_MODEL_PATH              — ACT 모델 경로 (inner policy가 ACT 사용 시)
#    AIC_F5_ENABLED              — F5 조기 종료 ("1"=on, "0"=off, 기본: on)
#
#  사용법:
#    AIC_DEMO_DIR=~/aic_community_e2e_demos \
#    AIC_INNER_POLICY_TRIAL_1=aic_example_policies.ros.CheatCode \
#    AIC_INNER_POLICY_TRIAL_2=aic_example_policies.ros.RunACTv1 \
#    AIC_INNER_POLICY_TRIAL_3=aic_example_policies.ros.RunACTHybrid \
#    ACT_MODEL_PATH=~/ws_aic/src/aic/outputs/train/.../pretrained_model \
#    pixi run ros2 run aic_model aic_model \
#      --ros-args -p use_sim_time:=true \
#      -p policy:=aic_example_policies.ros.CollectDispatchWrapper
#

import os
import importlib

from aic_model.policy import Policy
from aic_task_interfaces.msg import Task

# CollectWrapper를 상속하여 F5 + 데이터 수집 기능을 그대로 사용
from aic_example_policies.ros.CollectWrapper import CollectWrapper


def _load_policy_class(class_path: str):
    """'aic_example_policies.ros.RunACTHybrid' → 모듈 import + 클래스 반환."""
    class_name = class_path.rsplit(".", 1)[-1]
    module = importlib.import_module(class_path)
    return getattr(module, class_name)


class CollectDispatchWrapper(CollectWrapper):
    """Trial별로 다른 inner policy를 위임하는 Dispatcher.

    CollectWrapper를 상속하므로:
      - 데이터 수집 (PNG + npy + metadata.json)
      - F5 조기 종료 (/scoring/insertion_event)
      - trial_duration_sec 타임스탬프
    가 모두 자동 적용됨.

    trial 카운터에 따라 inner policy 인스턴스를 교체한다.
    """

    def __init__(self, parent_node):
        # CollectWrapper.__init__은 AIC_INNER_POLICY로 단일 inner를 로드한다.
        # 여기서는 그 로직을 override하지 않고, super().__init__ 후에
        # trial별 inner를 추가로 로드한다.

        # 폴백: AIC_INNER_POLICY (CollectWrapper 기본값)
        fallback_path = os.environ.get(
            "AIC_INNER_POLICY",
            "aic_example_policies.ros.RunACTHybrid",
        )

        # trial별 inner policy 로드
        self._trial_inners = {}
        for trial_num in (1, 2, 3):
            env_key = f"AIC_INNER_POLICY_TRIAL_{trial_num}"
            class_path = os.environ.get(env_key, fallback_path)
            try:
                cls = _load_policy_class(class_path)
                self._trial_inners[trial_num] = cls(parent_node)
            except Exception as e:
                # import 실패 시 로그만 남기고 폴백 사용
                # (super().__init__에서 로드한 _inner가 폴백)
                pass

        # super().__init__은 AIC_INNER_POLICY(폴백)로 _inner를 세팅
        super().__init__(parent_node)

        # 로드 결과 로그
        for trial_num, inner in self._trial_inners.items():
            self.get_logger().info(
                f"[DispatchWrapper] trial_{trial_num} → {type(inner).__name__}"
            )
        self.get_logger().info(
            f"[DispatchWrapper] fallback → {type(self._inner).__name__}"
        )

    def insert_cable(self, task, get_observation, move_robot, send_feedback, **kwargs):
        """trial 카운터에 따라 inner policy를 교체한 뒤 CollectWrapper.insert_cable 실행."""
        # _trial_counter는 CollectWrapper._init_episode에서 증가 (1-based).
        # insert_cable 진입 시점에는 아직 증가 전이므로 +1로 예측.
        next_trial = self._trial_counter + 1

        if next_trial in self._trial_inners:
            self._inner = self._trial_inners[next_trial]
            self.get_logger().info(
                f"[DispatchWrapper] trial {next_trial}: "
                f"inner → {type(self._inner).__name__}"
            )

        # CollectWrapper.insert_cable이 _init_episode → 수집 → F5 → _save_episode 모두 처리
        return super().insert_cable(task, get_observation, move_robot, send_feedback, **kwargs)
