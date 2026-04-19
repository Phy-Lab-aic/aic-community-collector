---
name: aic-collect
description: >
  AIC 챌린지용 데이터(SFP/SC cable insertion)를 수집·생성할 때 사용. "학습 데이터 만들어줘",
  "SFP 100개 수집해줘", "평가 데이터 뽑아", "training data for cable insertion",
  "collect AIC samples", "새 policy 추가해줘", "수집 실패 원인 찾아줘",
  "worker 멈췄는데 뭐가 문제야" 같은 요청에 자동 활성화. 환경 점검 → 큐 생성 →
  워커 실행 → 결과 요약 전 과정과 policy 스캐폴딩·문제 진단을 책임진다.
---

# AIC Community Data Collector — 자동 수집 스킬

**목표**: 사용자가 세부 명령어/옵션을 몰라도 "N개 수집해줘" 한 마디로 전 과정이
돌아가게 한다. 에이전트가 환경 점검, 큐 적재, 워커 실행, 결과 집계, 문제 진단,
새 policy 스캐폴딩까지 담당한다.

---

## 0. 용어 한 줄 정리

- **Producer** = 큐에 config YAML을 적재 (scene/파라미터 랜덤화). 원래 Streamlit 작업 관리 탭이 하는 일.
- **Consumer** = pending/ 큐에서 하나씩 꺼내 시뮬레이터로 실행. `aic-collector-worker` CLI.
- **1 config = 1 trial** → 결과는 `~/aic_community_e2e/run_{tag}/` 평탄 구조.

---

## 1. 의도 확인 (20초)

다음을 사용자 요청에서 추출. **비어 있으면 한 번만** 묻는다. 표에 기본값이 있으면 묻지 않고 진행해도 됨.

| 필드 | 기본값 | 언제 물어야 하나 |
|---|---|---|
| `task` | 언급된 그것. 없으면 `sfp`+`sc` 둘 다 | "SFP만"/"SC만" 언급 시 확정 |
| `count` | **없음 — 반드시 확인** | SFP·SC 각각 몇 개? 또는 총 N개를 5:2로 분할 |
| `policy` | `cheatcode` | 사용자가 policy 이름 말했으면 그것 |
| `output_root` | `~/aic_community_e2e` | 사용자가 다른 경로 말했으면 |
| `ground_truth` | 학습용이면 `true`, 평가용이면 `false` | 의도 모호하면 물어봄 |
| `use_compressed` | `false` (raw ~58GB/run) | 디스크 걱정 시 `true` (3GB/run) 제안 |
| `collect_episode` | 학습 데이터 `true`, 평가 `false` | 보통 의도로 판단 가능 |
| `seed` | `42` | 재현 필요 시 명시 |

---

## 2. 환경 점검 (5초)

```bash
docker ps -a | grep aic_eval        # 컨테이너 존재
command -v distrobox                # 설치
test -d ~/ws_aic/src/aic && echo ok # pixi workspace
```

하나라도 실패하면 **수집 중단** 후 사용자에게 원인 설명. 자동 복구 가능한 건:

- pyaml/numpy/scipy 누락 → `uv sync` 제안
- `aic_eval` 컨테이너 없음 → `distrobox create --nvidia -i ghcr.io/intrinsic-dev/aic/aic_eval:latest aic_eval` 제안
- docker 그룹 미가입, pixi workspace 없음 → **사용자 개입 필요** (고칠 수 없음)

---

## 3. 큐에 config 적재 (Producer)

### 3.1 전체 cfg 스키마 (알아두면 모든 커스터마이징이 가능)

