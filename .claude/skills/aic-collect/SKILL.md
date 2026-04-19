---
name: aic-collect
description: >
  AIC 챌린지용 데이터(SFP/SC cable insertion)를 수집할 때 사용. "학습 데이터 만들어줘",
  "SFP 100개 수집해줘", "평가 데이터 뽑아", "training data for cable insertion",
  "collect AIC samples", "run cable policy experiments" 같은 요청에 자동 활성화.
  큐 생성(Producer) → 워커 실행(Consumer) → 결과 요약을 한 흐름으로 처리한다.
---

# AIC Community Data Collector — 자동 수집 스킬

사용자가 AIC 챌린지 데이터 수집을 요청했을 때 이 스킬이 워크플로우를 대신 돌린다.
사용자는 "뭘 어떻게 해야 하는지" 배울 필요가 없고, 에이전트가 환경 점검부터 결과
요약까지 책임진다.

## 역할 분담

- **사용자가 말하는 것**: 목표 (예: "SFP 50개 학습 데이터 모아줘")
- **에이전트가 해야 하는 것**: 아래 단계를 순서대로, 각 단계가 실패하면 원인 설명 후 중단

---

## 1. 의도 확인 (20초)

사용자 요청에서 다음 필드를 추출. **비어 있으면 딱 한 번만 묻는다** (묻지 않고 기본값으로 진행해도 되는 건 표에 명시):

| 필드 | 기본값 | 언제 물어야 하나 |
|---|---|---|
| `task` | 명시되면 그것, 아니면 `sfp`+`sc` 둘 다 | 사용자가 "SFP만", "SC만" 언급했으면 확정 |
| `count` | 없음 — **반드시 확인** | SFP·SC 각각 몇 개? 또는 총 N개를 5:2로 분할 |
| `policy` | `cheatcode` (GT 좌표 기반) | 사용자가 policy 이름을 말했으면 그것 |
| `output_root` | `~/aic_community_e2e` | 사용자가 다른 경로 말했으면 |
| `ground_truth` | `true` (학습용) | 평가 데이터면 `false` 제안 |
| `use_compressed` | `false` (raw ~58GB/run) | 디스크 걱정 언급 시 `true` (3GB/run) 제안 |
| `collect_episode` | 학습 데이터면 `true`, 평가면 `false` | 의도가 모호하면 기본값 |

---

## 2. 환경 점검 (5초)

다음을 한 번에 확인하고 실패 시 **수집 중단**:

```bash
docker ps -a | grep aic_eval  # 컨테이너 존재
which distrobox               # 설치
test -d ~/ws_aic/src/aic      # pixi workspace
```

실패하면 사용자에게 원인과 수정 방법을 알려준다 (README의 "시작하기 전에 필요한 것" 참조).
에이전트가 혼자 고칠 수 없는 항목(docker 그룹 등)은 사용자 개입 요청.

---

## 3. 큐에 config 적재 (Producer)

**Streamlit UI의 "📋 작업 관리" 탭이 하는 일을 Python API로 직접 수행.**

```bash
uv run python - <<'PY'
from pathlib import Path
from aic_collector.sampler import sample_scenes
from aic_collector.job_queue import (
    ensure_queue_dirs, next_sample_index, write_plans
)

ROOT = Path("configs/train")
TEMPLATE = Path("configs/community_random_config.yaml")

# 기본 cfg — AIC 공식 허용 범위를 자동으로 쓰고, target cycling ON
cfg = {"training": {"param_strategy": "uniform"}}

ensure_queue_dirs(ROOT)

# SFP N개
SFP_COUNT = 50   # ← 사용자 요청으로 대체
start_sfp = next_sample_index(ROOT, "sfp")
sfp_plans = sample_scenes(cfg, task_type="sfp", count=SFP_COUNT,
                          seed=42, start_index=start_sfp)
sfp_paths = write_plans(sfp_plans, root=ROOT, template_path=TEMPLATE)
print(f"[ok] SFP {len(sfp_paths)}개 pending/에 적재 (idx {start_sfp}~)")

# SC M개 (선택)
SC_COUNT = 20    # ← 사용자 요청으로 대체 (0이면 생략)
if SC_COUNT > 0:
    start_sc = next_sample_index(ROOT, "sc")
    sc_plans = sample_scenes(cfg, task_type="sc", count=SC_COUNT,
                             seed=42, start_index=start_sc)
    sc_paths = write_plans(sc_plans, root=ROOT, template_path=TEMPLATE)
    print(f"[ok] SC {len(sc_paths)}개 pending/에 적재 (idx {start_sc}~)")
PY
```

