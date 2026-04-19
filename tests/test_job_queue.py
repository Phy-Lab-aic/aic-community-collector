#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy", "pyyaml"]
# ///
"""
job_queue 패키지 단위 테스트.

실행:
    uv run tests/test_job_queue.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.job_queue import (  # noqa: E402
    QueueCounts,
    QueueState,
    TASK_TYPES,
    all_counts,
    ensure_queue_dirs,
    legacy_dir,
    list_configs,
    list_legacy,
    migrate_legacy_to_pending,
    next_sample_index,
    queue_counts,
    queue_dir,
    write_plan,
    write_plans,
)
from aic_collector.sampler import sample_scenes  # noqa: E402

TEMPLATE_PATH = PROJECT_DIR / "configs/community_random_config.yaml"


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def test_layout_queue_dir_shape() -> None:
    root = Path("/tmp/xyz")
    d = queue_dir(root, "sfp", QueueState.PENDING)
    assert d == Path("/tmp/xyz/sfp/pending")


def test_layout_invalid_task_type_raises() -> None:
    for bad in ("nic", "SFP", "", "sfp "):
        try:
            queue_dir(Path("/tmp"), bad, QueueState.PENDING)
        except ValueError:
            continue
        raise AssertionError(f"task_type={bad!r}에서 ValueError 기대")


def test_ensure_queue_dirs_creates_all() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ensure_queue_dirs(root)
        for t in TASK_TYPES:
            for s in QueueState:
                d = root / t / s.value
                assert d.is_dir(), f"{d} 생성되지 않음"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def test_counts_empty_root() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        c = queue_counts(root, "sfp")
        assert c == QueueCounts(0, 0, 0, 0, 0)
        assert c.total == 0


def test_counts_after_write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plans = sample_scenes({}, "sfp", 3, 42)
        write_plans(plans, root, TEMPLATE_PATH)
        c = queue_counts(root, "sfp")
        assert c.pending == 3
        assert c.running == 0
        assert c.done == 0
        assert c.failed == 0
        assert c.legacy == 0


def test_legacy_counted_separately() -> None:
    """상태 디렉토리 밖의 flat *.yaml은 legacy로 집계."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        flat = root / "sfp"
        flat.mkdir(parents=True)
        (flat / "config_sfp_0000.yaml").write_text("dummy")
        (flat / "config_sfp_0001.yaml").write_text("dummy")
        # 상태 디렉토리에는 없음
        c = queue_counts(root, "sfp")
        assert c.legacy == 2
        assert c.pending == 0


def test_all_counts_covers_task_types() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_plans(sample_scenes({}, "sfp", 2, 42), root, TEMPLATE_PATH)
        write_plans(sample_scenes({}, "sc", 1, 42), root, TEMPLATE_PATH)
        ac = all_counts(root)
        assert set(ac.keys()) == set(TASK_TYPES)
        assert ac["sfp"].pending == 2
        assert ac["sc"].pending == 1


def test_list_configs_sorted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plans = sample_scenes({}, "sfp", 5, 42)
        write_plans(plans, root, TEMPLATE_PATH)
        files = list_configs(root, "sfp", QueueState.PENDING)
        assert [f.name for f in files] == [
            "config_sfp_000000.yaml",
            "config_sfp_000001.yaml",
            "config_sfp_000002.yaml",
            "config_sfp_000003.yaml",
            "config_sfp_000004.yaml",
        ]


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def test_write_plan_creates_pending_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plans = sample_scenes({}, "sfp", 1, 42)
        path = write_plan(plans[0], root, TEMPLATE_PATH)
        assert path.exists()
        assert path.parent == root / "sfp" / "pending"
        assert path.name == "config_sfp_000000.yaml"


def test_write_plan_sc_task_type_routes_correctly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plans = sample_scenes({}, "sc", 1, 42)
        path = write_plan(plans[0], root, TEMPLATE_PATH)
        assert path.parent == root / "sc" / "pending"
        assert path.name == "config_sc_000000.yaml"


def test_write_plan_explicit_index_width_is_preserved() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plans = sample_scenes({}, "sfp", 1, 42, start_index=50)
        path = write_plan(plans[0], root, TEMPLATE_PATH, index_width=4)
        assert path.name == "config_sfp_0050.yaml"


def test_next_index_empty_returns_zero() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assert next_sample_index(root, "sfp") == 0


def test_next_index_after_pending_writes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_plans(sample_scenes({}, "sfp", 3, 42), root, TEMPLATE_PATH)
        assert next_sample_index(root, "sfp") == 3


def test_next_index_counts_all_states() -> None:
    """done/failed/running에도 같은 NNNN이 있으면 그 다음부터."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ensure_queue_dirs(root)
        (root / "sfp" / "done" / "config_sfp_0007.yaml").write_text("x")
        (root / "sfp" / "failed" / "config_sfp_0003.yaml").write_text("x")
        (root / "sfp" / "running" / "config_sfp_0005.yaml").write_text("x")
        assert next_sample_index(root, "sfp") == 8


def test_next_index_includes_legacy() -> None:
    """상태 디렉토리 밖 flat 파일도 번호 계산에 포함 (중복 방지)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        flat = root / "sfp"
        flat.mkdir(parents=True)
        (flat / "config_sfp_0054.yaml").write_text("dummy")
        # pending은 비어있지만 legacy=54 → 다음은 55
        assert next_sample_index(root, "sfp") == 55


