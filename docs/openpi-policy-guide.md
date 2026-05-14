# OpenPI Policy 수집 가이드

AIC Community Collector에서 OpenPI (pi05) 모델로 데이터를 수집하는 방법.

---

## 사전 조건

1. **rlinf + openpi 설치** (pixi 환경이 아닌 별도 venv 또는 conda)
   ```bash
   cd ~/ws_aic/RLinf
   pip install -e ".[embodied,openpi]"
   ```

2. **체크포인트 준비** — 둘 중 하나
   - HuggingFace 자동 다운로드 (최초 실행 시 자동): `Phy-lab/aic_cable_insert_sft_openpi_pi05_pretrained_v3_bs128_mb16_step11040`
   - 이미 캐시된 경우 심볼릭 링크 확인:
     ```bash
     ls checkpoints/pi05_aic_cable_insert_ur5e/global_step_11040
     ```
     깨진 경우 재생성:
     ```bash
     ln -sfn ~/.cache/huggingface/hub/models--Phy-lab--aic_cable_insert_sft_openpi_pi05_pretrained_v3_bs128_mb16_step11040/snapshots/*/global_step_11040 \
         checkpoints/pi05_aic_cable_insert_ur5e/global_step_11040
     ```

3. **Policy 배포**
   ```bash
   uv run python -c "from src.aic_collector.prefect.policy_env import deploy_policies; deploy_policies('.')"
   ```

---

## 기본 실행 흐름

### 1단계: config 생성 (pending/ 에 추가)

```bash
# SFP 10개 생성
uv run python - <<'PY'
import sys; sys.path.insert(0, "src")
from pathlib import Path
from aic_collector.sampler import sample_scenes
from aic_collector.job_queue.writer import write_plans, next_sample_index

root = Path("configs/train")
start = next_sample_index(root, "sfp")
plans = sample_scenes(
    cfg={"training": {"param_strategy": "uniform"}},
    task_type="sfp", count=10, seed=42, start_index=start,
)
paths = write_plans(plans, root, Path("configs/community_random_config.yaml"))
print(f"{len(paths)}개 생성, 마지막: {paths[-1]}")
PY
```

### 2단계: 워커 실행

```bash
uv run aic-collector-worker \
    --root configs/train \
    --task sfp \
    --limit 10 \
    --policy openpi \
    --policy-timeout 600 \
    --timeout 900 \
    --output-root ~/aic_data_openpi
```

> **`--policy-timeout 600` 필수** — 32GB 모델 최초 로딩에 ~4분 소요.
> `--timeout`은 전체 프로세스 킬 타임아웃이므로 `--policy-timeout`보다 크게 설정.

---

## 테스트 (1개만)

```bash
# config 1개 생성
uv run python - <<'PY'
import sys; sys.path.insert(0, "src")
from pathlib import Path
from aic_collector.sampler import sample_scenes
from aic_collector.job_queue.writer import write_plans, next_sample_index

root = Path("configs/train")
start = next_sample_index(root, "sfp")
plans = sample_scenes(
    cfg={"training": {"param_strategy": "uniform"}},
    task_type="sfp", count=1, seed=42, start_index=start,
)
paths = write_plans(plans, root, Path("configs/community_random_config.yaml"))
print(f"생성: {paths[0]}")
PY

# 워커 실행 (1개, 포그라운드)
uv run aic-collector-worker \
    --root configs/train \
    --task sfp \
    --limit 1 \
    --policy openpi \
    --policy-timeout 600 \
    --timeout 900 \
    --output-root ~/aic_data_openpi_test
```

---

## 환경변수 옵션

| 변수 | 기본값 | 설명 |
|---|---|---|
| `OPENPI_MODEL_PATH` | `checkpoints/pi05_aic_cable_insert_ur5e/global_step_11040` | 로컬 체크포인트 경로. 없으면 HF 자동 다운로드 |
| `OPENPI_CHECKPOINT` | `global_step_11040` | HF 다운로드 시 사용할 체크포인트 폴더명 |
| `OPENPI_PROMPT` | `"Insert the SFP-to-SC cable into the target port"` | 모델 태스크 프롬프트 |
| `OPENPI_ACTION_DIM` | `7` | action 차원 |
| `OPENPI_ACTION_HORIZON` | `5` | action chunk 크기 |
| `OPENPI_DEVICE` | `cuda` (가능 시) | `cuda` / `cpu` |

예시:
```bash
OPENPI_PROMPT="Insert cable" \
uv run aic-collector-worker --policy openpi --policy-timeout 600 ...
```

---

## 로그 확인

```bash
# 워커 전체 로그
tail -f /tmp/aic_worker_run.log

# ROS policy 로그 (모델 로딩 상태)
tail -f ~/.ros/log/$(ls -t ~/.ros/log/ | grep "^python_" | head -1)
```

정상 진행 시 ROS 로그에 아래 메시지가 순서대로 출력됩니다:
```
[RunOpenPI] Initialized (lazy). Model will load on first trial.
[RunOpenPI] Loading model from .../global_step_11040 onto cuda ...
[RunOpenPI] Model ready in 240.3s.
[RunOpenPI] insert_cable() start.
[RunOpenPI] New action chunk queried. step=0, first_action=[...]
[RunOpenPI] insert_cable() done. total_steps=N
```

---

## 소요 시간 (1 config 기준)

| 단계 | 시간 |
|---|---|
| Docker 재시작 + 엔진 기동 | ~2분 |
| 모델 로딩 (최초 1회) | ~4분 |
| insert_cable 실행 (30초 루프) | ~30초 |
| cleanup + postprocess | ~30초 |
| **합계** | **~7분** |

두 번째 config부터는 모델이 이미 로드된 상태이므로 **~3분**으로 단축됩니다.
(단, 워커를 종료하면 다시 로딩 필요)

---

## 파일 구조

```
aic-community-collector/
├── policies/
│   ├── RunOpenPI.py          ← AIC inner policy (이 파일 수정 시 배포 필요)
│   ├── aic_policy.py         ← AICInputs/AICOutputs 변환
│   ├── aic_dataconfig.py     ← LeRobotAICDataConfig
│   ├── _register.py          ← rlinf config 등록
│   └── assets/               ← norm_stats.json
└── checkpoints/
    └── pi05_aic_cable_insert_ur5e/
        └── global_step_11040 → (symlink → HF cache)
```

> policies/ 수정 후에는 반드시 배포 명령 재실행:
> ```bash
> uv run python -c "from src.aic_collector.prefect.policy_env import deploy_policies; deploy_policies('.')"
> ```
