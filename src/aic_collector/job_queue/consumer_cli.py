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
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from aic_collector.job_queue.layout import TASK_TYPES
from aic_collector.job_queue.state import queue_counts
from aic_collector.job_queue.worker import (
    ClaimedConfig,
    claim_one,
    mark_done,
    mark_failed,
    recover_running_to_pending,
)

# 진행 상태 공유 파일 (UI가 읽음). Automation runners may override this
# path via --state-file or AIC_WORKER_STATE_FILE to avoid colliding with the
# normal Streamlit worker status.
WORKER_STATE_FILE = Path("/tmp/aic_worker_state.json")
DEFAULT_WORKER_STATE_FILE = WORKER_STATE_FILE


@dataclass(frozen=True)
class LerobotUploadConfig:
    """Post-collection LeRobot conversion + Hugging Face upload settings."""

    hf_repo_id: str
    manifest_path: Path
    staging_root: Path
    lerobot_root: Path
    converter_path: Path
    path_prefix: str
    batch_id: str
    cleanup_after_upload: bool = True


@dataclass(frozen=True)
class PreparedLerobotItem:
    """A collected item that has been converted and is waiting for batch upload."""

    item_id: str
    run_dir: Path
    staged_path: Path
    lerobot_path: Path


def _item_id_from_claim(claim: ClaimedConfig) -> str:
    return claim.name.removesuffix(".yaml")


def _path_in_repo(prefix: str, item_id: str) -> str:
    clean_prefix = prefix.strip("/")
    return f"{clean_prefix}/{item_id}" if clean_prefix else item_id


