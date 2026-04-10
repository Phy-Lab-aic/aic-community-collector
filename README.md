# AIC Community Data Collector

AI for Industry Challenge 커뮨니티 구성원이 **자신의 Policy로 평가 데이터를 수집**하는 도구.

## 바로 시작하기

```bash
# 1. 저장소 clone
git clone https://github.com/e7217/aic-community-collector
cd aic-community-collector

# 2. policies/ 에 내 policy 파일 넣기

# 3. Web UI 실행
uv run src/aic_collector/webapp.py
```

브라우저에서 `http://localhost:8501` 접속.

## 전제 조건

챌린지 참가자라면 이미 갖춰져 있습니다.
설치가 안 되어 있다면 [AIC Getting Started 가이드](https://github.com/intrinsic-dev/aic/blob/main/docs/getting_started.md)를 참고하세요.

- Docker + `aic_eval` 컨테이너 (현재 사용자가 docker 그룹에 속해야 합니다)
- Distrobox
- pixi + `~/ws_aic/src/aic`
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

## 기능

| 기능 | 설명 |
|------|------|
| **Web UI** | 브라우저에서 설정 → 수집 → 결과 확인 |
| **Policy 선택** | 기본 제공(CheatCode/ACT/Hybrid) + 커스텀 policy |
| **Trial별 설정** | 각 trial에 다른 policy + 파라미터 범위 지정 |
| **파라미터 분포** | LHS / Uniform / Sobol 샘플링, seed 기반 재현 |
| **조기 종료** | 삽입 완료 시 자동으로 다음 trial로 (시간 -54%, 스코어 +6%) |
| **백그라운드 수집** | 탭을 옮겨도 수집 계속 진행 |
| **Config 관리** | 설정 저장/불러오기/삭제 |
| **환경 점검** | 필요한 도구 자동 체크 + 설치 |

## CLI 사용

Web UI 없이 CLI로도 수집 가능:

```bash
# dry-run (설정 확인만)
./scripts/collect_e2e.sh --config configs/e2e_default.yaml --dry-run

# 수집 실행
./scripts/collect_e2e.sh --config configs/e2e_default.yaml --runs 3

# trial 2만 수집
./scripts/collect_e2e.sh --config configs/e2e_trial2_only.yaml --runs 5
```

## 내 Policy 사용하기

1. `policies/` 디렉토리에 Python 파일 추가
2. `aic_model.policy.Policy`를 상속하고 `insert_cable()` 구현
3. Web UI에서 자동으로 드롭다운에 표시됨

## 결과 구조

```
~/aic_community_e2e/
└── run_01_20260408_234406/
    ├── config.yaml
    ├── trial_1_score95/
    │   ├── bag/          # rosbag
    │   ├── episode/      # PNG + npy
    │   ├── scoring.yaml
    │   └── tags.json     # 구조화 메타데이터
    ├── trial_2_score95/
    └── trial_3_score25/
```

## License

Apache-2.0
