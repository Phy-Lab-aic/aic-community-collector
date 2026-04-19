# Config Reference

`configs/e2e_*.yaml`(legacy Sweep)와 `configs/train/{sfp,sc}/{pending,running,done,failed}/config_*.yaml`(작업 큐) 의 항목 설명.

## 두 가지 Config 유형

- **Sweep config** (`configs/e2e_*.yaml`, legacy) — 기존 "🚀 수집" 탭에서 사용. Prefect flow가 파라미터 샘플링·엔진 config 생성·실행을 한 흐름으로 처리. 신규 작업은 Producer/Consumer 큐 사용 권장.
- **Queue config** (`configs/train/{sfp,sc}/{pending,running,done,failed}/config_*.yaml`) — 각 파일이 **완전한 scene**을 포함하는 엔진 config. **📋 수집 관리** 탭이 생성해 `pending/`에 적재, **🏃 작업 실행** 탭의 워커가 atomic rename으로 `running/` → `done/` / `failed/`로 이동시키며 소비.

## 작업 큐 디렉토리 규약 (Phase 2a+)

```
configs/train/
  ├ sfp/
  │   ├ pending/   ← 생성 직후 (워커가 소비 대상)
  │   ├ running/   ← 실행 중
  │   ├ done/      ← 성공
  │   └ failed/    ← 실패
  └ sc/ (동일 구조)
```

- 파일명: `config_{task_type}_{NNNNNN}.yaml` (NNNNNN은 6자리 sample_index).
- append 모드: 모든 상태 디렉토리 + legacy(플랫 *.yaml)를 스캔해 `max(NNNNNN) + 1`부터 부여. 중복 없음.
- Legacy 파일(플랫 구조): 관리 탭의 "Legacy → pending/ 이동" 버튼으로 일괄 마이그레이션 가능. pending에 동일 이름이 있으면 건너뜀(데이터 손실 방지).

## Sweep config 전체 구조

```yaml
schema_version: "0.1"

collection:   # 수집 기본 설정
policy:       # Policy 선택
parameters:   # 씬 파라미터 랜덤화 범위
sampling:     # 샘플링 전략
tags:         # 결과 자동 태깅
engine:       # 시뮬레이션 엔진 옵션
```

---

## collection

| 항목 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| `runs` | int | `10` | 반복 횟수. 각 run마다 다른 파라미터 세트로 수집 |
| `trials` | list | `[1, 2, 3]` | 수집할 trial 번호. 부분 수집 시 `[2]` 등으로 지정 |
| `output_root` | path | `~/aic_community_e2e` | 결과 저장 루트 디렉토리 |
| `seed` | int | `42` | 파라미터 샘플링 재현용 seed |

**Trial 번호와 작업:**

| Trial | 작업 | 대상 |
|-------|------|------|
| 1 | SFP 삽입 | NIC Card 0 |
| 2 | SFP 삽입 | NIC Card 1 |
| 3 | SC 삽입 | SC Port |

CLI에서 `--runs`, `--seed`로 오버라이드 가능합니다.

---

## policy

| 항목 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| `default` | string | `cheatcode` | 모든 trial에 적용할 기본 policy |
| `per_trial` | dict | (없음) | trial별 다른 policy 지정. `default`보다 우선 |
| `act_model_path` | path | (없음) | ACT/Hybrid policy용 모델 체크포인트 경로 |

**사용 가능한 policy:**

| 이름 | 클래스 | 설명 |
|------|--------|------|
| `cheatcode` | `CheatCodeInner` | Ground truth 좌표 기반 삽입 (수집용) |
| `act` | `RunACTv1` | ACT 모델 단독 추론 |
| `hybrid` | `RunACTHybrid` | ACT + 보정 로직 혼합 |

**trial별 다른 policy 예시:**

```yaml
policy:
  default: cheatcode        # 기본
  per_trial:
    1: cheatcode            # trial 1: cheatcode
    2: act                  # trial 2: ACT 모델
    3: hybrid               # trial 3: Hybrid
  act_model_path: ~/ws_aic/src/aic/outputs/train/.../pretrained_model
```

---

## parameters

씬 파라미터의 랜덤화 범위를 `{ min, max }` 형식으로 지정합니다.
범위는 `community_random_config.yaml`의 `task_board_limits` 기준입니다.

| 파라미터 | 단위 | 기본 min | 기본 max | 대상 |
|----------|------|----------|----------|------|
| `nic0_translation` | m | -0.0215 | 0.0234 | NIC Card 0 위치 |
| `nic0_yaw` | rad | -0.1745 | 0.1745 | NIC Card 0 각도 |
| `nic1_translation` | m | -0.0215 | 0.0234 | NIC Card 1 위치 |
| `nic1_yaw` | rad | -0.1745 | 0.1745 | NIC Card 1 각도 |
| `sc0_translation` | m | -0.06 | 0.055 | SC Card 0 위치 (배경 오브젝트) |
| `sc0_yaw` | rad | -0.1745 | 0.1745 | SC Card 0 각도 |
| `sc1_translation` | m | -0.06 | 0.055 | SC Card 1 위치 |
| `sc1_yaw` | rad | -0.1745 | 0.1745 | SC Card 1 각도 |

범위를 좁히면 특정 조건에 집중, 넓히면 더 다양한 데이터를 수집합니다.

---

## sampling

| 항목 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| `strategy` | string | `lhs` | 샘플링 전략 |

**전략 비교:**

