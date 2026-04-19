#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy", "pyyaml"]
# ///
"""
job_queue.worker atomic 전이 테스트.

실행:
    uv run tests/test_worker.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.job_queue import (  # noqa: E402
    QueueState,
    claim_one,
    ensure_queue_dirs,
    mark_done,
    mark_failed,
    queue_counts,
    queue_dir,
    recover_running_to_pending,
    write_plans,
)
from aic_collector.sampler import sample_scenes  # noqa: E402

TEMPLATE_PATH = PROJECT_DIR / "configs/community_random_config.yaml"


def _seed_queue(root: Path, sfp_count: int = 3, sc_count: int = 0) -> None:
    if sfp_count:
        write_plans(sample_scenes({}, "sfp", sfp_count, 42), root, TEMPLATE_PATH)
    if sc_count:
        write_plans(sample_scenes({}, "sc", sc_count, 42), root, TEMPLATE_PATH)


def test_claim_empty_returns_none() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert claim_one(root) is None


def test_claim_picks_first_pending_in_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_queue(root, sfp_count=3)
        claim = claim_one(root, ["sfp"])
        assert claim is not None
        assert claim.task_type == "sfp"
        assert claim.sample_index == 0
        assert claim.running_path.exists()
        assert claim.running_path.parent == queue_dir(root, "sfp", QueueState.RUNNING)
        # pending에서는 사라짐
        assert not (queue_dir(root, "sfp", QueueState.PENDING) / claim.name).exists()


def test_claim_skips_unrelated_task_type() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_queue(root, sfp_count=0, sc_count=2)
        # sfp만 요청 → None
        assert claim_one(root, ["sfp"]) is None
        # sc 요청 → 있음
        c = claim_one(root, ["sc"])
        assert c is not None and c.task_type == "sc"


def test_claim_round_robin_multiple_task_types() -> None:
    """task_types 순서대로 탐색 — 앞 task_type이 비면 다음을 본다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_queue(root, sfp_count=0, sc_count=1)
        c = claim_one(root, ["sfp", "sc"])
        assert c is not None and c.task_type == "sc"


def test_multiple_claims_exhaust_queue() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_queue(root, sfp_count=3)
        seen: set[int] = set()
        while True:
            c = claim_one(root, ["sfp"])
            if c is None:
                break
            seen.add(c.sample_index)
        assert seen == {0, 1, 2}


def test_mark_done_moves_to_done() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_queue(root, sfp_count=1)
        claim = claim_one(root, ["sfp"])
        assert claim is not None
        mark_done(claim, root)
        c = queue_counts(root, "sfp")
        assert c.pending == 0 and c.running == 0 and c.done == 1 and c.failed == 0


def test_mark_failed_moves_to_failed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_queue(root, sfp_count=1)
        claim = claim_one(root, ["sfp"])
        assert claim is not None
        mark_failed(claim, root)
        c = queue_counts(root, "sfp")
        assert c.pending == 0 and c.running == 0 and c.done == 0 and c.failed == 1


def test_mark_done_conflict_raises() -> None:
    """done/에 이미 같은 이름이 있으면 덮어쓰지 않고 에러."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ensure_queue_dirs(root)
        _seed_queue(root, sfp_count=1)
        # 같은 이름 파일을 done/에 미리 둠
        (queue_dir(root, "sfp", QueueState.DONE) / "config_sfp_0000.yaml").write_text("x")
        claim = claim_one(root, ["sfp"])
        assert claim is not None
        try:
            mark_done(claim, root)
        except FileExistsError:
            return
        raise AssertionError("done 충돌 시 FileExistsError 기대")


def test_recover_running_moves_back_to_pending() -> None:
    """비정상 종료로 running에 남은 파일 복구."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_queue(root, sfp_count=2)
        # 한 개 claim하고 mark 하지 않음 (비정상 종료 시뮬레이션)
        c = claim_one(root, ["sfp"])
        assert c is not None
        counts_before = queue_counts(root, "sfp")
        assert counts_before.running == 1
        assert counts_before.pending == 1

        moved = recover_running_to_pending(root, "sfp")
        assert moved == 1

        counts_after = queue_counts(root, "sfp")
        assert counts_after.running == 0
        assert counts_after.pending == 2


def test_recover_skips_existing_pending_conflicts() -> None:
    """복구 시 pending에 동일 이름이 있으면 건너뛴다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ensure_queue_dirs(root)
        name = "config_sfp_0001.yaml"
        (queue_dir(root, "sfp", QueueState.RUNNING) / name).write_text("running_content")
        (queue_dir(root, "sfp", QueueState.PENDING) / name).write_text("pending_content")
        moved = recover_running_to_pending(root, "sfp")
        assert moved == 0
        # pending은 원래 내용 보존
        assert (queue_dir(root, "sfp", QueueState.PENDING) / name).read_text() == "pending_content"


def test_claim_skips_unrelated_filenames() -> None:
    """디렉토리에 예상 외 파일(예: .txt, 잘못된 task_type)이 있어도 claim 안 함."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ensure_queue_dirs(root)
        pending = queue_dir(root, "sfp", QueueState.PENDING)
        (pending / "notes.txt").write_text("x")
        (pending / "config_sc_0000.yaml").write_text("x")  # 잘못된 task
        (pending / "random.yaml").write_text("x")
        assert claim_one(root, ["sfp"]) is None


def main() -> int:
    tests = [
        ("claim: 빈 큐→None", test_claim_empty_returns_none),
        ("claim: 첫 pending을 running으로", test_claim_picks_first_pending_in_order),
        ("claim: 다른 task_type 스킵", test_claim_skips_unrelated_task_type),
        ("claim: task_types 순차 탐색", test_claim_round_robin_multiple_task_types),
        ("claim: 반복으로 큐 소진", test_multiple_claims_exhaust_queue),
        ("mark_done: running→done", test_mark_done_moves_to_done),
        ("mark_failed: running→failed", test_mark_failed_moves_to_failed),
        ("mark_done: 충돌 시 FileExistsError", test_mark_done_conflict_raises),
        ("recover: running→pending", test_recover_running_moves_back_to_pending),
        ("recover: pending 충돌 보존", test_recover_skips_existing_pending_conflicts),
        ("claim: 무관 파일 무시", test_claim_skips_unrelated_filenames),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"✅ {name}")
        except AssertionError as e:
            print(f"❌ {name} — {e}")
            failed += 1
        except Exception as e:
            print(f"💥 {name} — {type(e).__name__}: {e}")
            failed += 1
    total = len(tests)
    print(f"\n{total - failed}/{total} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