def _safe_batch_id_part(value: str | None, fallback: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", (value or "").strip()).strip(".-")
    return clean or fallback


def _default_worker_batch_id(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    user = _safe_batch_id_part(getpass.getuser(), "user")
    host = _safe_batch_id_part(socket.gethostname().split(".", maxsplit=1)[0], "host")
    return f"worker-{timestamp}-{user}-{host}-{os.getpid()}-{uuid4().hex[:8]}"


def _append_failure_event(manifest_path: Path, *, item_id: str, state: str, batch_id: str, **payload: Any) -> None:
    """Best-effort failure ledger append; never hides the original worker error."""
    try:
        from aic_collector.automation.manifest import append_event

        append_event(manifest_path, item_id=item_id, state=state, batch_id=batch_id, **payload)
    except Exception as exc:  # pragma: no cover - defensive ledger fallback
        print(f"[upload-ledger-warn] {item_id}: {state} 기록 실패: {type(exc).__name__}: {exc}")


def record_worker_manifest_start(config: LerobotUploadConfig, claim: ClaimedConfig) -> None:
    """Record that the normal queue worker owns this upload automation item."""
    from aic_collector.automation.manifest import append_event, latest_event

    item_id = _item_id_from_claim(claim)
    latest = latest_event(config.manifest_path, item_id)
    latest_state = latest.get("state") if latest else None
    if latest_state == "worker_started":
        return
    if latest_state is not None and latest_state != "planned":
        # Do not hide manifest recovery problems. Other states mean this item
        # already reached a terminal/later point and should not be claimed again.
        raise RuntimeError(f"{item_id} manifest state is {latest_state!r}, cannot start worker item")
    if latest_state is None:
        append_event(
            config.manifest_path,
            item_id=item_id,
            state="planned",
            batch_id=config.batch_id,
            queue_path=str(claim.running_path),
        )
    append_event(
        config.manifest_path,
        item_id=item_id,
        state="worker_started",
        batch_id=config.batch_id,
        queue_path=str(claim.running_path),
    )


def record_worker_manifest_failure(config: LerobotUploadConfig, claim: ClaimedConfig, *, return_code: int) -> None:
    item_id = _item_id_from_claim(claim)
    _append_failure_event(
        config.manifest_path,
        item_id=item_id,
        state="worker_failed",
        batch_id=config.batch_id,
        return_code=return_code,
        queue_path=str(claim.running_path),
    )


def prepare_lerobot_upload_item(
    *,
    config: LerobotUploadConfig,
    claim: ClaimedConfig,
    done_path: Path,
    output_root: str,
    run_tag: str,
    collect_episode: bool,
) -> tuple[PreparedLerobotItem | None, dict[str, Any]]:
    """Validate a completed worker run and convert it to a per-item LeRobot folder."""
    from aic_collector.automation.manifest import append_event
    from aic_collector.automation.batch_runner import (
        folder_inventory,
        run_converter,
        stage_run_artifacts,
        validate_run_artifacts,
    )

    item_id = _item_id_from_claim(claim)
    run_dir = Path(output_root).expanduser() / f"run_{run_tag}"

    # The manifest is an append-only safety ledger, but older cleanup code could
    # delete it if a bad cleanup_paths entry pointed at the ledger/root. Rebuild
    # the pre-completion states before appending worker_finished so a recoverable
    # ledger loss does not crash the worker after a successful collection.
    record_worker_manifest_start(config, claim)
    append_event(
        config.manifest_path,
        item_id=item_id,
        state="worker_finished",
        batch_id=config.batch_id,
        queue_path=str(done_path),
        run_dir=str(run_dir),
    )
    append_event(
        config.manifest_path,
        item_id=item_id,
        state="reconciled",
        batch_id=config.batch_id,
        queue_path=str(done_path),
        run_dir=str(run_dir),
    )

    validation = validate_run_artifacts(run_dir, collect_episode=collect_episode)
    if not validation.get("ok"):
        append_event(
            config.manifest_path,
            item_id=item_id,
            state="validation_failed",
            batch_id=config.batch_id,
            run_dir=str(run_dir),
            validation=validation,
        )
        return None, {"ok": False, "stage": "validation", "validation": validation}
    append_event(
        config.manifest_path,
        item_id=item_id,
        state="collected_validated",
        batch_id=config.batch_id,
        run_dir=str(run_dir),
        validation=validation,
    )

    try:
        staged_path = stage_run_artifacts(
            run_dir=run_dir,
            staging_root=config.staging_root,
            item_id=item_id,
        )
    except Exception as exc:
        append_event(
            config.manifest_path,
            item_id=item_id,
            state="stage_failed",
            batch_id=config.batch_id,
            run_dir=str(run_dir),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None, {"ok": False, "stage": "stage", "error": str(exc)}
    append_event(
        config.manifest_path,
        item_id=item_id,
        state="staged",
        batch_id=config.batch_id,
        staged_path=str(staged_path),
        inventory=folder_inventory(staged_path),
    )

    lerobot_path = config.lerobot_root / "items" / item_id
    try:
        rc = run_converter(
            converter_path=config.converter_path,
            input_path=staged_path,
            output_path=lerobot_path,
        )
    except Exception as exc:
        append_event(
            config.manifest_path,
            item_id=item_id,
            state="convert_failed",
            batch_id=config.batch_id,
            staged_path=str(staged_path),
            lerobot_path=str(lerobot_path),
            converter_path=str(config.converter_path),
            error=f"{type(exc).__name__}: {exc}",
        )
        return None, {
            "ok": False,
            "stage": "convert",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if rc != 0:
        append_event(
            config.manifest_path,
            item_id=item_id,
            state="convert_failed",
            batch_id=config.batch_id,
            staged_path=str(staged_path),
            lerobot_path=str(lerobot_path),
            converter_path=str(config.converter_path),
            return_code=rc,
        )
        return None, {"ok": False, "stage": "convert", "return_code": rc}
    append_event(
        config.manifest_path,
        item_id=item_id,
        state="converted",
        batch_id=config.batch_id,
        staged_path=str(staged_path),
        lerobot_path=str(lerobot_path),
        inventory=folder_inventory(lerobot_path),
    )
    return PreparedLerobotItem(
        item_id=item_id,
        run_dir=run_dir,
        staged_path=staged_path,
        lerobot_path=lerobot_path,
    ), {"ok": True, "stage": "converted"}


def upload_lerobot_batch(
    *,
    config: LerobotUploadConfig,
    items: list[PreparedLerobotItem],
    batch_index: int,
) -> dict[str, Any]:
    """Upload a group of converted items, verify remotely, then clean local raw data."""
    if not items:
        return {"ok": True, "stage": "empty"}

    from aic_collector.automation.manifest import append_event
    from aic_collector.automation.batch_runner import (
        cleanup_verified_paths,
        folder_inventory,
        link_or_copy,
        record_upload_and_verify,
    )

    batch_name = f"batch_{batch_index:04d}"
    batch_item_id = f"{config.batch_id}_{batch_name}"
    batch_folder = config.lerobot_root / "upload_batches" / config.batch_id / batch_name
    if batch_folder.exists():
        shutil.rmtree(batch_folder)
    batch_folder.mkdir(parents=True, exist_ok=True)

    for item in items:
        target = batch_folder / item.item_id
        shutil.copytree(item.lerobot_path, target, copy_function=link_or_copy)

    path_in_repo = _path_in_repo(config.path_prefix, f"{config.batch_id}/{batch_name}")
    cleanup_paths = [str(batch_folder)]
    if config.cleanup_after_upload:
        for item in items:
            cleanup_paths.extend([str(item.run_dir), str(item.staged_path), str(item.lerobot_path)])

    try:
        remote = record_upload_and_verify(
            manifest_path=config.manifest_path,
            item_id=batch_item_id,
            batch_id=config.batch_id,
            local_folder=batch_folder,
            repo_id=config.hf_repo_id,
            path_in_repo=path_in_repo,
            cleanup_paths=cleanup_paths,
        )
    except Exception as exc:
        _append_failure_event(
            config.manifest_path,
            item_id=batch_item_id,
            state="upload_failed",
            batch_id=config.batch_id,
            batch_items=[item.item_id for item in items],
            batch_folder=str(batch_folder),
            error=f"{type(exc).__name__}: {exc}",
        )
        return {"ok": False, "stage": "upload", "error": str(exc)}

    if not remote.get("ok"):
        return {"ok": False, "stage": "remote_verify_failed", "remote": remote}

    batch_inventory = folder_inventory(batch_folder) if batch_folder.exists() else {"file_count": 0, "files": []}
    for item in items:
        member_path = f"{path_in_repo}/{item.item_id}"
        append_event(
            config.manifest_path,
            item_id=item.item_id,
            state="uploaded",
            batch_id=config.batch_id,
            upload={
                "repo_id": config.hf_repo_id,
                "repo_type": "dataset",
                "path_in_repo": member_path,
                "method": "upload_folder_batch_member",
                "batch_item_id": batch_item_id,
                "inventory": batch_inventory,
            },
        )
        append_event(
            config.manifest_path,
            item_id=item.item_id,
            state="remote_verified",
            batch_id=config.batch_id,
            remote={**remote, "path_in_repo": member_path, "batch_item_id": batch_item_id},
            cleanup_paths=[str(item.run_dir), str(item.staged_path), str(item.lerobot_path)]
            if config.cleanup_after_upload else [],
        )

    deleted = cleanup_verified_paths(config.manifest_path) if config.cleanup_after_upload else []
    return {
        "ok": True,
        "stage": "remote_verified",
        "batch_item_id": batch_item_id,
        "batch_size": len(items),
        "deleted_paths": deleted,
        "remote": remote,
    }


def recover_converted_upload_items(config: LerobotUploadConfig) -> tuple[list[PreparedLerobotItem], int]:
    """Recover converted-but-not-uploaded items after a worker restart."""
    from aic_collector.automation.manifest import append_event, read_events

    latest: dict[str, dict[str, Any]] = {}
    paths_by_item: dict[str, dict[str, str]] = {}
    for event in read_events(config.manifest_path):
        item_id = str(event.get("item_id") or "")
        if not item_id:
            continue
        latest[item_id] = event
        item_paths = paths_by_item.setdefault(item_id, {})
        for key in ("run_dir", "staged_path", "lerobot_path"):
            value = event.get(key)
            if value:
                item_paths[key] = str(value)

    recovered: list[PreparedLerobotItem] = []
    failures = 0
    for item_id, event in sorted(latest.items()):
        if event.get("state") != "converted":
            continue
        item_paths = paths_by_item.get(item_id, {})
        required = {
            "run_dir": item_paths.get("run_dir"),
            "staged_path": item_paths.get("staged_path"),
            "lerobot_path": item_paths.get("lerobot_path"),
        }
        missing = [key for key, value in required.items() if not value or not Path(value).exists()]
        if missing:
            failures += 1
            append_event(
                config.manifest_path,
                item_id=item_id,
                state="upload_failed",
                batch_id=event.get("batch_id") or config.batch_id,
                stage="resume_converted",
                missing_paths=missing,
            )
            continue
        recovered.append(
            PreparedLerobotItem(
                item_id=item_id,
                run_dir=Path(required["run_dir"]),
                staged_path=Path(required["staged_path"]),
                lerobot_path=Path(required["lerobot_path"]),
            )
        )
    return recovered, failures


def run_lerobot_upload_automation(
    *,
    config: LerobotUploadConfig,
    claim: ClaimedConfig,
    done_path: Path,
    output_root: str,
    run_tag: str,
    collect_episode: bool,
) -> dict[str, Any]:
    """Backward-compatible single-item collect→convert→upload helper."""
    item, result = prepare_lerobot_upload_item(
        config=config,
        claim=claim,
        done_path=done_path,
        output_root=output_root,
        run_tag=run_tag,
        collect_episode=collect_episode,
    )
    if item is None:
        return result
    return upload_lerobot_batch(config=config, items=[item], batch_index=1)


def resolve_worker_state_file(cli_state_file: str | None) -> Path:
    """Resolve worker state-file precedence: CLI > env > legacy default."""
    if cli_state_file:
        return Path(cli_state_file).expanduser()
    env_path = os.environ.get("AIC_WORKER_STATE_FILE")
    if env_path:
        return Path(env_path).expanduser()
    return WORKER_STATE_FILE


def _write_state(state: dict, *, state_file: Path | None = None) -> None:
    target = state_file or WORKER_STATE_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(state))
    except Exception:
        pass


def _read_state(*, state_file: Path | None = None) -> dict:
    target = state_file or WORKER_STATE_FILE
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text())
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
    headless: bool = False,
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
        "--headless" if headless else "--no-headless",
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
    parser.add_argument(
        "--state-file", default=None,
        help=(
            "워커 상태 JSON 파일. 기본은 /tmp/aic_worker_state.json, "
            "자동화 배치는 AIC_WORKER_STATE_FILE 또는 이 옵션으로 격리."
        ),
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "엔진 GUI 비표시 모드 (Gazebo + RViz 창 끄기). "
            "기본 GUI 표시. 대량 수집/CI에는 --headless 권장."
        ),
    )
    parser.add_argument(
        "--hf-repo-id",
        default=None,
        help=(
            "지정하면 워커가 각 성공 run을 LeRobot으로 변환하고 Hugging Face dataset repo에 "
            "업로드/remote verify까지 수행."
        ),
    )
    parser.add_argument(
        "--automation-manifest",
        type=Path,
        default=None,
        help="LeRobot/HF 업로드 이벤트 JSONL. 기본: <output-root>/worker_lerobot_upload_manifest.jsonl",
    )
    parser.add_argument(
        "--staging-root",
        type=Path,
        default=Path("/tmp/aic_worker_lerobot_stage"),
        help="run 산출물을 rosbag-to-lerobot 입력으로 복사할 임시 staging root.",
    )
    parser.add_argument(
        "--lerobot-root",
        type=Path,
        default=Path("/tmp/aic_worker_lerobot_dataset"),
        help="변환된 LeRobot dataset output root.",
    )
    parser.add_argument(
        "--converter-path",
        type=Path,
        default=Path("third_party/rosbag-to-lerobot"),
        help="rosbag-to-lerobot checkout 경로.",
    )
    parser.add_argument(
        "--hf-path-prefix",
        default="worker",
        help="HF repo 안 업로드 prefix. 실제 path는 <prefix>/<config_id>.",
    )
    parser.add_argument(
        "--upload-batch-size",
        type=int,
        default=1,
        help="LeRobot/HF 업로드 묶음 크기. 예: --limit 800 --upload-batch-size 20.",
    )
    parser.add_argument(
        "--cleanup-after-upload",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="remote verify 후 raw run_dir/staging/LeRobot 임시 폴더 삭제.",
    )
    parser.add_argument(
        "--batch-id",
        default=None,
        help="manifest에 기록할 batch id. 기본: worker 시작 시각 기반 자동 생성.",
    )
    args = parser.parse_args()
    state_file = resolve_worker_state_file(args.state_file)

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        sys.stderr.write(f"[error] 큐 루트 없음: {root}\n")
        return 2

    log_path = Path(args.log) if args.log else None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("")  # 새 세션 시작 시 초기화

    targets = None if args.task == "all" else [args.task]
    upload_config: LerobotUploadConfig | None = None
    upload_fail_count = 0
    if args.hf_repo_id:
        if args.upload_batch_size <= 0:
            parser.error("--upload-batch-size must be positive")
        output_root_path = Path(args.output_root).expanduser()
        manifest_path = args.automation_manifest or (output_root_path / "worker_lerobot_upload_manifest.jsonl")
        batch_id = args.batch_id or _default_worker_batch_id()
        upload_config = LerobotUploadConfig(
            hf_repo_id=args.hf_repo_id,
            manifest_path=manifest_path.expanduser(),
            staging_root=args.staging_root.expanduser(),
            lerobot_root=args.lerobot_root.expanduser(),
            converter_path=args.converter_path.expanduser(),
            path_prefix=args.hf_path_prefix,
            batch_id=batch_id,
            cleanup_after_upload=bool(args.cleanup_after_upload),
        )
        upload_config.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        upload_config.staging_root.mkdir(parents=True, exist_ok=True)
        upload_config.lerobot_root.mkdir(parents=True, exist_ok=True)
        print(
            "[upload] LeRobot/HF 자동화 활성화: "
            f"repo={upload_config.hf_repo_id} manifest={upload_config.manifest_path} "
            f"upload_batch_size={args.upload_batch_size} cleanup={upload_config.cleanup_after_upload}"
        )
        converter_entrypoint = upload_config.converter_path / "src" / "main.py"
        if not converter_entrypoint.exists():
            sys.stderr.write(
                "[error] rosbag-to-lerobot converter entry point not found: "
                f"{converter_entrypoint}. Install/checkout rosbag-to-lerobot at that path "
                "or pass --converter-path to an existing checkout.\n"
            )
            return 2

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
    pending_upload_items: list[PreparedLerobotItem] = []
    upload_batch_index = 0
    upload_batches_done = 0
    if upload_config is not None:
        recovered_upload_items, recovered_upload_failures = recover_converted_upload_items(upload_config)
        pending_upload_items.extend(recovered_upload_items)
        upload_fail_count += recovered_upload_failures
        if recovered_upload_items:
            print(f"[upload-recover] converted 항목 {len(recovered_upload_items)}개를 업로드 대기열로 복구")
        if recovered_upload_failures:
            print(f"[upload-recover] converted 항목 {recovered_upload_failures}개는 경로 누락으로 upload_failed 처리")

    def _flush_upload_batch() -> dict[str, Any] | None:
        nonlocal upload_batch_index, upload_batches_done, upload_fail_count
        if upload_config is None or not pending_upload_items:
            return None
        upload_batch_index += 1
        batch_items = list(pending_upload_items)
        pending_upload_items.clear()
        result = upload_lerobot_batch(
            config=upload_config,
            items=batch_items,
            batch_index=upload_batch_index,
        )
        if result.get("ok"):
            upload_batches_done += 1
            print(f"[upload] batch {upload_batch_index} ({len(batch_items)}개) → remote_verified")
        else:
            upload_fail_count += len(batch_items)
            print(
                f"[upload-fail] batch {upload_batch_index} "
                f"({len(batch_items)}개) stage={result.get('stage')}"
            )
        return result

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
            "upload_failed": upload_fail_count,
            "upload_enabled": upload_config is not None,
            "upload_batch_size": args.upload_batch_size if upload_config is not None else None,
            "upload_batch_pending": len(pending_upload_items),
            "upload_batches_done": upload_batches_done,
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

    _write_state(_snapshot(status="running"), state_file=state_file)

    try:
        while True:
            if args.limit is not None and processed >= args.limit:
                break

            claim = claim_one(root, targets)
            if claim is None:
                break

            claim_started_at_iso = datetime.now().isoformat(timespec="seconds")
            claim_t0 = time.time()
            if upload_config is not None:
                record_worker_manifest_start(upload_config, claim)
            _write_state(_snapshot(
                status="running",
                current=claim.name,
                current_path=str(claim.running_path),
                current_started_at=claim_started_at_iso,
            ), state_file=state_file)

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
                headless=args.headless,
            )

            duration_sec = int(time.time() - claim_t0)
            if rc == 0:
                done_path = mark_done(claim, root)
                done_count += 1
                result_label = "done"
                print(f"[done ] {claim.name} (rc=0)")
                upload_result: dict[str, Any] | None = None
                if upload_config is not None:
                    prepared_item, upload_result = prepare_lerobot_upload_item(
                        config=upload_config,
                        claim=claim,
                        done_path=done_path,
                        output_root=args.output_root,
                        run_tag=run_tag,
                        collect_episode=args.collect_episode,
                    )
                    if prepared_item is not None:
                        pending_upload_items.append(prepared_item)
                        result_label = "converted"
                        print(
                            f"[upload-pending] {claim.name} converted "
                            f"({len(pending_upload_items)}/{args.upload_batch_size})"
                        )
                        if len(pending_upload_items) >= args.upload_batch_size:
                            upload_result = _flush_upload_batch()
                            if upload_result and upload_result.get("ok"):
                                result_label = "uploaded"
                            else:
                                result_label = "upload_failed"
                    else:
                        upload_fail_count += 1
                        result_label = "upload_failed"
                        print(
                            f"[upload-fail] {claim.name} "
                            f"stage={upload_result.get('stage')}"
                        )
            else:
                if upload_config is not None:
                    record_worker_manifest_failure(upload_config, claim, return_code=rc)
                mark_failed(claim, root)
                fail_count += 1
                result_label = "failed"
                print(f"[fail ] {claim.name} (rc={rc})")

            recent_item = {
                "name": claim.name,
                "result": result_label,
                "duration_sec": duration_sec,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            }
            if upload_config is not None and rc == 0:
                recent_item["upload_stage"] = (upload_result or {}).get("stage")
            recent.insert(0, recent_item)
            del recent[5:]

            processed += 1

        final_upload_result = _flush_upload_batch()
        if final_upload_result is not None:
            recent.insert(0, {
                "name": final_upload_result.get("batch_item_id", f"batch_{upload_batch_index:04d}"),
                "result": "uploaded" if final_upload_result.get("ok") else "upload_failed",
                "duration_sec": 0,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "upload_stage": final_upload_result.get("stage"),
            })
            del recent[5:]

        elapsed = int(time.time() - t0)
        counts_after = {t: queue_counts(root, t) for t in (targets or list(TASK_TYPES))}

        _write_state(_snapshot(status="completed", finished=True), state_file=state_file)

        print()
        print(
            f"=== 워커 종료: {processed}개 처리 "
            f"(done={done_count}, failed={fail_count}, upload_failed={upload_fail_count}, 소요 {elapsed}s) ==="
        )
        for tt, c in counts_after.items():
            print(f"  {tt}: pending={c.pending} running={c.running} done={c.done} failed={c.failed}")

        return 0 if fail_count == 0 and upload_fail_count == 0 else 1

    except KeyboardInterrupt:
        _write_state(_snapshot(status="interrupted", finished=True), state_file=state_file)
        print("\n[interrupt] 워커 중단. running/에 남은 파일은 --recover로 복구 가능.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
