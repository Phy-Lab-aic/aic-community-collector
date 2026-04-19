# 사용 가이드

AIC Community Data Collector의 상세 사용 흐름입니다. README만으로 부족할 때 참고하세요.

## 목차

1. [설치와 첫 실행](#설치와-첫-실행)
2. [환경 점검](#환경-점검)
3. [작업 관리 — 주문서 만들기](#작업-관리--주문서-만들기)
4. [작업 실행 — 워커 돌리기](#작업-실행--워커-돌리기)
5. [결과 확인](#결과-확인)
6. [고급: Prefect 대시보드](#고급-prefect-대시보드)
7. [CLI 사용](#cli-사용)
8. [문제 해결](#문제-해결)

---

## 설치와 첫 실행

### 전제

AIC 챌린지 환경(Docker + `aic_eval` 컨테이너, Distrobox, pixi + `~/ws_aic/src/aic`)이 이미 갖춰져 있다고 가정합니다. 없다면 [AIC Getting Started](https://github.com/intrinsic-dev/aic/blob/main/docs/getting_started.md)부터 진행하세요.

```bash
# uv 설치 (안 되어 있다면)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 저장소 clone
git clone https://github.com/e7217/aic-community-collector
cd aic-community-collector
```

### 첫 실행

```bash
uv run src/aic_collector/webapp.py
```

처음 실행하면 의존성을 자동으로 받아 설치합니다(~30초). 실행되면 다음 메시지가 출력됩니다:

```
Local URL: http://localhost:8501
Network URL: http://192.168.x.x:8501
```

브라우저에서 `http://localhost:8501`로 접속하면 **4개 탭**이 보입니다:

🔍 환경 점검 · 📋 작업 관리 · 🏃 작업 실행 · 📊 결과

> **Producer/Consumer 구조** — 주문서 생성(Producer)과 실제 실행(Consumer)을 분리했습니다. 주문서를 한 번에 수백 개 쌓아두고, 워커가 자기 속도로 소비합니다. 중간에 컴퓨터가 꺼져도 `pending/`에 남은 주문서는 안 사라져요.

---

## 환경 점검

처음 접속하면 🔍 **환경 점검** 탭이 기본으로 열립니다.

![환경 점검 탭](images/tab_check.png)

### 체크 항목

| 항목 | 의미 | 실패 시 대응 |
|------|------|-------------|
| **Docker (aic_eval)** | `aic_eval` 컨테이너 존재 | `distrobox create --nvidia -i ghcr.io/intrinsic-dev/aic/aic_eval:latest aic_eval` |
| **Distrobox** | distrobox 명령어 사용 가능 | `sudo apt install distrobox` |
| **pixi workspace** | `~/ws_aic/src/aic` 경로 존재 | AIC Getting Started 참고 |
| **pyyaml / numpy / scipy** | Python 의존성 | **[설치]** 버튼 또는 `uv sync` |

- ✅ 초록색 → 통과
- ❌ 빨간색 → 옆에 **[설치]** 버튼이 있으면 클릭, 없으면 안내된 명령어 수동 실행

모든 항목이 ✅ 되면 **📋 작업 관리** 탭으로 이동하세요.

---

## 작업 관리 — 주문서 만들기

로봇이 할 일을 **YAML 설정 파일**로 만들어서 `pending/`(대기 폴더) 큐에 쌓아두는 곳입니다. 실제 실행은 다음 탭에서 합니다.

![작업 관리 탭](images/tab_manage.png)

### 큐 루트

```
configs/train/
  ├─ sfp/
  │   ├─ pending/   ← 생성 직후 (워커가 소비할 대상)
  │   ├─ running/   ← 지금 실행 중
  │   ├─ done/      ← 성공
  │   └─ failed/    ← 실패
  └─ sc/ (동일 구조)
```

작업 관리 탭과 작업 실행 탭은 **큐 루트 경로가 세션으로 공유**됩니다. 한 쪽에서 바꾸면 다른 쪽도 자동 갱신됩니다.

### 📊 큐 상태 막대

Task별(SFP·SC) 현재 상태를 한 줄 가로 막대로 보여줍니다. 색상: 🟡 pending · 🔵 running · 🟢 done · 🔴 failed.

### ➕ 큐에 추가

두 종류의 Task에 대해 주문서를 생성합니다.

- **SFP** — NIC 카드에 케이블 꽂기 (5 rail × 2 port = 10종 target)
- **SC** — SC 포트 연결 (rail 0·1 = 2종 target)

#### 🎬 Scene — 엔티티 개수와 target 전략

| 항목 | 설명 |
|------|------|
| **NIC 고정 개수** | ON = 매 샘플 정확히 N개 / OFF = 1~N개 사이 랜덤 |
| **SC 고정 개수** | ON = 매 샘플 정확히 N개 / OFF = 1~N개 사이 랜덤 |
| **Target cycling** | ON = sample_index 기반 결정적 순환으로 **target을 정확히 균등 분배**. OFF = 매 샘플 uniform 추첨 (소량일 때 편향 위험) |

#### 📏 Parameters — 랜덤화 범위

케이블 위치(translation), 회전(yaw), 그리퍼 오차(gripper offset)의 **허용 최대 범위**입니다. 슬라이더 한계는 [AIC 공식 문서](https://github.com/intrinsic-dev/aic/blob/main/docs/task_board_description.md)의 최대 허용 범위와 같아 넘을 수 없습니다.

**샘플링 전략**

| 전략 | 방식 | 언제 |
|------|------|------|
| `uniform` | 각 샘플 독립 균등 난수 | 기본값, 단순 랜덤 |
| `lhs` | Latin Hypercube Sampling | 샘플 수가 적을 때 공간 커버 |

> append 모드에서는 배치마다 LHS가 독립 재추첨됩니다(이어지는 1000개를 한 번에 LHS로 엮지 않음). 큰 1회 생성이 이론상 더 고르지만, 실용적으로 차이는 작습니다.

### ⚙ 생성

**SFP configs** / **SC configs** 수량을 입력한 뒤 **[큐에 추가]**를 누르면 `pending/`에 파일이 생성됩니다.

- 파일명: `config_{task}_{NNNN}.yaml` (NNNN = 4자리 sample_index)
- append 모드: 모든 상태 디렉토리를 스캔해 **max(NNNN) + 1** 부터 번호 부여 → 중복 없음
- Seed는 `base_seed + sample_index` 로 파생되므로 재실행해도 동일 config가 나옵니다

### 🔀 Legacy 마이그레이션

예전 방식으로 `configs/train/{sfp,sc}/` 바로 아래에 있던 config 파일이 감지되면 **[Legacy N개 → pending/ 이동]** 버튼이 나타납니다. pending에 같은 이름이 있으면 데이터 손실 방지로 건너뜁니다.

### 🧠 내 Policy 사용하기

작업 관리 단계에서는 Policy를 고르지 않습니다. 주문서(scene)만 만들고, Policy 선택은 **작업 실행 탭의 실행 시점**에 결정합니다.

내 policy를 등록하려면:

1. `policies/` 또는 `~/ws_aic/src/aic/.pixi/envs/default/lib/python3.12/site-packages/aic_example_policies/ros/` 에 `.py` 파일 추가
2. `aic_model.policy.Policy` 상속 + `insert_cable()` 구현
3. 작업 실행 탭 Policy 드롭다운에서 자동 인식됨

---

## 작업 실행 — 워커 돌리기

`pending/`에 쌓인 주문서를 실제로 실행하는 곳입니다.

![작업 실행 탭](images/tab_execute.png)

### 🚀 실행 설정

| 항목 | 기본값 | 설명 |
|------|--------|------|
| **task 필터** | `all` | `all` = sfp→sc 순서로 전부, `sfp`/`sc` = 해당 task만 |
| **limit** | `5` | 최대 처리 config 수. `0` = 큐가 빌 때까지 |
| **Policy (기본)** | `cheatcode` | 어떤 policy로 실행할지 |
| **SFP/SC 분리** | OFF | ON이면 task별로 다른 policy 지정 가능 |
| **ACT 모델 경로** | (act/hybrid 선택 시만 표시) | `~/ws_aic/src/aic/outputs/train/.../pretrained_model` |
| **ground_truth** | ON | OFF = 현실적 조건(평가용), ON = GT 좌표 제공(수집용) |
| **use_compressed** | OFF | ON = JPEG 압축(~3 GB/run), OFF = raw(~58 GB/run) |
| **collect_episode** | OFF | ON = 이미지+npy 저장, OFF = bag+scoring만 |
| **timeout (초)** | `300` | config 1개당 최대 실행 시간. 넘으면 failed |
| **시작 전 running→pending 복구** | ON | 비정상 종료로 남은 파일을 자동 복원 |
| **Output root** | `~/aic_community_e2e` | 결과 저장 루트. 결과 탭과 세션으로 공유됨 |

### ▶ 워커 시작

버튼을 누르면 `aic-collector-worker` 프로세스가 백그라운드에서 시작됩니다.

실행 중에는 **3초마다 자동 갱신**되는 상태 화면이 나옵니다:

- 🟢 **● 실행 중 (PID: NNNN)**
- **진행률 막대** — `처리 N / 전체 M (done X, failed Y)`
- ⏱ **ETA** — 평균 `s/config` × 남은 수량
- 🔹 **현재 실행 중**: config 이름 + 경과 시간
- ✅/❌ **최근 처리 목록**

### ⏹ 워커 정지

버튼을 누르면 현재 실행 중인 config는 끝까지 처리한 뒤 깔끔하게 종료합니다(SIGTERM → 2초 → SIGKILL).

### ↩ 수동 복구

워커를 켜지 않고도 `running/`에 남은 파일을 `pending/`으로 되돌릴 수 있는 **[running/ N개 → pending/ 복구]** 버튼이 표시됩니다.

### 동시 실행 방지

이미 워커가 돌고 있을 때는 **워커 시작** 버튼이 숨겨집니다. PID 파일(`/tmp/aic_worker.pid`)과 상태 파일(`/tmp/aic_worker_state.json`)로 중복을 차단합니다.

---

## 결과 확인

![결과 탭](images/tab_results.png)

| 메트릭 | 의미 |
|------|------|
| **총 Trials** | 전체 시도 횟수 |
| **성공** | tier_3 임계값 이상 점수를 받은 trial 수와 비율 |
| **평균 점수** | 모든 trial의 평균 점수 |

### 결과 테이블 컬럼

- **time**: 수집 시각
- **run**: run 디렉토리명
- **trial**: 번호
- **score**: 0~100
- **success**: ✅ / ❌
- **duration**: 초
- **policy**: 사용된 policy
- **조기종료**: F5 조기 종료 여부 (⚡)

### CSV 다운로드 / 삭제

**📥 결과 CSV 다운로드**로 표를 내려받을 수 있고, **🗑 결과 정리** popover에서 특정 run을 삭제할 수 있습니다(되돌릴 수 없음).

### 검증 경고

씬 파라미터가 기대값과 어긋나거나 이상이 감지되면 테이블 아래 **⚠️ 검증 경고** expander가 열립니다. 어느 run의 어떤 체크가 실패했는지 확인하세요.

### 결과 파일 구조

큐 모드(1 config = 1 trial)는 **평탄 구조**로 저장됩니다. 디렉토리명에 타임스탬프·task·sample_index가 들어있고, 내부에 trial 래퍼 없이 곧바로 bag/episode/scoring 이 배치됩니다.

```
~/aic_community_e2e/
└── run_20260419_101852_sfp_0006/
    ├── config.yaml              # 실제 사용된 엔진 config
    ├── policy.txt               # 사용된 policy
    ├── seed.txt                 # 샘플링 seed
    ├── scoring_run.yaml         # 엔진의 전체 원본 scoring
    ├── trial_scoring.yaml       # trial 단독 추출 scoring
    ├── tags.json                # 태그/메타데이터
    ├── validation.json          # 구조/크기 검증 결과
    ├── bag/                     # ROS bag (mcap + metadata.yaml)
    └── episode/                 # collect_episode=ON일 때만
        ├── images/              # left/center/right PNG
        ├── states.npy
        ├── actions.npy
        └── metadata.json
```

Legacy Sweep 모드(다중 trial)는 여전히 trial 래퍼를 유지합니다:

```
run_01_20260408_234406/
├── config.yaml
├── trial_1_score95/
│   ├── bag/
│   ├── episode/
│   ├── scoring.yaml
│   └── tags.json
└── trial_2_score95/
```

---

## 고급: Prefect 대시보드

워커는 내부적으로 Prefect flow로 실행됩니다. **webapp 시작 시** Prefect 서버가 자동으로 백그라운드 기동됩니다 (`localhost:4200`). 이미 떠 있으면 재기동 없이 재사용합니다. CLI 워커만 단독 사용하려면 `uv run prefect server start` 을 수동으로 먼저 띄우세요.

### 접속

```
http://localhost:4200
```

처음 접속 시 "Join the Prefect Community" 모달이 뜨면 **Skip**으로 닫으세요.

### 대시보드

![Prefect 대시보드](images/prefect_dashboard.png)

- **Flow Runs** — 전체 실행 상태 분포
- **Task Runs** — task별 성공/실패 통계

### Runs

![Prefect Runs](images/prefect_runs.png)

좌측 **Runs**에서 시간순으로 모든 flow run을 조회. 이름을 클릭하면 상세로 이동.

### Flow Run 상세

![Flow 상세](images/prefect_flow_detail.png)

- **타임라인** — 각 task 구간/소요 시간
- **Logs** — stdout/stderr
- **Task Runs** — 개별 태스크 상태

### Artifacts (run 요약)

![Artifacts](images/prefect_artifacts.png)

각 run의 markdown 요약. 클릭하면 상세:

![Artifact 상세](images/prefect_artifact_detail.png)

요약에는 Policy·Seed·Trial별 점수·파라미터·출력 경로가 포함됩니다.

> Prefect는 **메타데이터만** 보여줍니다. rosbag/이미지/npy 같은 실제 데이터는 `~/aic_community_e2e/`에서 직접 확인하세요.

---

## CLI 사용

웹 화면 없이 터미널에서 바로 돌리기.

### 큐 소비 워커 (권장)

```bash
uv run aic-collector-worker --root configs/train --task all \
    --policy cheatcode --output-root ~/aic_data

# SFP만, 5개, 주문서당 최대 5분
uv run aic-collector-worker --root configs/train --task sfp \
    --limit 5 --timeout 300

# SFP·SC 분리 policy
uv run aic-collector-worker --root configs/train \
    --policy-sfp MyVisionPolicy --policy-sc cheatcode

# 비정상 종료 복구 후 실행
uv run aic-collector-worker --root configs/train --recover
```

주요 옵션:

| 옵션 | 기본 | 설명 |
|------|------|------|
| `--root` | `configs/train` | 큐 루트 |
| `--task` | `all` | `all`/`sfp`/`sc` |
| `--limit` | (없음) | 최대 처리 수 |
| `--policy` | `cheatcode` | 기본 policy |
| `--policy-sfp` / `--policy-sc` | (없음) | task별 분리 |
| `--act-model-path` | (없음) | act/hybrid용 |
| `--ground-truth` | `true` | GT 좌표 제공 |
| `--use-compressed` | `false` | JPEG 압축 |
| `--collect-episode` | `false` | episode 저장 |
| `--output-root` | `~/aic_community_e2e` | 결과 루트 |
| `--timeout` | (없음) | 1 config 최대 초 |
| `--recover` | off | 시작 전 running→pending |
| `--log` | `/tmp/aic_worker_run.log` | 엔진 실행 로그(append) |

### 단일 prebuilt config 실행

```bash
uv run aic-prefect-run \
    --engine-config configs/train/sfp/pending/config_sfp_0050.yaml \
    --policy cheatcode --output-root ~/aic_data
```

---

## 문제 해결

### "Error response from daemon: unable to find user ..."

컨테이너 초기화가 끝나지 않은 상태에서 진입을 시도해 발생. 처음 `aic_eval` 컨테이너를 만든 직후에는 초기화에 1~2분 걸릴 수 있습니다. 한 번 수동으로 진입해 끝내 주세요:

```bash
distrobox enter aic_eval -- true
```

### 워커가 실행 중이라고 나오는데 실제로는 없음

```bash
rm -f /tmp/aic_worker.pid /tmp/aic_worker_state.json
```

또는 작업 실행 탭에서 **⏹ 워커 정지**를 먼저 눌러 깔끔히 종료하세요.

### running/에 파일이 남음

비정상 종료 후 상태. 두 가지 복구 방법:

- **작업 실행 탭** — **↩ running/ N개 → pending/ 복구** 버튼
- **CLI** — `uv run aic-collector-worker --root configs/train --recover`

### Policy 타임아웃

config당 `--timeout` 초를 넘기면 failed로 이동합니다. 원인:

- 엔진이 제대로 기동되지 않음 → `/tmp/aic_worker_run.log` 확인
- Policy 코드가 멈춤 → 같은 로그의 policy stdout 확인
- 하드웨어 응답 없음 → ROS 토픽 상태 확인

Prefect 대시보드의 task별 로그가 가장 정확합니다.

### 수집은 됐는데 결과 탭이 비어있음

`Output root` 경로가 작업 실행 때 지정한 값과 결과 탭에서 보는 경로가 일치하는지 확인하세요. 두 값은 세션으로 공유되지만, 워커를 CLI로 별도 실행했다면 결과 탭 경로를 수동으로 맞춰야 합니다.

### 디스크 공간 부족

raw 이미지(`use_compressed=false`)는 run당 ~58 GB입니다. 대안:

- 작업 실행 탭에서 **use_compressed** ON → ~3 GB/run
- 작업 실행 탭에서 **collect_episode** OFF → bag+scoring만 저장
- 오래된 `run_*` 디렉토리를 결과 탭의 🗑 버튼으로 정리

### Prefect 서버가 안 뜸

webapp은 시작 시 자동으로 4200에 prefect 서버를 띄웁니다. 실패 안내가 상단에 뜨면 `/tmp/e2e_prefect_server.log` 에서 원인을 확인하세요. 수동으로 기동하려면:

```bash
uv run prefect server start --host 127.0.0.1 --port 4200
```

그 뒤 webapp 새로고침하면 health 체크를 통과해 그 서버를 그대로 사용합니다.

### 로그 위치 정리

| 파일 | 내용 |
|------|------|
| `/tmp/aic_worker_run.log` | 워커가 실행한 엔진/policy 통합 로그 (append) |
| `/tmp/aic_worker.pid` | 실행 중인 워커 PID |
| `/tmp/aic_worker_state.json` | 워커 실시간 상태 (처리·done·failed·current) |
| `~/.prefect/prefect.db` | Prefect 메타데이터 (run·task·artifact 기록) |

---

## 더 알아보기

- [Config Reference](config-reference.md) — YAML 항목별 설명·범위·규칙
- [AIC 공식 문서](https://github.com/intrinsic-dev/aic) — Task 구조·파라미터 근거
