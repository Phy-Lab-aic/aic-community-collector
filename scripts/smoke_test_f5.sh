#!/bin/bash
# F5 (Trial 조기 종료) 검증 스모크 테스트
#
# 목적: CollectCheatCode(항상 삽입 성공)로 trial 2만 수집, F5 off→on 순차 실행 후
#       trial_duration_sec 및 early_terminated 메타 비교.
#
# 산출물:
#   ~/aic_f5_test_off/episode_0000/metadata.json  (F5 off)
#   ~/aic_f5_test_on/episode_0000/metadata.json   (F5 on)
#
# 예상 소요: ~5-6분 (엔진 기동 × 2 + 각 trial ~32초 off / ~27초 on)
#
# 자동 실행 전:
#   uv run python -c "from aic_collector.prefect.policy_env import deploy_policies; deploy_policies('.')"
#
# 사용법:
#   ./scripts/smoke_test_f5.sh

set -e

export DBX_CONTAINER_MANAGER=docker
export PATH="$HOME/.pixi/bin:$PATH"
READY_FLAG="$HOME/aic_ready"
DONE_FLAG="$HOME/aic_done"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$PROJECT_DIR/configs/community_random_config.yaml"
TEST_CONFIG="/tmp/engine_config_f5_test.yaml"

# 정책 파일 자동 배포 (편집 내용 반영 보장)
echo "[prep] 정책 배포 (policies/ → pixi env)..."
uv run python -c "from aic_collector.prefect.policy_env import deploy_policies; deploy_policies('$PROJECT_DIR')"

# 이전 F5 테스트 결과 정리
rm -rf "$HOME/aic_f5_test_off" "$HOME/aic_f5_test_on"
mkdir -p "$HOME/aic_f5_test_off" "$HOME/aic_f5_test_on"

# 필터링된 config 1회 생성 (trial 2만, 중간값 파라미터로 고정 — 두 실행이 같은 조건)
echo "=== F5 verification smoke test ==="
echo "[prep] 필터링된 config 생성 (trial 2만)..."
python3 "$PROJECT_DIR/scripts/build_engine_config.py" \
    --template "$TEMPLATE" \
    --trials 2 \
    --out "$TEST_CONFIG"

