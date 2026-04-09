#!/bin/bash
# Policy 파일을 우리 프로젝트 → AIC 설치 경로로 배포
# 사용법: ./scripts/deploy_policies.sh

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
POLICY_SRC="$PROJECT_DIR/policies"
POLICY_DST="$HOME/ws_aic/src/aic/.pixi/envs/default/lib/python3.12/site-packages/aic_example_policies/ros"

if [ ! -d "$POLICY_SRC" ]; then
    echo "[ERROR] policies/ 디렉토리가 없습니다: $POLICY_SRC"
    exit 1
fi

if [ ! -d "$POLICY_DST" ]; then
    echo "[ERROR] 설치 경로가 없습니다: $POLICY_DST"
    exit 1
fi

count=0
for f in "$POLICY_SRC"/*.py; do
    [ -f "$f" ] || continue
    name=$(basename "$f")
    cp "$f" "$POLICY_DST/$name"
    echo "[OK] $name → 배포 완료"
    count=$((count + 1))
done

echo "=== $count개 Policy 배포 완료 ==="