```python
cfg = {
    "training": {
        # --- Scene: 씬에 놓을 엔티티 개수·target 전략 ---
        "scene": {
            "nic_count_range": [1, 5],     # NIC 카드 개수 [lo, hi] (랜덤). [N, N]이면 고정.
            "sc_count_range":  [1, 2],     # SC 포트 개수.
            "target_cycling":  True,       # True: sample_index로 10/2종 target 결정적 순환(균등 분배).
                                           # False: 매 샘플 uniform 추첨(소량일 때 편향).
        },
        # --- Ranges: 파라미터 랜덤화 범위 (AIC 공식 허용 최대값이 기본) ---
        "ranges": {
            "nic_translation": [-0.0215, 0.0234],  # m   (기본=AIC 최대)
            "nic_yaw":         [-0.1745, 0.1745],  # rad (±10°, 기본=AIC 최대)
            "sc_translation":  [-0.06,   0.055],   # m   (기본=AIC 최대)
            "gripper_xy":      0.002,              # ±m  (scalar, nominal 기준 편차)
            "gripper_z":       0.002,              # ±m
            "gripper_rpy":     0.04,               # ±rad
        },
        # --- 샘플링 전략 (파라미터 연속값 분포) ---
        "param_strategy": "uniform",   # "uniform" | "lhs"
        # --- (보통 건드리지 않음) Gripper nominal 값 ---
        # "gripper_nominal": {"sfp": {...}, "sc": {...}},
    }
}
```

**핵심 규칙**:
- 슬라이더 한계 = **AIC 공식 허용 최대값**. 기본값을 그대로 쓰면 안전. 넘기면 채점 무효.
- `ranges` 값을 **좁히면** 제한적 환경 학습, **넓히면** AIC 한계.
- `target_cycling=True` + count가 10/2의 배수면 완벽 균등 분배.

### 3.2 자주 쓰는 커스터마이징 패턴

| 사용자 요청 | 오버라이드 |
|---|---|
| "쉬운 조건으로 먼저 수집" | `ranges.nic_translation: [-0.01, 0.01]` 등 절반 축소 |
| "AIC 최대 범위로 빡세게" | 기본값 그대로 (생략) |
| "NIC 카드 딱 3개씩 고정" | `scene.nic_count_range: [3, 3]` |
| "NIC 한 장짜리만" | `scene.nic_count_range: [1, 1]` |
| "Target 균등 분배 끄기 (진짜 랜덤)" | `scene.target_cycling: False` |
| "LHS로 더 고르게 뽑아" | `param_strategy: "lhs"` (scipy 필요) |
| "그리퍼 흔들림 크게" | `ranges.gripper_xy: 0.005, gripper_rpy: 0.08` |
| "baseline용 고정 씬" | Queue가 아니라 legacy sweep(`--config ...yaml`)이 적합 |

### 3.3 실행 스크립트 (이걸 그대로 사용자 요청값만 치환해 돌림)

```bash
uv run python - <<'PY'
from pathlib import Path
from aic_collector.sampler import sample_scenes
from aic_collector.job_queue import (
    ensure_queue_dirs, next_sample_index, write_plans
)

ROOT = Path("configs/train")
TEMPLATE = Path("configs/community_random_config.yaml")

# ↓ 사용자 요청에 맞춰 여기만 수정 ↓
cfg = {
    "training": {
        "param_strategy": "uniform",
        # "scene": {"nic_count_range": [3, 3]},
        # "ranges": {"nic_translation": [-0.01, 0.01]},
    },
}
SFP_COUNT = 50       # 0이면 SFP 생성 skip
SC_COUNT = 20        # 0이면 SC 생성 skip
BASE_SEED = 42
# ↑ 사용자 요청에 맞춰 여기만 수정 ↑

ensure_queue_dirs(ROOT)

for task, count in [("sfp", SFP_COUNT), ("sc", SC_COUNT)]:
    if count <= 0:
        continue
    start = next_sample_index(ROOT, task)
    plans = sample_scenes(cfg, task_type=task, count=count,
                          seed=BASE_SEED, start_index=start)
    paths = write_plans(plans, root=ROOT, template_path=TEMPLATE)
    print(f"[ok] {task.upper()} {len(paths)}개 pending/에 적재 (idx {start}~{start+count-1})")
PY
```

