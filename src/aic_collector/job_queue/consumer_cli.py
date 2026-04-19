#!/usr/bin/env python3
"""
큐 소비 CLI — pending에서 claim → aic-prefect-run --engine-config 실행 → mark.

Usage:
    uv run aic-collector-worker --root configs/train --task all --limit 10

모든 주요 옵션은 subprocess로 aic-prefect-run에 전달된다. 실행 실패(반환코드 0이
아닌 경우)에는 해당 config를 failed/로 이동한다. 워커 비정상 종료로 running/에
남은 파일은 다음 기동 시 `--recover` 옵션으로 pending/으로 되돌릴 수 있다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from aic_collector.job_queue.layout import TASK_TYPES
from aic_collector.job_queue.state import queue_counts
from aic_collector.job_queue.worker import (
    claim_one,
    mark_done,
    mark_failed,
    recover_running_to_pending,
)

# 진행 상태 공유 파일 (UI가 읽음)
WORKER_STATE_FILE = Path("/tmp/aic_worker_state.json")


def _write_state(state: dict) -> None:
    try:
        WORKER_STATE_FILE.write_text(json.dumps(state))
    except Exception:
        pass


def _read_state() -> dict:
    if not WORKER_STATE_FILE.exists():
        return {}
    try:
        return json.loads(WORKER_STATE_FILE.read_text())
    except Exception:
        return {}


def run_one(
    running_path: Path,
    policy: str,
    act_model_path: str | None,
    ground_truth: bool,
    use_compressed: bool,
    collect_episode: bool,
    output_root: str,
    run_tag: str,
    timeout_sec: int | None,
    log_path: Path | None,
) -> int:
    """aic-prefect-run --engine-config 를 subprocess로 실행하고 리턴코드 반환."""
    cmd = [
        "uv", "run", "aic-prefect-run",
        "--engine-config", str(running_path),
        "--policy", policy,
        "--ground-truth", str(ground_truth).lower(),
        "--use-compressed", str(use_compressed).lower(),
        "--collect-episode", str(collect_episode).lower(),
        "--output-root", output_root,
        "--run-tag", run_tag,
    ]
    if act_model_path:
        cmd += ["--act-model-path", act_model_path]

    out = open(log_path, "ab") if log_path else subprocess.DEVNULL
    try:
        proc = subprocess.run(
            cmd,
            stdout=out, stderr=subprocess.STDOUT,
            timeout=timeout_sec,
            check=False,
        )
        return proc.returncode
    except subprocess.TimeoutExpired:
        return 124
    finally:
        if hasattr(out, "close"):
            out.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="AIC 큐 consumer 워커")
    parser.add_argument("--root", default="configs/train", help="큐 루트")
    parser.add_argument(
        "--task", choices=["all"] + list(TASK_TYPES), default="all",
        help="소비할 task 종류",
    )
    parser.add_argument("--limit", type=int, default=None, help="최대 처리 수")
    parser.add_argument("--policy", default="cheatcode",
                        help="기본 policy. --policy-sfp/--policy-sc 미지정 시 fallback.")
    parser.add_argument("--policy-sfp", default=None,
                        help="SFP task 전용 policy. 미지정 시 --policy 값 사용.")
    parser.add_argument("--policy-sc", default=None,
                        help="SC task 전용 policy. 미지정 시 --policy 값 사용.")
    parser.add_argument("--act-model-path", default=None)
    parser.add_argument("--ground-truth", type=lambda x: x.lower() == "true", default=True)
    parser.add_argument("--use-compressed", type=lambda x: x.lower() == "true", default=False)
    parser.add_argument("--collect-episode", type=lambda x: x.lower() == "true", default=False)
    parser.add_argument("--output-root", default="~/aic_community_e2e")
    parser.add_argument(
        "--timeout", type=int, default=None,
        help="config 1개당 최대 실행 시간(초). 넘으면 failed.",
    )
    parser.add_argument(
        "--recover", action="store_true",
        help="시작 전 running/에 남은 파일을 pending/으로 되돌림",
    )
    parser.add_argument(
        "--log", default="/tmp/aic_worker_run.log",
        help="엔진 실행 로그 파일 (append 모드)",
    )
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        sys.stderr.write(f"[error] 큐 루트 없음: {root}\n")
        return 2

    log_path = Path(args.log) if args.log else None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("")  # 새 세션 시작 시 초기화

    targets = None if args.task == "all" else [args.task]

    if args.recover:
        for tt in (targets or list(TASK_TYPES)):
            n = recover_running_to_pending(root, tt)
            if n:
                print(f"[recover] {tt}: {n}개 pending으로 복구")

    started_at = datetime.now().isoformat(timespec="seconds")
    processed = 0
    done_count = 0
    fail_count = 0
    t0 = time.time()

    # 워커 시작 시점의 대상 task_type별 pending+running 합 — 진행률 계산용
    tt_for_total = targets if targets else list(TASK_TYPES)
    total_at_start = 0
    for tt in tt_for_total:
        c = queue_counts(root, tt)
        total_at_start += c.pending + c.running
    if args.limit is not None and args.limit > 0:
        total_at_start = min(total_at_start, args.limit)

    recent: list[dict] = []  # 최근 처리 rolling 리스트 (max 5)

    def _snapshot(
        *, status: str, current: str | None = None,
        current_path: str | None = None,
        current_started_at: str | None = None,
        finished: bool = False,
    ) -> dict:
        s = {
            "status": status,
            "started_at": started_at,
            "processed": processed,
            "done": done_count,
            "failed": fail_count,
            "current": current,
            "current_path": current_path,
            "current_started_at": current_started_at,
            "recent": list(recent),
            "total_at_start": total_at_start,
            "root": str(root),
            "task": args.task,
            "limit": args.limit,
        }
        if finished:
            s["finished_at"] = datetime.now().isoformat(timespec="seconds")
            s["elapsed_sec"] = int(time.time() - t0)
        return s

    _write_state(_snapshot(status="running"))

    try:
        while True:
            if args.limit is not None and processed >= args.limit:
                break

            claim = claim_one(root, targets)
            if claim is None:
                break

            claim_started_at_iso = datetime.now().isoformat(timespec="seconds")
            claim_t0 = time.time()
            _write_state(_snapshot(
                status="running",
                current=claim.name,
                current_path=str(claim.running_path),
                current_started_at=claim_started_at_iso,
            ))

            run_tag = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{claim.task_type}_{claim.sample_index:04d}"
            # task별 policy dispatch — 미지정 시 --policy 값 사용
            if claim.task_type == "sfp":
                effective_policy = args.policy_sfp or args.policy
            elif claim.task_type == "sc":
                effective_policy = args.policy_sc or args.policy
            else:
                effective_policy = args.policy
            print(f"[claim] {claim.name} → running (policy={effective_policy})")

            rc = run_one(
                claim.running_path,
                policy=effective_policy,
                act_model_path=args.act_model_path,
                ground_truth=args.ground_truth,
                use_compressed=args.use_compressed,
                collect_episode=args.collect_episode,
                output_root=args.output_root,
                run_tag=run_tag,
                timeout_sec=args.timeout,
                log_path=log_path,
            )

            duration_sec = int(time.time() - claim_t0)
            if rc == 0:
                mark_done(claim, root)
                done_count += 1
                result_label = "done"
                print(f"[done ] {claim.name} (rc=0)")
            else:
                mark_failed(claim, root)
                fail_count += 1
                result_label = "failed"
                print(f"[fail ] {claim.name} (rc={rc})")

            recent.insert(0, {
                "name": claim.name,
                "result": result_label,
                "duration_sec": duration_sec,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            })
            del recent[5:]

            processed += 1

        elapsed = int(time.time() - t0)
        counts_after = {t: queue_counts(root, t) for t in (targets or list(TASK_TYPES))}

        _write_state(_snapshot(status="completed", finished=True))

        print()
        print(f"=== 워커 종료: {processed}개 처리 (done={done_count}, failed={fail_count}, 소요 {elapsed}s) ===")
        for tt, c in counts_after.items():
            print(f"  {tt}: pending={c.pending} running={c.running} done={c.done} failed={c.failed}")

        return 0 if fail_count == 0 else 1

    except KeyboardInterrupt:
        _write_state(_snapshot(status="interrupted", finished=True))
        print("\n[interrupt] 워커 중단. running/에 남은 파일은 --recover로 복구 가능.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
