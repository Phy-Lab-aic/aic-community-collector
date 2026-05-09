"""Unit tests for postprocess_run's Hz / timestamp quality analysis."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aic_collector.postprocess_run import (
    DEFAULT_TARGET_HZ,
    HZ_MIN_RATIO,
    SIM_WALL_MISMATCH_RATIO,
    _analyze_topic,
    _camera_sync_stats,
    _is_rate_critical,
    _percentile,
    compute_hz_report,
    hz_report_warnings,
)


def _ts_series(start_ns: int, count: int, period_ns: int) -> list[int]:
    return [start_ns + i * period_ns for i in range(count)]


def test_is_rate_critical_classification() -> None:
    assert _is_rate_critical("/joint_states") is True
    assert _is_rate_critical("/left_camera/image") is True
    assert _is_rate_critical("/center_camera/image") is True
    assert _is_rate_critical("/aic_controller/controller_state") is True
    assert _is_rate_critical("/fts_broadcaster/wrench") is True
    # Low-rate / event topics are excluded.
    assert _is_rate_critical("/tf_static") is False
    assert _is_rate_critical("/scoring/insertion_event") is False
    assert _is_rate_critical("/aic/gazebo/contacts/off_limit") is False
    # Random topic stays untracked.
    assert _is_rate_critical("/some/other/topic") is False


def test_percentile_basic() -> None:
    assert _percentile([], 50.0) == 0.0
    assert _percentile([1.0], 50.0) == 1.0
    assert _percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50.0) == pytest.approx(3.0)
    # p95 of 1..100 should be very close to 95.
    assert _percentile([float(v) for v in range(1, 101)], 95.0) == pytest.approx(
        95.05
    )


def test_analyze_topic_passes_at_target() -> None:
    period_ns = 50_000_000  # 20 Hz
    series = _ts_series(0, 200, period_ns)  # ~10 s @ 20 Hz
    stats = _analyze_topic("/joint_states", series, DEFAULT_TARGET_HZ)
    assert stats["rate_critical"] is True
    assert stats["valid"] is True
    assert stats["actual_hz"] == pytest.approx(20.0, abs=0.05)
    assert stats["median_gap_ms"] == pytest.approx(50.0, abs=0.1)
    # No drops at exactly target rate.
    assert stats["dropped_estimate"] == 0
    assert stats["expected_count"] >= 199


def test_analyze_topic_flags_below_threshold() -> None:
    # 10 Hz on a rate-critical topic should fail the 14 Hz threshold.
    period_ns = 100_000_000
    series = _ts_series(0, 100, period_ns)  # ~10 s @ 10 Hz
    stats = _analyze_topic("/center_camera/image", series, DEFAULT_TARGET_HZ)
    assert stats["valid"] is False
    assert stats["actual_hz"] < DEFAULT_TARGET_HZ * HZ_MIN_RATIO
    # Roughly half the expected frames at 20 Hz are missing.
    assert stats["dropped_estimate"] > 0


def test_analyze_topic_low_rate_topic_does_not_fail() -> None:
    # Single message simulates /tf_static — pass without warnings.
    stats = _analyze_topic("/tf_static", [1_000_000_000], DEFAULT_TARGET_HZ)
    assert stats["rate_critical"] is False
    assert stats["valid"] is True


def test_camera_sync_stats_detects_skew() -> None:
    # Three cameras at 20 Hz: center (primary) and left aligned, right offset
    # by 40 ms — every nearest-right neighbour for a center tick is 10 ms or
    # 40 ms away, so several ticks should breach the 25 ms tolerance.
    period_ns = 50_000_000
    left = _ts_series(0, 50, period_ns)
    center = _ts_series(0, 50, period_ns)
    right = _ts_series(40_000_000, 50, period_ns)
    stats = _camera_sync_stats(
        {
            "/left_camera/image": left,
            "/center_camera/image": center,
            "/right_camera/image": right,
        },
        DEFAULT_TARGET_HZ,
    )
    assert stats is not None
    # Sorted primary = /center_camera/image (alphabetical).
    assert stats["primary"] == "/center_camera/image"
    assert stats["max_skew_ms"] >= 40.0
    assert stats["out_of_tolerance_frames"] >= 1
    assert stats["total_frames"] == len(center)


def test_compute_hz_report_returns_none_for_missing_bag(tmp_path: Path) -> None:
    assert compute_hz_report(None) is None
    assert compute_hz_report(tmp_path / "nope") is None
    bag = tmp_path / "bag"
    bag.mkdir()
    # No mcap inside.
    assert compute_hz_report(bag) is None


def test_hz_report_warnings_renders_drift_message() -> None:
    report = {
        "summary": {
            "all_pass": False,
            "worst_topic": "/center_camera/image",
            "worst_hz": 11.0,
            "drop_rate": 0.32,
            "total_expected": 200,
            "total_dropped_estimate": 64,
        },
        "duration_check": {
            "bag_duration_sec": 10.0,
            "episode_wall_duration_sec": 14.0,
            "drift_ratio": 0.4,
            "mismatch": True,
            "threshold_ratio": SIM_WALL_MISMATCH_RATIO,
        },
        "camera_sync": {
            "out_of_tolerance_frames": 12,
            "total_frames": 50,
            "tolerance_ms": 25.0,
            "p95_skew_ms": 30.0,
        },
        "target_hz": DEFAULT_TARGET_HZ,
        "min_ratio": HZ_MIN_RATIO,
    }
    warnings = hz_report_warnings(report, "trial_1")
    joined = "\n".join(warnings)
    assert "Hz 미달" in joined
    assert "/center_camera/image" in joined
    assert "프레임 드롭률" in joined
    assert "sim_time/wall_time" in joined
    assert "카메라 동기 어긋남" in joined


def test_hz_report_warnings_quiet_when_clean() -> None:
    report = {
        "summary": {
            "all_pass": True,
            "worst_topic": "/joint_states",
            "worst_hz": 19.8,
            "drop_rate": 0.001,
            "total_expected": 200,
            "total_dropped_estimate": 0,
        },
        "duration_check": {
            "bag_duration_sec": 10.0,
            "episode_wall_duration_sec": 10.05,
            "drift_ratio": 0.005,
            "mismatch": False,
            "threshold_ratio": SIM_WALL_MISMATCH_RATIO,
        },
        "camera_sync": None,
        "target_hz": DEFAULT_TARGET_HZ,
        "min_ratio": HZ_MIN_RATIO,
    }
    assert hz_report_warnings(report, "trial_1") == []


def test_hz_report_writes_json_via_synthetic_mcap(tmp_path: Path) -> None:
    """End-to-end: build a tiny MCAP and confirm compute_hz_report finds it.

    Skipped automatically when the optional `mcap` writer isn't available
    (the runtime dep is `mcap>=0.0.10` per pyproject, which ships the writer).
    """
    mcap_writer = pytest.importorskip("mcap.writer")

    bag_dir = tmp_path / "bag"
    bag_dir.mkdir()
    mcap_path = bag_dir / "trial.mcap"

    period_ns = 50_000_000
    with open(mcap_path, "wb") as fp:
        writer = mcap_writer.Writer(fp)
        writer.start()
        # Schemas/channels are required even for raw blobs.
        schema_id = writer.register_schema(
            name="std_msgs/String", encoding="ros2msg", data=b"string data"
        )
        ch_joint = writer.register_channel(
            topic="/joint_states", message_encoding="cdr", schema_id=schema_id
        )
        ch_cam = writer.register_channel(
            topic="/center_camera/image",
            message_encoding="cdr",
            schema_id=schema_id,
        )
        for i in range(40):
            t = i * period_ns
            writer.add_message(
                channel_id=ch_joint,
                log_time=t,
                publish_time=t,
                data=b"\x00",
                sequence=i,
            )
            # Camera at half the rate (10 Hz → should fail).
            if i % 2 == 0:
                writer.add_message(
                    channel_id=ch_cam,
                    log_time=t,
                    publish_time=t,
                    data=b"\x00",
                    sequence=i,
                )
        writer.finish()

    report = compute_hz_report(bag_dir, target_hz=DEFAULT_TARGET_HZ)
    assert report is not None
    summary = report["summary"]
    assert summary["all_pass"] is False
    assert summary["worst_topic"] == "/center_camera/image"
    # /joint_states ≈ 20 Hz, /center_camera/image ≈ 10 Hz.
    topic_hz = {t["topic"]: t["actual_hz"] for t in report["topics"]}
    assert topic_hz["/joint_states"] == pytest.approx(20.0, abs=0.5)
    assert topic_hz["/center_camera/image"] == pytest.approx(10.0, abs=0.5)


def test_hz_report_persists_alongside_tags(tmp_path: Path) -> None:
    """Confirm the JSON shape that flow.py + webapp.py rely on."""
    report = {
        "schema_version": "0.1.0",
        "target_hz": DEFAULT_TARGET_HZ,
        "min_ratio": HZ_MIN_RATIO,
        "topics": [],
        "summary": {
            "avg_hz": 20.0,
            "worst_topic": "/joint_states",
            "worst_hz": 20.0,
            "all_pass": True,
            "total_dropped_estimate": 0,
            "total_expected": 0,
            "drop_rate": 0.0,
        },
        "duration_check": {
            "bag_duration_sec": 10.0,
            "episode_wall_duration_sec": 10.0,
            "drift_ratio": 0.0,
            "mismatch": False,
            "threshold_ratio": SIM_WALL_MISMATCH_RATIO,
        },
        "camera_sync": None,
    }
    target = tmp_path / "hz_report.json"
    target.write_text(json.dumps(report))
    parsed = json.loads(target.read_text())
    assert parsed["summary"]["all_pass"] is True
    assert hz_report_warnings(parsed, "trial_1") == []