주의:
- **SFP 10의 배수, SC 2의 배수** 권장 (target cycling 균등 분배). 사용자 숫자가 배수가 아니면 "균등 분배가 약간 불균형해진다" 한 줄만 알려주고 진행.
- `seed`는 고정 42 유지 — append 모드이므로 `start_index`가 달라져 자동으로 새 샘플 생성.
- `next_sample_index`가 모든 상태 디렉토리를 스캔해 **중복 없는 인덱스**를 보장.
- 파라미터 범위 기본값 = AIC 공식 허용 최대값. 사용자가 범위를 좁히고 싶다고
  말하면 `cfg["training"]["ranges"]["nic_translation"] = [-0.01, 0.01]` 같이 오버라이드.

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

주요 옵션:
- `--task all` = sfp→sc 순서로 전부. 특정 task만 원하면 `--task sfp` / `--task sc`.
- `--limit N` = 한 세션에 최대 N개까지 (생략 = 큐가 빌 때까지).
- `--policy-sfp` / `--policy-sc` = task별 다른 policy. `--policy`는 fallback.
- `--timeout` = config 1개 최대 실행 시간(초). 초과하면 failed.
- `--recover` = 이전 비정상 종료로 `running/`에 남은 파일을 `pending/`으로 복원 후 시작.

**진행 모니터링**: 워커는 `/tmp/aic_worker_state.json`에 실시간 상태를 기록한다.
장시간 실행 시 사용자에게 Prefect 대시보드(`http://localhost:4200`) 링크를 알려준다.

---

## 5. 결과 요약

완료되면 결과를 Python으로 집계해 **성공률·평균 점수·실패 원인**을 보고한다.

```bash
uv run python - <<'PY'
from pathlib import Path
from aic_collector.webapp import load_results, load_run_validations

rows = load_results(Path.home() / "aic_community_e2e")
if not rows:
    print("수집된 결과 없음")
    raise SystemExit

total = len(rows)
ok = sum(1 for r in rows if r["success"] == "✅")
avg = sum(r["score"] for r in rows) / total

print(f"총 {total}개 trial | 성공 {ok} ({100*ok/total:.0f}%) | 평균 점수 {avg:.1f}")

# 최근 실패들
fails = [r for r in rows if r["success"] == "❌"][-5:]
if fails:
    print("\n최근 실패 (최대 5개):")
    for r in fails:
        print(f"  {r['run']} — score {r['score']}")

# 검증 경고
warns = load_run_validations(Path.home() / "aic_community_e2e")
if warns:
    print(f"\n⚠️  검증 경고 있는 run {len(warns)}개 — 결과 탭에서 확인")
PY
```

결과 데이터의 **실제 파일 위치**는 `~/aic_community_e2e/run_{timestamp}_{task}_{NNNN}/` 평탄 구조.
각 run_dir 바로 아래에 `bag/`, `episode/`, `tags.json`, `scoring_run.yaml`, `validation.json` 배치.

---

## 실패 대응 체크리스트

| 증상 | 첫 대응 |
|---|---|
| 워커 시작 실패 "이미 실행 중" | `rm /tmp/aic_worker.pid /tmp/aic_worker_state.json` 후 재시작 |
| `running/`에 파일 남음 | `aic-collector-worker --recover` 한 번 실행 |
| config 전부 timeout | policy 코드 오류 또는 엔진 기동 실패. `/tmp/aic_worker_run.log` 확인 |
| 디스크 공간 부족 | `use_compressed=true` 제안, 또는 오래된 `run_*` 폴더 정리 |
| Prefect 서버 죽음 | 첫 워커 시작 시 자동으로 띄움. 안 되면 `uv run prefect server start` 수동 |

---

## 하지 말아야 할 것

- **AIC 공식 범위 초과** — `ranges`에 허용 한계 밖 값을 넣지 않는다 (UI 슬라이더가 막고 있는 이유).
- **`configs/train/*`를 git에 커밋** — 런타임 데이터. `.gitignore`에 이미 포함.
- **worker가 도는 중 `running/` 파일 수동 이동** — atomic claim이 깨진다. 반드시 워커 정지 후.
- **`policies/` 파일 추가만 하고 `insert_cable()` 미구현** — 런타임에 AttributeError.

---

## 빠른 참조

- README: 전반적 소개 (중학생 눈높이)
- `docs/usage-guide.md`: 단계별 상세 가이드
- `docs/config-reference.md`: YAML 항목·범위·규칙 전체 레퍼런스
