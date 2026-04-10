#!/bin/bash
# Step 0 스모크 테스트: trial_2만 담긴 config로 엔진 1회 실행
#
# 목적: 엔진이 trials 부분 dict을 받아들이는지 실제 검증.
# 산출물: ~/aic_smoke_results/scoring.yaml (trial_2 키 하나만 있으면 성공)
#
# 사용법:
#   ./scripts/smoke_test_partial_trial.sh

set -e

export DBX_CONTAINER_MANAGER=docker
READY_FLAG="$HOME/aic_ready"
DONE_FLAG="$HOME/aic_done"

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
TEMPLATE="$PROJECT_DIR/configs/community_random_config.yaml"
TEST_CONFIG="/tmp/engine_config_trial2_only.yaml"
RESULT_ARCHIVE="$HOME/aic_smoke_results"

echo "=== Step 0 smoke test: trial_2 only ==="

# 1. 필터링된 config 생성
echo "[1/7] 필터링된 config 생성..."
python3 "$PROJECT_DIR/scripts/build_engine_config.py" \
    --template "$TEMPLATE" \
    --trials 2 \
    --out "$TEST_CONFIG"

# 2. 이전 결과 치우기
rm -f "$READY_FLAG" "$DONE_FLAG"
if [ -d "$HOME/aic_results" ]; then
    mv "$HOME/aic_results" "$HOME/aic_results_smoke_backup_$(date +%s)" 2>/dev/null || true
fi
rm -rf "$RESULT_ARCHIVE"

# 3. 컨테이너 재시작
echo "[2/7] 컨테이너 재시작..."
docker restart aic_eval
sleep 5

# 4. 엔진 시작 (backgrounded)
echo "[3/7] 엔진 시작 (trial_2 only config)..."
distrobox enter -r aic_eval -- /entrypoint.sh \
    ground_truth:=true start_aic_engine:=true \
    aic_engine_config_file:="$TEST_CONFIG" \
    &> /tmp/aic_smoke_engine.log &
ENGINE_PID=$!

# 5. 엔진 준비 대기
echo "[4/7] 엔진 준비 대기 (25s)..."
sleep 25

# 6. cheatcode policy 실행 (동기)
echo "[5/7] CheatCode policy 실행..."
POLICY_LOG="/tmp/aic_smoke_policy.log"
cd ~/ws_aic/src/aic && pixi run ros2 run aic_model aic_model \
    --ros-args -p use_sim_time:=true \
    -p policy:=aic_example_policies.ros.CollectCheatCode \
    &> "$POLICY_LOG" &
POLICY_PID=$!

# on_shutdown 대기 (최대 200초)
elapsed=0
while ! grep -q "on_shutdown" "$POLICY_LOG" 2>/dev/null; do
    if ! kill -0 $POLICY_PID 2>/dev/null; then
        echo "[warn] Policy 프로세스가 먼저 종료됨"
        break
    fi
    sleep 2
    elapsed=$((elapsed + 2))
    if [ $elapsed -ge 200 ]; then
        echo "[warn] Policy 타임아웃"
        break
    fi
done

# 7. 정리
echo "[6/7] 프로세스 정리..."
pkill -f "aic_model" 2>/dev/null || true
sleep 2
pkill -9 -f "aic_model" 2>/dev/null || true
kill -INT $ENGINE_PID 2>/dev/null || true
sleep 3
kill -9 $ENGINE_PID 2>/dev/null || true
wait 2>/dev/null || true

# 8. 결과 확인
echo "[7/7] 결과 확인..."
if [ -d "$HOME/aic_results" ]; then
    cp -r "$HOME/aic_results" "$RESULT_ARCHIVE"
    echo ""
    echo "=== scoring.yaml 내용 ==="
    if [ -f "$RESULT_ARCHIVE/scoring.yaml" ]; then
        cat "$RESULT_ARCHIVE/scoring.yaml"
        echo ""
        echo "=== trial 키만 추출 ==="
        python3 -c "
import yaml
with open('$RESULT_ARCHIVE/scoring.yaml') as f:
    d = yaml.safe_load(f)
trials_in_scoring = [k for k in d.keys() if k.startswith('trial_')]
print(f'trials in scoring.yaml: {trials_in_scoring}')
if trials_in_scoring == ['trial_2']:
    print('[PASS] trial_2 키만 존재 — 엔진이 부분 config를 받아들임')
else:
    print(f'[FAIL] 예상: [\"trial_2\"], 실제: {trials_in_scoring}')
"
    else
        echo "[ERROR] scoring.yaml 없음"
    fi
else
    echo "[ERROR] aic_results 디렉토리 없음 — 엔진/policy 실행 실패"
    echo "엔진 로그: /tmp/aic_smoke_engine.log"
    echo "Policy 로그: /tmp/aic_smoke_policy.log"
fi

echo ""
echo "로그 위치:"
echo "  엔진: /tmp/aic_smoke_engine.log"
echo "  Policy: /tmp/aic_smoke_policy.log"
echo "  결과: $RESULT_ARCHIVE"
