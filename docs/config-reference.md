# Config Reference

`configs/e2e_*.yaml` 파일의 전체 항목 설명.

## 전체 구조

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

Web UI에서 설정을 변경한 뒤 저장하면 `configs/e2e_*.yaml`로 추가됩니다.
