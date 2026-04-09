#!/bin/bash
# EXP-009 E2E 수집 파이프라인
#
# Config 파일 하나로 자동 수집:
#   1. sampler.py로 N 세트 파라미터 생성
#   2. 각 run마다: builder → 엔진 → policy → postprocess
#   3. 결과를 `run_<id>_<date>/trial_<N>_score<NNN>/` 계층으로 저장
#
# 사용법:
#   ./scripts/collect_e2e.sh --config configs/e2e_default.yaml
#   ./scripts/collect_e2e.sh --config <cfg> --runs 3 --seed 42
#   ./scripts/collect_e2e.sh --config <cfg> --dry-run
#
# 옵션:
#   --config PATH    필수. E2E config 파일
#   --runs N         config의 runs 오버라이드
#   --seed N         config의 seed 오버라이드
#   --dry-run        config 해석 + 샘플링 시퀀스만 출력, 수집 안 함
#   --no-deploy      시작 시 deploy_policies.sh 호출 생략 (이미 배포했을 때)

set -e

export DBX_CONTAINER_MANAGER=docker
export PATH="$HOME/.pixi/bin:$PATH"

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
READY_FLAG="$HOME/aic_ready"
DONE_FLAG="$HOME/aic_done"
ENGINE_RESULTS="$HOME/aic_results"

# ---------- CLI 파싱 ----------
CONFIG=""
RUNS_OVERRIDE=""
SEED_OVERRIDE=""
DRY_RUN=0
DO_DEPLOY=1

while [ $# -gt 0 ]; do
    case "$1" in
        --config)
            CONFIG="$2"; shift 2 ;;
        --runs)
            RUNS_OVERRIDE="$2"; shift 2 ;;
        --seed)
            SEED_OVERRIDE="$2"; shift 2 ;;
        --dry-run)
            DRY_RUN=1; shift ;;
        --no-deploy)
            DO_DEPLOY=0; shift ;;
        -h|--help)
            sed -n '2,22p' "$0"; exit 0 ;;
        *)
            echo "[error] 알 수 없는 옵션: $1"; exit 1 ;;
    esac
done

if [ -z "$CONFIG" ]; then
    echo "[error] --config 필수"
    exit 1
fi
if [ ! -f "$CONFIG" ]; then
    echo "[error] config 없음: $CONFIG"
    exit 1
fi

# ---------- config 파싱 (Python) ----------
echo "=== EXP-009 E2E 수집 ==="
echo "config: $CONFIG"

# Python으로 config 읽어서 환경변수로 export
eval "$(python3 << PY
import yaml, os, shlex
with open("$CONFIG") as f:
    cfg = yaml.safe_load(f)

collection = cfg.get("collection", {}) or {}
policy = cfg.get("policy", {}) or {}
sampling = cfg.get("sampling", {}) or {}
engine = cfg.get("engine", {}) or {}

runs = "$RUNS_OVERRIDE" or collection.get("runs", 10)
seed = "$SEED_OVERRIDE" or collection.get("seed", 42)
output_root = os.path.expanduser(collection.get("output_root", "~/aic_community_e2e"))
trials = collection.get("trials", [1, 2, 3])
trials_str = ",".join(str(t) for t in trials)
policy_default = policy.get("default", "cheatcode")
act_model_path = os.path.expanduser(policy.get("act_model_path", ""))
strategy = sampling.get("strategy", "uniform")
ground_truth = str(engine.get("ground_truth", True)).lower()
template = engine.get("template", "configs/community_random_config.yaml")

print(f"RUNS={runs}")
print(f"SEED={seed}")
print(f"OUTPUT_ROOT={shlex.quote(output_root)}")
print(f"TRIALS={shlex.quote(trials_str)}")
print(f"POLICY_DEFAULT={shlex.quote(policy_default)}")
print(f"ACT_MODEL_PATH={shlex.quote(act_model_path)}")
print(f"SAMPLING_STRATEGY={shlex.quote(strategy)}")
print(f"GROUND_TRUTH={ground_truth}")
print(f"TEMPLATE={shlex.quote(template)}")
use_compressed = str(engine.get("use_compressed", True)).lower()
print(f"USE_COMPRESSED={use_compressed}")
PY
)"