주의:
- **SFP 10의 배수, SC 2의 배수** 권장 (target 균등 분배). 어긋나면 한 줄만 경고 후 진행.
- `next_sample_index`가 전 상태 디렉토리를 스캔해 중복 인덱스 방지 — append 안전.
- `seed`는 고정으로 두어도 `start_index`가 달라 매 배치 고유 샘플 생성.

---

## 4. 워커 실행 (Consumer)

```bash
uv run aic-collector-worker \
    --root configs/train \
    --task all \
    --policy cheatcode \
    --output-root ~/aic_community_e2e \
    --ground-truth true \
    --use-compressed false \
    --collect-episode true \
    --timeout 300 \
    --recover
```

옵션 치트시트:

| 옵션 | 용도 |
|---|---|
| `--task {all,sfp,sc}` | 필터. `all`은 sfp→sc 순 |
| `--limit N` | 한 세션 최대 N개 (생략 = 큐 빌 때까지) |
| `--policy NAME` | 기본 policy. fallback용 |
| `--policy-sfp NAME` / `--policy-sc NAME` | task별 다른 policy |
| `--act-model-path PATH` | `act`/`hybrid` policy용 모델 체크포인트 |
| `--timeout SEC` | config당 최대 실행 시간. 초과 시 failed |
| `--recover` | 이전 비정상 종료로 `running/`에 남은 파일 복구 후 시작 |
| `--log PATH` | 엔진 로그 파일 (기본 `/tmp/aic_worker_run.log`) |

**실시간 상태**: 워커는 `/tmp/aic_worker_state.json`에 `processed/done/failed/current` 기록.
장시간 실행이면 사용자에게 **Prefect 대시보드** `http://localhost:4200` 안내.

---

## 5. 결과 집계

```bash
uv run python - <<'PY'
from pathlib import Path
from aic_collector.webapp import load_results, load_run_validations

OUT = Path.home() / "aic_community_e2e"
rows = load_results(OUT)
if not rows:
    print("수집된 결과 없음")
    raise SystemExit

total = len(rows)
ok = sum(1 for r in rows if r["success"] == "✅")
avg = sum(r["score"] for r in rows) / total
print(f"총 {total}개 trial | 성공 {ok} ({100*ok/total:.0f}%) | 평균 {avg:.1f}")

# 최근 실패
fails = [r for r in rows if r["success"] == "❌"][-5:]
if fails:
    print("\n최근 실패:")
    for r in fails:
        print(f"  {r['run']} (score {r['score']})")

warns = load_run_validations(OUT)
if warns:
    print(f"\n⚠️  검증 경고 있는 run {len(warns)}개")
PY
```

파일 위치: `~/aic_community_e2e/run_{timestamp}_{task}_{NNNN}/{bag,episode,tags.json,scoring_run.yaml,trial_scoring.yaml,validation.json}`.

---

## 6. 새 Policy 추가하기

사용자가 "내 policy 추가해줘", "VisionXxx라는 policy 만들어줘" 같이 요청하면.

### 최소 템플릿

```python
# policies/MyPolicy.py
from aic_model.policy import (
    GetObservationCallback,
    MoveRobotCallback,
    SendFeedbackCallback,
    Policy,
)
from aic_task_interfaces.msg import Task


class MyPolicy(Policy):
    """한 줄 설명."""

    def insert_cable(
        self,
        task: Task,
        get_observation: GetObservationCallback,
        move_robot: MoveRobotCallback,
        send_feedback: SendFeedbackCallback,
    ) -> bool:
        self.get_logger().info(f"MyPolicy.insert_cable(): {task}")
        # 1. task.target_module_name, task.port_name 등으로 목표 파악
        # 2. get_observation()으로 이미지/센서 읽기
        # 3. move_robot(MotionUpdate(...))로 로봇 제어
        # 4. 성공했다고 판단되면 True, 아니면 False 반환
        return True
```

### 등록·사용

1. 파일을 **`policies/`** 디렉토리에 두기 (클래스명과 파일명을 동일하게).
2. 자동 인식 — 워커 `--policy MyPolicy`에 이름으로 넘기면 된다.
3. 확인: `uv run python -c "from aic_collector.webapp import discover_policies; print(discover_policies())"`

