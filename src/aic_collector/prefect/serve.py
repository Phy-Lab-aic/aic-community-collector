#!/usr/bin/env python3
"""Prefect flow를 CLI에서 직접 실행하는 엔트리포인트.

Usage (기존 E2E 수집):
    uv run aic-prefect-run --config configs/e2e_test.yaml
    uv run aic-prefect-run --config configs/e2e_default.yaml --runs 3 --seed 123
    uv run aic-prefect-run --config configs/e2e_test.yaml --dry-run

Usage (Phase 2b — 이미 생성된 엔진 config 1회 실행):
    uv run aic-prefect-run --engine-config configs/train/sfp/running/config_sfp_0050.yaml \\
        --policy cheatcode --output-root ~/aic_data
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime


def _parse_bool(x: str) -> bool:
    return x.strip().lower() in ("1", "true", "yes", "y", "on")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prefect 수집 파이프라인")

    # 기존 E2E 모드
    parser.add_argument("--config", help="E2E config YAML 경로 (샘플링+실행)")
    parser.add_argument("--runs", type=int, default=None, help="runs 오버라이드")
    parser.add_argument("--seed", type=int, default=None, help="seed 오버라이드")
    parser.add_argument("--no-deploy", action="store_true", help="policy 배포 생략")
    parser.add_argument("--dry-run", action="store_true", help="샘플링만 확인")

    # Prebuilt 모드 (Phase 2b 큐 consumer)
    parser.add_argument(
        "--engine-config",
        help="이미 생성된 엔진 config 파일 — 이 파일로 1회 실행 (샘플링·생성 생략)",
    )
    parser.add_argument("--policy", default="cheatcode", help="policy 이름 (prebuilt 모드)")
    parser.add_argument("--act-model-path", default=None, help="ACT 모델 경로")
    parser.add_argument("--ground-truth", type=_parse_bool, default=True)
    parser.add_argument("--use-compressed", type=_parse_bool, default=False)
    parser.add_argument("--collect-episode", type=_parse_bool, default=False)
    parser.add_argument(
        "--output-root", default="~/aic_community_e2e",
        help="run_dir 루트 (prebuilt 모드)",
    )
    parser.add_argument(
        "--run-tag", default=None,
        help="run_dir 접미 태그 (기본: 타임스탬프)",
    )

    args = parser.parse_args()

    if not args.config and not args.engine_config:
        parser.error("--config 또는 --engine-config 중 하나는 필요합니다.")
    if args.config and args.engine_config:
        parser.error("--config와 --engine-config는 동시 사용 불가.")

    # Prebuilt 모드
    if args.engine_config:
        from aic_collector.prefect.flow import run_prebuilt_engine_config

        run_tag = args.run_tag or datetime.now().strftime("%Y%m%d_%H%M%S")
        result = run_prebuilt_engine_config(
            engine_cfg_path=args.engine_config,
            run_tag=run_tag,
            policy_default=args.policy,
            act_model_path=args.act_model_path,
            ground_truth=args.ground_truth,
            use_compressed=args.use_compressed,
            collect_episode=args.collect_episode,
            output_root=args.output_root,
        )
        return 0 if result.get("success", False) else 1

    # 기존 E2E 모드
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