echo "  runs: $RUNS"
echo "  seed: $SEED"
echo "  trials: $TRIALS"
echo "  policy: $POLICY_DEFAULT"
echo "  sampling: $SAMPLING_STRATEGY"
echo "  compressed: $USE_COMPRESSED"
echo "  output_root: $OUTPUT_ROOT"

# ---------- 1. 샘플링 ----------
SAMPLES_JSON="/tmp/e2e_samples_$$.json"
python3 "$PROJECT_DIR/scripts/sampler.py" \
    --config "$CONFIG" \
    --strategy "$SAMPLING_STRATEGY" \
    --runs "$RUNS" \
    --seed "$SEED" \
    > "$SAMPLES_JSON"

SAMPLE_COUNT=$(python3 -c "import json; print(len(json.load(open('$SAMPLES_JSON'))))")
echo "[sampler] $SAMPLE_COUNT 개 샘플 생성 → $SAMPLES_JSON"

if [ "$DRY_RUN" = "1" ]; then
    echo ""
    echo "=== DRY-RUN: 샘플 시퀀스 ==="
    python3 -m json.tool "$SAMPLES_JSON"
    echo ""
    echo "=== DRY-RUN: 첫 run의 엔진 config 미리보기 ==="
    DRY_CFG="/tmp/e2e_dry_$$.yaml"
    python3 "$PROJECT_DIR/scripts/build_engine_config.py" \
        --template "$TEMPLATE" \
        --trials "$TRIALS" \
        --params-json "$SAMPLES_JSON" \
        --params-index 0 \
        --out "$DRY_CFG"
    echo "(trials 키만 출력)"
    python3 -c "import yaml; d = yaml.safe_load(open('$DRY_CFG')); print('  trials:', list(d['trials'].keys()))"
    rm -f "$DRY_CFG" "$SAMPLES_JSON"
    exit 0
fi

# ---------- 2. 정책 배포 ----------
if [ "$DO_DEPLOY" = "1" ]; then
    echo "[deploy] policies/ → pixi env"
    "$PROJECT_DIR/scripts/deploy_policies.sh"
fi

# ---------- Policy 환경변수 설정 ----------

# F2-b: per_trial 설정이 있으면 DispatchWrapper 사용
PER_TRIAL_JSON=$(python3 -c "
import yaml, json
with open('$CONFIG') as f:
    cfg = yaml.safe_load(f)
pt = (cfg.get('policy') or {}).get('per_trial')
print(json.dumps(pt) if pt else '')
" 2>/dev/null)

POLICY_MAP=(cheatcode:CollectCheatCode hybrid:RunACTHybrid act:RunACTv1)
_resolve_policy_class() {
    local name="$1"
    case "$name" in
        cheatcode) echo "aic_example_policies.ros.CollectCheatCode" ;;
        hybrid)    echo "aic_example_policies.ros.RunACTHybrid" ;;
        act)       echo "aic_example_policies.ros.RunACTv1" ;;
        *)         echo "aic_example_policies.ros.$name" ;;
    esac
}

POLICY_CLASS=""
if [ -n "$PER_TRIAL_JSON" ] && [ "$PER_TRIAL_JSON" != "null" ]; then
    # F2-b: trial별 다른 policy → DispatchWrapper 사용
    POLICY_CLASS="aic_example_policies.ros.CollectDispatchWrapper"
    # 폴백 inner policy 세팅
    export AIC_INNER_POLICY="$(_resolve_policy_class "$POLICY_DEFAULT")"
    # per_trial 환경변수 세팅 (AIC_INNER_POLICY_TRIAL_1, _2, _3)
    eval "$(python3 -c "
import json, os
pt = json.loads('$PER_TRIAL_JSON')
resolve = {
    'cheatcode': 'aic_example_policies.ros.CheatCodeInner',
    'hybrid': 'aic_example_policies.ros.RunACTHybrid',
    'act': 'aic_example_policies.ros.RunACTv1',
}
for trial_num, policy_name in pt.items():
    cls = resolve.get(str(policy_name), f'aic_example_policies.ros.{policy_name}')
    print(f'export AIC_INNER_POLICY_TRIAL_{trial_num}=\"{cls}\"')
