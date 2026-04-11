# AIC Community Data Collector

AI for Industry Challenge 커뮤니티 구성원이 **자신의 Policy로 평가 데이터를 수집**하는 도구.

> 💡 단계별 사용법, 문제 해결, 고급 기능은 [사용 가이드](docs/usage-guide.md)를 참고하세요.

## 전제 조건

챌린지 참가자라면 이미 갖춰져 있습니다.
설치가 안 되어 있다면 [AIC Getting Started 가이드](https://github.com/intrinsic-dev/aic/blob/main/docs/getting_started.md)를 참고하세요.

- Docker + `aic_eval` 컨테이너 (현재 사용자가 docker 그룹에 속해야 합니다)
- Distrobox
- pixi + `~/ws_aic/src/aic`
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Quick Start

```bash
# 1. 저장소 clone
git clone https://github.com/e7217/aic-community-collector
cd aic-community-collector

# 2. (선택) policies/ 에 내 policy 파일 넣기

# 3. Web UI 실행
uv run src/aic_collector/webapp.py
```
## 환경 설정

브라우저에서 `http://localhost:8501` 접속 후, **환경 점검** 탭에서 모든 항목이 통과하는지 확인하세요.

> 자세한 단계별 안내는 [사용 가이드 → 환경 점검](docs/usage-guide.md#환경-점검) 참고.

![환경 점검](docs/images/tab_check.png)

## 데이터 수집

모든 항목이 통과하면 **수집** 탭에서 Policy, 반복 횟수, 샘플링 전략을 설정하고 수집을 시작합니다.
저장된 Config를 불러오거나 직접 설정할 수 있습니다.

샘플링 전략은 씬 파라미터(케이블 위치, 각도 등)를 랜덤화하는 방법입니다:

- **LHS** (기본 권장) — 각 차원을 N등분하여 구간마다 한 점씩 샘플링. 적은 횟수로도 고르게 커버
- **Uniform** — 독립 균등 난수. 단순 랜덤이 필요할 때
- **Sobol** — 준난수 수열. `runs`를 2의 거듭제곱(8, 16, 32...)으로 설정할 때 효과적
- **Static** — AIC 공식 고정값 사용. 랜덤 없이 모든 run이 동일 조건 (baseline, 디버깅)

### 내 Policy 사용하기

1. `policies/` 디렉토리에 Python 파일 추가
2. `aic_model.policy.Policy`를 상속하고 `insert_cable()` 구현
3. Web UI 드롭다운에 자동 표시

![수집 설정](docs/images/tab_collect.png)

## 결과 확인

수집이 완료되면 **결과** 탭에서 전체 성공률, 평균 점수, trial별 상세 결과를 확인할 수 있습니다.
CSV 다운로드도 가능합니다.

![수집 결과](docs/images/tab_results.png)

## Config 파일

`configs/` 디렉토리의 YAML 파일로 수집 설정을 관리합니다. Web UI의 **Config 관리** 탭에서 조회/저장/삭제할 수 있습니다.

![Config 관리](docs/images/tab_config.png)

| 파일 | 용도 |
|------|------|
| `e2e_default.yaml` | 기본 설정 (3 trial, 10 runs, LHS) |
| `e2e_test.yaml` | 빠른 테스트 (1 trial, 1 run) |
| `e2e_trial2_only.yaml` | Trial 2만 집중 수집 |

전체 항목 설명은 [Config Reference](docs/config-reference.md)를 참고하세요.

## CLI 사용

Web UI 없이 CLI로도 수집 가능합니다. Prefect flow로 직접 실행:

```bash
# dry-run (설정 확인만, 실제 수집 안 함)
uv run aic-prefect-run --config configs/e2e_default.yaml --dry-run

# 수집 실행 (3회 반복)
uv run aic-prefect-run --config configs/e2e_default.yaml --runs 3

# 빠른 테스트 (1회, trial 1개)
uv run aic-prefect-run --config configs/e2e_test.yaml
```

## 결과 구조

```
~/aic_community_e2e/
└── run_01_20260408_234406/
    ├── config.yaml
    ├── trial_1_score95/
    │   ├── bag/          # rosbag
    │   ├── episode/      # PNG + npy
    │   ├── scoring.yaml
    │   └── tags.json
    ├── trial_2_score95/
    └── trial_3_score25/
```

## 더 알아보기

- [사용 가이드](docs/usage-guide.md) — 단계별 사용법, Prefect 대시보드, 문제 해결
- [Config Reference](docs/config-reference.md) — 모든 config 항목 설명

## License

Apache-2.0