### 참고 (기존 policies)

- `CheatCodeInner` / `CollectCheatCode`: GT 좌표 기반 베이스라인
- `RunACTv1` / `RunACTHybrid`: ACT 모델 추론 (체크포인트 필요)
- `VisionCheatCode`: DINOv2 비전 + force-feedback 삽입

---

## 7. 문제 진단 (증상 → 원인 → 로그 → 수정)

| 증상 | 가능한 원인 | 확인할 로그/파일 | 수정 |
|---|---|---|---|
| 워커 시작 "이미 실행 중" | 이전 세션이 PID 파일 남김 | `/tmp/aic_worker.pid` | `rm /tmp/aic_worker.pid /tmp/aic_worker_state.json` |
| `running/`에 파일 남음 | 비정상 종료 | `ls configs/train/{sfp,sc}/running/` | `aic-collector-worker --recover` |
| 모든 config가 timeout | 엔진 미기동 또는 policy 무한루프 | `/tmp/aic_worker_run.log` 끝부분 | 엔진 로그에서 에러 검색; timeout 늘려보기 |
| "Error: unable to find user e7217" | 컨테이너 초기화 미완료 | - | `distrobox enter aic_eval -- true` 한 번 실행 |
| 점수가 0 또는 1 계속 나옴 | Policy 로직 실패 또는 ground_truth 꺼짐 | `~/aic_community_e2e/run_*/trial_scoring.yaml` | `--ground-truth true` 로 확인; policy 코드 점검 |
| 디스크 공간 부족 | raw 이미지 축적 | `df -h ~` | `--use-compressed true` + 오래된 run 삭제 |
| 결과 탭이 비어 있음 | output_root 경로 불일치 | `ls ~/aic_community_e2e/` | 워커 `--output-root` 와 webapp 결과 탭 경로 일치 확인 |
| Prefect UI 접속 안 됨 | 서버 미기동 | `ps aux | grep prefect` | 첫 워커 시작 시 자동 뜸. 안 되면 `uv run prefect server start` 수동 |
| validation.json에 경고 다수 | scene 파라미터가 기대 범위 이탈 | `~/aic_community_e2e/run_*/validation.json` | cfg `ranges` 값이 AIC 범위 내인지 확인 |
| bag 파일 크기 <1KB | 엔진이 bag 기록 전 죽음 | `/tmp/aic_worker_run.log` | policy 실패로 조기 종료됐는지 확인 |

### 공통 로그 위치

| 파일 | 내용 |
|---|---|
| `/tmp/aic_worker_run.log` | 엔진 + policy 통합 stdout/stderr (append) |
| `/tmp/aic_worker.pid` | 실행 중인 워커 PID |
| `/tmp/aic_worker_state.json` | 실시간 상태 (processed/done/failed/current) |
| `~/.prefect/prefect.db` | Prefect run/task/artifact 메타 (SQLite) |
| `~/aic_community_e2e/run_*/validation.json` | run별 구조/크기 체크 결과 |

---

## 8. 하지 말아야 할 것

- **AIC 공식 범위 초과** — `ranges`에 허용 한계 밖 값 넣으면 채점 무효.
- **`configs/train/*`를 git에 커밋** — 런타임 데이터. `.gitignore`에 이미 포함.
- **워커 동작 중 `running/` 파일 수동 이동** — atomic claim 깨짐. 정지 후 작업.
- **`policies/` 파일만 추가하고 `insert_cable()` 미구현** — 런타임 AttributeError.
- **긴 수집을 foreground에서 돌리기** — 터미널 끊기면 워커도 죽음. `nohup` 또는 Streamlit UI 권장.

---

## 9. 빠른 참조

- `README.md` — 전반 소개
- `docs/usage-guide.md` — 단계별 상세
- `docs/config-reference.md` — YAML 항목·범위·규칙 완전 레퍼런스
- [AIC 공식 문서](https://github.com/intrinsic-dev/aic) — Task 구조·파라미터 근거