")"
    [ -n "$ACT_MODEL_PATH" ] && export ACT_MODEL_PATH
    echo "[policy] F2-b DispatchWrapper (per_trial 활성)"
    echo "  fallback: $AIC_INNER_POLICY"
    for i in 1 2 3; do
        var="AIC_INNER_POLICY_TRIAL_$i"
        [ -n "${!var}" ] && echo "  trial_$i: ${!var}"
    done
else
    # F2-a: 단일 policy
    case "$POLICY_DEFAULT" in
        cheatcode)
            POLICY_CLASS="aic_example_policies.ros.CollectCheatCode"
            ;;
        hybrid)
            POLICY_CLASS="aic_example_policies.ros.CollectWrapper"
            export AIC_INNER_POLICY="aic_example_policies.ros.RunACTHybrid"
            [ -n "$ACT_MODEL_PATH" ] && export ACT_MODEL_PATH
            ;;
        act)
            POLICY_CLASS="aic_example_policies.ros.CollectWrapper"
            export AIC_INNER_POLICY="aic_example_policies.ros.RunACTv1"
            [ -n "$ACT_MODEL_PATH" ] && export ACT_MODEL_PATH
            ;;
        wrapper)
            POLICY_CLASS="aic_example_policies.ros.CollectWrapper"
            ;;
        *)
            echo "[error] 알 수 없는 policy: $POLICY_DEFAULT"
            exit 1
            ;;
    esac
    echo "[policy] class: $POLICY_CLASS"
fi

RUN_TAG="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTPUT_ROOT"

