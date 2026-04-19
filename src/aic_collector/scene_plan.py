"""
Scene plan DTO — Producer/Consumer 통합 수집기의 공통 중간 표현.

`ScenePlan`은 config 파일 1개에 대응하며, 1개 이상의 `TrialPlan`을 담는다.

- 기존 `TrainingSample`(sampler.py)이 1-trial 한정이었다면,
  `ScenePlan`은 `trials_per_config=1` (학습용)과 `=3` (평가용)을 모두 표현.
- Producer(sampler/builder)가 만들고 Consumer(엔진 실행기)가 소비.

Phase 1에서는 내부 리팩토링 용도. Phase 2a부터 파일 큐(`pending/`)의 단위.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

TaskType = Literal["sfp", "sc"]


@dataclass
class TrialPlan:
    """단일 trial 1개의 scene + task 서술.

    엔진 config의 `trials.trial_N` 한 항목을 만들기에 충분한 모든 정보를 담는다.
    """

    task_type: TaskType

    nic_rails: list[int] = field(default_factory=list)
    """활성 NIC rail 번호 목록 (오름차순). 길이 1~5."""

    nic_poses: dict[int, dict[str, float]] = field(default_factory=dict)
    """{rail_idx: {translation, yaw}} — nic_rails 각각에 대응."""

    sc_rails: list[int] = field(default_factory=list)
    """활성 SC rail 번호 목록. 길이 1~2."""

    sc_poses: dict[int, dict[str, float]] = field(default_factory=dict)
    """{rail_idx: {translation, yaw}} — sc_poses[r]['yaw']는 항상 0 (AIC 공식)."""

    target_rail: int = 0
    """타겟 rail 번호 (SFP: 0~4 / SC: 0~1). 반드시 활성 목록에 포함."""

    target_port_name: str = ""
    """타겟 port 이름 — 엔진의 tasks.task_N.port_name."""

    gripper: dict[str, float] = field(default_factory=dict)
    """{x, y, z, roll, pitch, yaw} — nominal ± 범위."""

    def to_dict(self) -> dict[str, Any]:
        """JSON 직렬화용 (int 키 → str)."""
        d = asdict(self)
        d["nic_poses"] = {str(k): v for k, v in self.nic_poses.items()}
        d["sc_poses"] = {str(k): v for k, v in self.sc_poses.items()}
        return d


@dataclass
class ScenePlan:
    """Config 1개 단위 — trial 1개 또는 3개를 담음.

    sample_index는 target cycling과 파일명(NNNN)에 공통 사용.
    seed는 per-sample RNG 파생 시드(동일 seed → 동일 출력).
    """

    sample_index: int
    seed: int
    trials: list[TrialPlan] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_index": self.sample_index,
            "seed": self.seed,
            "trials": [t.to_dict() for t in self.trials],
        }

    @property
    def primary_task_type(self) -> TaskType:
        """첫 trial의 task_type — 출력 경로(sfp/ vs sc/) 결정용.

        빈 ScenePlan은 유효하지 않다.
        """
        if not self.trials:
            raise ValueError("ScenePlan has no trials")
        return self.trials[0].task_type