run_once() {
    local label="$1"        # "off" | "on"
    local demo_dir="$2"
    local f5_enabled="$3"

    echo ""
    echo "============================================================"
    echo "  RUN: F5=${label}  AIC_F5_ENABLED=${f5_enabled}"
    echo "============================================================"

    export AIC_DEMO_DIR="$demo_dir"
    export AIC_F5_ENABLED="$f5_enabled"

    rm -f "$READY_FLAG" "$DONE_FLAG"
    [ -d "$HOME/aic_results" ] && mv "$HOME/aic_results" "$HOME/aic_results_f5_backup_$(date +%s)" 2>/dev/null || true

    echo "[1/5] 컨테이너 재시작..."
    docker restart aic_eval
    sleep 5

    echo "[2/5] 엔진 시작 (trial_2 only, ground_truth=true)..."
    distrobox enter aic_eval -- /entrypoint.sh \
        ground_truth:=true start_aic_engine:=true \
        aic_engine_config_file:="$TEST_CONFIG" \
        &> "/tmp/aic_f5_${label}_engine.log" &
    local ENGINE_PID=$!

    echo "[3/5] 엔진 대기 (25s)..."
    sleep 25

    echo "[4/5] CollectCheatCode 실행 (AIC_F5_ENABLED=${f5_enabled})..."
    local POLICY_LOG="/tmp/aic_f5_${label}_policy.log"
    cd ~/ws_aic/src/aic && AIC_F5_ENABLED="$f5_enabled" AIC_DEMO_DIR="$demo_dir" \
        pixi run ros2 run aic_model aic_model \
        --ros-args -p use_sim_time:=true \
        -p policy:=aic_example_policies.ros.CollectCheatCode \
        &> "$POLICY_LOG" &
    local POLICY_PID=$!

    # on_shutdown 또는 타임아웃 대기 (F5 off일 땐 ACT_DURATION+나머지로 길어질 수 있음)
    local elapsed=0
    local timeout=200
    while ! grep -q "on_shutdown" "$POLICY_LOG" 2>/dev/null; do
        if ! kill -0 $POLICY_PID 2>/dev/null; then
            echo "[warn] Policy 프로세스가 먼저 종료됨"
            break
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        if [ $elapsed -ge $timeout ]; then
            echo "[warn] Policy 타임아웃 (${timeout}초)"
            break
        fi
    done

    echo "[5/5] 프로세스 정리..."
    pkill -f "aic_model" 2>/dev/null || true
    sleep 2
    pkill -9 -f "aic_model" 2>/dev/null || true
    kill -INT $ENGINE_PID 2>/dev/null || true
    sleep 3
    kill -9 $ENGINE_PID 2>/dev/null || true
    wait 2>/dev/null || true

    # Episode metadata 확인
    local meta_file
    meta_file=$(find "$demo_dir" -name "metadata.json" 2>/dev/null | head -1)
    if [ -n "$meta_file" ]; then
        echo ""
        echo "=== F5=${label} metadata.json ==="
        python3 -c "
import json, sys
with open('$meta_file') as f:
    d = json.load(f)
print(f'  trial_duration_sec: {d.get(\"trial_duration_sec\", \"N/A\")}')
print(f'  duration_sec:       {d.get(\"duration_sec\", \"N/A\")}')
print(f'  num_steps:          {d.get(\"num_steps\", \"N/A\")}')
print(f'  success:            {d.get(\"success\", \"N/A\")}')
print(f'  early_terminated:   {d.get(\"early_terminated\", \"N/A\")}')
print(f'  early_term_source:  {d.get(\"early_term_source\", \"N/A\")}')
print(f'  plug_port_distance: {d.get(\"plug_port_distance\", \"N/A\")}')
"
    else
        echo "[ERROR] metadata.json 없음 — 수집 실패"
    fi
}

# 1. F5 OFF
run_once "off" "$HOME/aic_f5_test_off" "0"

# 2. F5 ON
run_once "on" "$HOME/aic_f5_test_on" "1"

# 3. 비교 리포트
echo ""
echo "============================================================"
echo "  RESULT COMPARISON"
echo "============================================================"
python3 <<'PY'
import json
from pathlib import Path

def load_meta(dir_name):
    d = Path.home() / dir_name
    files = list(d.rglob("metadata.json"))
    if not files:
        return None
    with open(files[0]) as f:
        return json.load(f)

off = load_meta("aic_f5_test_off")
on  = load_meta("aic_f5_test_on")

if off is None:
    print("[ERROR] F5 off metadata 없음")
elif on is None:
    print("[ERROR] F5 on metadata 없음")
else:
    t_off = off.get("trial_duration_sec")
    t_on  = on.get("trial_duration_sec")
    print(f"F5 off trial_duration: {t_off} s")
    print(f"F5 on  trial_duration: {t_on} s  (early_terminated={on.get('early_terminated')}, source={on.get('early_term_source')})")
    if isinstance(t_off, (int, float)) and isinstance(t_on, (int, float)) and t_off > 0:
        diff = t_off - t_on
        pct = diff / t_off * 100
        print(f"차이: {diff:+.2f} s  ({pct:+.1f}%)")
        if t_on < t_off:
            print("[PASS] F5 적용 시 trial 실행 시간 단축됨")
        else:
            print("[WARN] F5가 trial 시간을 줄이지 못함 — 원인 조사 필요")
    else:
        print("[WARN] trial_duration_sec 값이 비정상")
PY

echo ""
echo "로그 위치:"
echo "  F5 off engine: /tmp/aic_f5_off_engine.log"
echo "  F5 off policy: /tmp/aic_f5_off_policy.log"
echo "  F5 on  engine: /tmp/aic_f5_on_engine.log"
echo "  F5 on  policy: /tmp/aic_f5_on_policy.log"