# ---------- 3. 각 run 실행 ----------
run_one() {
    local run_idx="$1"           # 1-based
    local sample_idx=$((run_idx - 1))
    local run_name
    printf -v run_name "run_%02d_%s" "$run_idx" "$RUN_TAG"
    local run_dir="$OUTPUT_ROOT/$run_name"
    local engine_cfg="/tmp/e2e_engine_${RUN_TAG}_run${run_idx}.yaml"
    local demo_dir="/tmp/e2e_demos_${RUN_TAG}_run${run_idx}"
    local params_json="/tmp/e2e_params_${RUN_TAG}_run${run_idx}.json"

    echo ""
    echo "============================================================"
    echo "  RUN ${run_idx}/${RUNS}  →  $run_dir"
    echo "============================================================"

    rm -rf "$demo_dir"
    mkdir -p "$demo_dir"
    mkdir -p "$run_dir"

    # 이 run의 파라미터 한 세트를 JSON으로 추출
    python3 -c "
import json
with open('$SAMPLES_JSON') as f:
    samples = json.load(f)
with open('$params_json', 'w') as f:
    json.dump(samples[$sample_idx], f)
"

    # 엔진 config 생성
    python3 "$PROJECT_DIR/scripts/build_engine_config.py" \
        --template "$TEMPLATE" \
        --trials "$TRIALS" \
        --params-json "$SAMPLES_JSON" \
        --params-index "$sample_idx" \
        --out "$engine_cfg"

    # 이전 결과 치우기
    rm -f "$READY_FLAG" "$DONE_FLAG"
    [ -d "$ENGINE_RESULTS" ] && mv "$ENGINE_RESULTS" "${ENGINE_RESULTS}_e2e_backup_$(date +%s)" 2>/dev/null || true

    # 컨테이너 재시작
    echo "[engine] 컨테이너 재시작..."
    sudo docker restart aic_eval
    sleep 5

    # 엔진 기동
    local gt_arg="ground_truth:=true"
    [ "$GROUND_TRUTH" = "false" ] && gt_arg="ground_truth:=false"
    echo "[engine] 기동 ($gt_arg, trials=$TRIALS)..."
    distrobox enter -r aic_eval -- /entrypoint.sh \
        $gt_arg start_aic_engine:=true \
        aic_engine_config_file:="$engine_cfg" \
        &> "/tmp/e2e_engine_${RUN_TAG}_run${run_idx}.log" &
    local engine_pid=$!

    sleep 25

    # 카메라 compressed republish (USE_COMPRESSED=true일 때만)
    local republish_pids=""
    if [ "$USE_COMPRESSED" = "true" ]; then
        echo "[republish] 카메라 compressed 시작..."
        for cam in left_camera center_camera right_camera; do
            distrobox enter -r aic_eval -- bash -c \
                "source /ws_aic/install/setup.bash && \
                 export RMW_IMPLEMENTATION=rmw_zenoh_cpp && \
                 ros2 run image_transport republish \
                 --ros-args -p in_transport:=raw -p out_transport:=compressed \
                 -r in:=/${cam}/image -r out/compressed:=/${cam}/image/compressed \
                 -p use_sim_time:=true" \
                &> "/tmp/e2e_republish_${cam}_${RUN_TAG}_run${run_idx}.log" &
            republish_pids="$republish_pids $!"
        done
        sleep 3
    else
        echo "[republish] 이미지 압축 비활성화 — raw 이미지 사용"
    fi

    echo "[policy] $POLICY_DEFAULT 실행..."

    # Policy 실행 (inline env vars로 설정)
    local policy_log="/tmp/e2e_policy_${RUN_TAG}_run${run_idx}.log"
    cd ~/ws_aic/src/aic && \
        AIC_DEMO_DIR="$demo_dir" \
        AIC_F5_ENABLED="${AIC_F5_ENABLED:-1}" \
        pixi run ros2 run aic_model aic_model \
            --ros-args -p use_sim_time:=true \
            -p policy:="$POLICY_CLASS" \
            &> "$policy_log" &
    local policy_pid=$!
    cd "$PROJECT_DIR"

    # on_shutdown 대기 (최대 300초)
    local elapsed=0
    local timeout=300
    while ! grep -q "on_shutdown" "$policy_log" 2>/dev/null; do
        if ! kill -0 $policy_pid 2>/dev/null; then
            echo "[warn] Policy 프로세스 먼저 종료"
            break
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        if [ $elapsed -ge $timeout ]; then
            echo "[warn] Policy 타임아웃 (${timeout}초)"
            break
        fi
    done

    # 정리 (republish → policy → engine 순)
    for pid in $republish_pids; do
        kill $pid 2>/dev/null || true
    done
    pkill -f "aic_model" 2>/dev/null || true
    sleep 2
    pkill -9 -f "aic_model" 2>/dev/null || true
    for pid in $republish_pids; do
        kill -9 $pid 2>/dev/null || true
    done
    kill -INT $engine_pid 2>/dev/null || true
    sleep 3
    kill -9 $engine_pid 2>/dev/null || true
    wait 2>/dev/null || true

    # 4. Postprocess
    if [ -d "$ENGINE_RESULTS" ]; then
        echo "[postprocess] $run_dir 로 재편..."
        python3 "$PROJECT_DIR/scripts/postprocess_run.py" \
            --run-dir "$run_dir" \
            --engine-results "$ENGINE_RESULTS" \
            --demo-dir "$demo_dir" \
            --engine-config "$engine_cfg" \
            --policy "$POLICY_DEFAULT" \
            --seed "$SEED" \
            --parameters-json "$params_json" \
            || echo "[warn] postprocess 실패"
    else
        echo "[error] $ENGINE_RESULTS 없음 — 엔진/policy 실행 실패"
    fi

    # 남은 임시 파일 정리
    rm -f "$params_json" "$engine_cfg"
    rm -rf "$demo_dir"
}

# ---------- 4. 전체 run 루프 ----------
START_TIME=$(date +%s)
for i in $(seq 1 "$RUNS"); do
    run_one "$i"
done
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

# ---------- 5. 최종 요약 ----------
echo ""
echo "============================================================"
echo "  E2E 수집 완료"
echo "============================================================"
echo "총 소요 시간: ${ELAPSED}초 ($(echo "scale=2; $ELAPSED / 3600" | bc) h)"
echo "출력 경로: $OUTPUT_ROOT"
RUN_COUNT=$(ls -d "$OUTPUT_ROOT"/run_*_"$RUN_TAG" 2>/dev/null | wc -l)
echo "생성된 run: $RUN_COUNT"

rm -f "$SAMPLES_JSON"