| 전략 | 방식 | 장점 | 주의사항 |
|------|------|------|----------|
| `lhs` | Latin Hypercube Sampling | 적은 횟수로 공간을 고르게 커버 | scipy 필요 |
| `uniform` | 독립 균등 난수 | 단순, 의존성 없음 | 고차원에서 빈 공간 발생 가능 |
| `sobol` | 준난수(quasi-random) 수열 | 고차원 균등 분포 | `runs`를 2^k(8, 16, 32...)로 설정해야 효과적. scipy 필요 |
| `static` | AIC 공식 고정값 (랜덤 없음) | 모든 run 동일 조건 (baseline, 디버깅) | `parameters`의 min/max 무시. seed 영향 없음 |

**static 전략의 고정값** (AIC `sample_config.yaml` 기준):

| 파라미터 | 값 | 출처 |
|----------|-----|------|
| `nic0_translation` | 0.036 | trial_1 `nic_rail_0` |
| `nic0_yaw` | 0.0 | |
| `nic1_translation` | 0.036 | trial_2 `nic_rail_1` |
| `nic1_yaw` | 0.0 | |
| `sc0_translation` | 0.042 | trial_1/2 배경 `sc_rail_0` |
| `sc0_yaw` | 0.1 | |
| `sc1_translation` | -0.055 | trial_3 `sc_rail_1` |
| `sc1_yaw` | 0.0 | |

**사용 예:** 같은 씬에서 여러 번 수집하여 Policy 일관성 측정, 또는 랜덤 변수 없는 baseline 비교 실험. `parameters` 섹션은 무시되므로 범위 설정이 필요 없습니다.

---

## tags

수집 결과에 자동으로 `tags.json`을 생성하는 설정입니다.

| 항목 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| `schema_version` | string | `"0.0.0"` | 태그 스키마 버전 |
| `auto.success_from_scoring` | bool | `true` | `scoring.yaml`의 tier_3 성공 여부를 자동 복사 |
| `auto.cable_type_from_metadata` | bool | `true` | `metadata.json`의 cable_type을 자동 복사 |

---

## engine

| 항목 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| `ground_truth` | bool | `true` | `true`: 정확한 TF 좌표 제공 (수집용). `false`: 현실적 조건 (평가용) |
| `use_compressed` | bool | `false` | `true`: JPEG 압축 이미지 (~3GB/run). `false`: raw 이미지 (~58GB/run) |
| `template` | path | `configs/community_random_config.yaml` | 엔진 config 템플릿 파일 |

---

## 기본 제공 Config

| 파일 | runs | trials | policy | sampling | 용도 |
|------|------|--------|--------|----------|------|
| `e2e_default.yaml` | 10 | [1,2,3] | cheatcode | lhs | 표준 수집 |
| `e2e_test.yaml` | 1 | [1] | cheatcode | lhs | 빠른 동작 테스트 |
| `e2e_trial2_only.yaml` | 5 | [2] | cheatcode | lhs | Trial 2 집중 수집 |

위 Sweep config들은 legacy 경로이며 신규 작업은 Producer/Consumer 큐(작업 관리 탭)를 사용하는 것을 권장합니다.

---

## Training / Queue Config

Web UI **📋 작업 관리 탭 → ➕ 큐에 추가**로 일괄 생성되는 엔진 config들 (`configs/train/{sfp,sc}/pending/config_*.yaml`).
Sweep config와 달리 각 파일이 **완전한 scene**을 가지므로 엔진에 바로 넘길 수 있다(플레이스홀더 없음).

### 출력 구조

```
configs/train/
├── sfp/
│   ├── config_sfp_000000.yaml    ← trial 1개를 완전히 기술
│   ├── config_sfp_000001.yaml
│   ...
└── sc/
    ├── config_sc_000000.yaml
    ...
```

파일명의 `NNNNNN`은 6자리 sample index. **append 모드(기본)** 에서는 기존 마지막 번호 다음부터 이어서 생성됩니다.

### 자동 생성 규칙 (피드백 문서 기준)

| 항목 | 규칙 |
|------|------|
| Task board pose | SFP `(0.15, -0.2, 1.14, π)` / SC `(0.17, 0, 1.14, 3.0)` 고정 |
| NIC card 개수 | 1~5개 랜덤 (rail 0~4 비복원 선택) |
| NIC card translation | `[-0.0215, 0.0234]` 균등 |
| NIC card yaw | `[-0.1745, 0.1745]` 균등 (±10°) |
| SC port 개수 | 1~2개 랜덤 (rail 0, 1 선택) |
| SC port translation | `[-0.06, 0.055]` 균등 |
| SC port yaw | 0.0 고정 |
| Gripper offset xyz | nominal ± 0.002 m 랜덤 |
| Gripper offset rpy | nominal ± 0.04 rad 랜덤 |
| Mount rails | trial_1 패턴 고정 |
| **Target** | **SFP 10종(5 rail × 2 port), SC 2종 결정적 순환 — 균등 분포 보장** |

### 결정적 순환 (Target cycling)

작업 관리 탭의 Target cycling(ON)은 `sample_index`를 target cycle 배열 인덱스로 사용해 **동일 seed라도 target 분포를 완벽히 균등**하게 맞춥니다.

- **SFP**: `[(rail, port) for rail in 0..4 for port in ("sfp_port_0", "sfp_port_1")]` → 총 10종. `sample_index % 10`.
- **SC**: `[(0, "sc_port_0"), (1, "sc_port_1")]` → 2종. `sample_index % 2`.
- **Target rail은 활성 rail 목록에 강제 포함**됩니다 (target이 없는 scene을 생성하지 않도록).

### 재현성

- 각 샘플의 RNG seed는 `base_seed + sample_index`로 파생 → append 모드에서도 새 샘플이 기존과 중복되지 않음.
- 동일 `base_seed` + 동일 `count` + 동일 `task_type` → 동일 config 시퀀스.