def test_next_index_mixed_widths_returns_next_numeric_index() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ensure_queue_dirs(root)
        (root / "sfp" / "pending" / "config_sfp_0050.yaml").write_text("x")
        (root / "sfp" / "done" / "config_sfp_200000.yaml").write_text("x")
        assert next_sample_index(root, "sfp") == 200001


def test_next_index_skips_unrelated_files() -> None:
    """task_type이 다른 파일은 번호 계산에 포함 안 됨."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_plans(sample_scenes({}, "sc", 5, 42), root, TEMPLATE_PATH)
        # sc만 5개 있지만 sfp는 여전히 0부터
        assert next_sample_index(root, "sfp") == 0
        assert next_sample_index(root, "sc") == 5


def test_migrate_legacy_to_pending_moves_and_counts() -> None:
    """Legacy 파일을 pending/으로 이동하고 카운트 반환."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # legacy 파일 준비
        (root / "sfp").mkdir(parents=True)
        (root / "sc").mkdir(parents=True)
        for i in range(3):
            (root / "sfp" / f"config_sfp_{i:04d}.yaml").write_text("x")
        for i in range(2):
            (root / "sc" / f"config_sc_{i:04d}.yaml").write_text("x")

        moved = migrate_legacy_to_pending(root)
        assert moved == {"sfp": 3, "sc": 2}

        # legacy는 비었고 pending에 옮겨짐
        assert queue_counts(root, "sfp").legacy == 0
        assert queue_counts(root, "sfp").pending == 3
        assert queue_counts(root, "sc").legacy == 0
        assert queue_counts(root, "sc").pending == 2


def test_migrate_legacy_preserves_existing_pending() -> None:
    """pending에 이미 동일 이름이 있으면 legacy를 덮어쓰지 않음."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sfp").mkdir(parents=True)
        (root / "sfp" / "config_sfp_0000.yaml").write_text("legacy_content")
        pending = root / "sfp" / "pending"
        pending.mkdir(parents=True)
        (pending / "config_sfp_0000.yaml").write_text("pending_content")

        moved = migrate_legacy_to_pending(root)
        assert moved == {"sfp": 0, "sc": 0}, "충돌 시 건너뛰어야 함"
        # pending은 원래 내용 보존
        assert (pending / "config_sfp_0000.yaml").read_text() == "pending_content"
        # legacy는 남아있음 (수동 처리 유도)
        assert (root / "sfp" / "config_sfp_0000.yaml").exists()


def test_write_plans_multiple_uses_plan_indices() -> None:
    """write_plans는 plan.sample_index를 그대로 파일명에 씀."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # start_index=5로 5,6,7번 생성
        plans = sample_scenes({}, "sfp", 3, 42, start_index=5)
        paths = write_plans(plans, root, TEMPLATE_PATH)
        names = [p.name for p in paths]
        assert names == [
            "config_sfp_000005.yaml",
            "config_sfp_000006.yaml",
            "config_sfp_000007.yaml",
        ]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    tests = [
        ("layout: queue_dir 경로", test_layout_queue_dir_shape),
        ("layout: invalid task_type", test_layout_invalid_task_type_raises),
        ("layout: ensure_queue_dirs 전체 생성", test_ensure_queue_dirs_creates_all),
        ("state: 빈 root 카운트", test_counts_empty_root),
        ("state: write 후 pending 카운트", test_counts_after_write),
        ("state: legacy 별도 집계", test_legacy_counted_separately),
        ("state: all_counts task_type 커버", test_all_counts_covers_task_types),
        ("state: list_configs 정렬", test_list_configs_sorted),
        ("writer: write_plan pending 경로", test_write_plan_creates_pending_file),
        ("writer: SC task_type 라우팅", test_write_plan_sc_task_type_routes_correctly),
        ("writer: write_plan explicit width", test_write_plan_explicit_index_width_is_preserved),
        ("writer: next_index 빈 root=0", test_next_index_empty_returns_zero),
        ("writer: next_index pending 반영", test_next_index_after_pending_writes),
        ("writer: next_index 모든 상태 스캔", test_next_index_counts_all_states),
        ("writer: next_index legacy 포함", test_next_index_includes_legacy),
        ("writer: next_index mixed width", test_next_index_mixed_widths_returns_next_numeric_index),
        ("writer: next_index task_type 격리", test_next_index_skips_unrelated_files),
        ("writer: write_plans 인덱스 보존", test_write_plans_multiple_uses_plan_indices),
        ("writer: migrate_legacy 이동", test_migrate_legacy_to_pending_moves_and_counts),
        ("writer: migrate_legacy 충돌 보존", test_migrate_legacy_preserves_existing_pending),
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
