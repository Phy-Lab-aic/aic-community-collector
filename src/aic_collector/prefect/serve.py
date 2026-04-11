#!/usr/bin/env python3
"""Prefect flow를 CLI에서 직접 실행하는 엔트리포인트.

Usage:
    # 기본 실행
    uv run aic-prefect-run --config configs/e2e_test.yaml

    # 옵션 오버라이드
    uv run aic-prefect-run --config configs/e2e_default.yaml --runs 3 --seed 123

    # dry-run
    uv run aic-prefect-run --config configs/e2e_test.yaml --dry-run
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Prefect E2E 수집 파이프라인")
    parser.add_argument("--config", required=True, help="E2E config YAML 경로")
    parser.add_argument("--runs", type=int, default=None, help="runs 오버라이드")
    parser.add_argument("--seed", type=int, default=None, help="seed 오버라이드")
    parser.add_argument("--no-deploy", action="store_true", help="policy 배포 생략")
    parser.add_argument("--dry-run", action="store_true", help="샘플링만 확인")
    args = parser.parse_args()

    from aic_collector.prefect.flow import collect_e2e_flow

    result = collect_e2e_flow(
        config_path=args.config,
        runs_override=args.runs,
        seed_override=args.seed,
        do_deploy=not args.no_deploy,
        dry_run=args.dry_run,
    )

    if result.get("dry_run"):
        return 0
    return 1 if result.get("fail_count", 0) == result.get("runs", 1) else 0


if __name__ == "__main__":
    sys.exit(main())
