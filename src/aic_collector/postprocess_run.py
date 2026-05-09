#!/usr/bin/env python3
"""
단일 run의 산출물 재편 유틸.

입력:
  - 엔진 산출물 (~/aic_results/) — scoring.yaml + bag_trial_N_*/
  - episode demos (run 전용 임시 디렉토리) — episode_*/metadata.json
  - 사용한 엔진 config
  - policy 이름 + seed

출력 (`<run_dir>/`):
  config.yaml              # 엔진에 주입한 config (복사)
  policy.txt               # 사용한 policy
  seed.txt                 # 샘플링 seed
  scoring_run.yaml         # 엔진의 원본 scoring.yaml (참고용 보존)
  trial_<N>_score<NNN>/
    episode/               # episode_NNNN의 내부 파일 전부 (metadata.json 포함)
    bag/                   # bag_trial_N_*/ (mcap + metadata.yaml)
    scoring.yaml           # run scoring에서 해당 trial만 추출
    tags.json              # 자동 태깅

Usage:
    python postprocess_run.py \\
        --run-dir ~/aic_community_e2e/run_01_20260408_230000 \\
        --engine-results ~/aic_results \\
        --demo-dir /tmp/e2e_demos_run01 \\
        --engine-config /tmp/engine_config_run01.yaml \\
        --policy cheatcode \\
        --seed 42
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.stderr.write("pyyaml 필요: pip install pyyaml\n")
    sys.exit(1)


TAGS_SCHEMA_VERSION = "0.1.0"
HZ_REPORT_SCHEMA_VERSION = "0.1.0"

# Default rate target — collector pipeline assumes 20 Hz sim publishing.
DEFAULT_TARGET_HZ = 20.0
# Topics that legitimately publish at sub-target rates (event/latched topics);
# they are still measured but never trigger an Hz warning.
LOW_RATE_TOPIC_PREFIXES: tuple[str, ...] = (
    "/tf_static",
    "/scoring/insertion_event",
    "/aic/gazebo/contacts/off_limit",
)
# Topics that should be measured against the target Hz when present.
RATE_CRITICAL_TOPIC_HINTS: tuple[str, ...] = (
    "/joint_states",
    "/fts_broadcaster/wrench",
    "/aic_controller/controller_state",
    "/_camera/image",  # matches /left_camera/image, /center_camera/image, ...
)
HZ_MIN_RATIO = 0.7
SIM_WALL_MISMATCH_RATIO = 0.10  # 10% drift between bag and episode duration


# ---------------------------------------------------------------------------
# scoring.yaml 분해
# ---------------------------------------------------------------------------


def split_scoring(scoring: dict) -> dict[str, dict]:
    """
    run 전체 scoring dict에서 `trial_<N>` 키만 뽑아 trial별 dict 반환.

    각 trial dict는 원본의 tier_1/2/3을 포함하고 total 필드를 추가로 계산.
    """
    per_trial: dict[str, dict] = {}
    for key, value in scoring.items():
        if not key.startswith("trial_"):
            continue
        if not isinstance(value, dict):
            continue
        tier_scores = []
        for tier_key in ("tier_1", "tier_2", "tier_3"):
            tier = value.get(tier_key)
            if isinstance(tier, dict) and isinstance(tier.get("score"), (int, float)):
                tier_scores.append(float(tier["score"]))
        total = sum(tier_scores) if tier_scores else None
        per_trial[key] = {
            "total": total,
            **value,  # tier_1/2/3 원본 그대로
        }
    return per_trial


def trial_total_score(trial_scoring: dict) -> int:
    """trial scoring dict에서 총점을 int로 반환 (디렉토리명용)."""
    t = trial_scoring.get("total")
    if t is None:
        return 0
    return int(round(float(t)))


# ---------------------------------------------------------------------------
# bag trial 매칭
# ---------------------------------------------------------------------------


BAG_PAT = re.compile(r"^bag_trial_(\d+)(?:_|$)")


def find_bag_for_trial(engine_results: Path, trial_num: int) -> Path | None:
    """~/aic_results/bag_trial_<N>_*/ 디렉토리 경로 반환."""
    for child in sorted(engine_results.iterdir()):
        if not child.is_dir():
            continue
        m = BAG_PAT.match(child.name)
        if m and int(m.group(1)) == trial_num:
            return child
    return None


def _bag_storage_config(engine_config: Path | None) -> dict[str, str]:
    """Read collector-owned rosbag storage conversion settings from engine config."""
    if not engine_config or not engine_config.exists():
        return {}
    try:
        with open(engine_config) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return {}

    scoring = cfg.get("scoring") or {}
    if not isinstance(scoring, dict):
        return {}

    out: dict[str, str] = {}
    for key in ("storage_id", "storage_preset_profile", "storage_config_uri"):
        value = scoring.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def _compress_bag_storage(bag_dir: Path, storage_cfg: dict[str, str]) -> bool:
    """Convert a moved bag directory to MCAP storage compression in-place.

    The engine records the source bag. Collector postprocess owns this conversion
    so upstream AIC scoring code can stay unchanged. If conversion fails, the
    original bag directory is kept.
    """
    preset = storage_cfg.get("storage_preset_profile")
    config_uri = storage_cfg.get("storage_config_uri")
    if not preset and not config_uri:
        return False

    if shutil.which("ros2") is None:
        print("[warn] ros2 CLI 없음 — bag storage compression 건너뜀")
        return False

    output_dir = bag_dir.with_name(f"{bag_dir.name}_storage_compressed")
    if output_dir.exists():
        shutil.rmtree(output_dir)

    output_spec: dict[str, Any] = {
        "uri": str(output_dir),
        "storage_id": storage_cfg.get("storage_id", "mcap"),
        "all": True,
    }
    if preset:
        output_spec["storage_preset_profile"] = preset
    if config_uri:
        output_spec["storage_config_uri"] = config_uri

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump({"output_bags": [output_spec]}, f, sort_keys=False)
        convert_config = Path(f.name)

    try:
        proc = subprocess.run(
            ["ros2", "bag", "convert", "-i", str(bag_dir), "-o", str(convert_config)],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except Exception as exc:
        print(f"[warn] bag storage compression 실패: {exc}")
        if output_dir.exists():
            shutil.rmtree(output_dir)
        return False
    finally:
        convert_config.unlink(missing_ok=True)

    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        print(
            f"[warn] bag storage compression 실패(returncode={proc.returncode}): "
            f"{stderr}"
        )
        if output_dir.exists():
            shutil.rmtree(output_dir)
        return False

    if not output_dir.exists() or not any(output_dir.glob("*.mcap")):
        print("[warn] bag storage compression 결과 MCAP 없음 — 원본 유지")
        if output_dir.exists():
            shutil.rmtree(output_dir)
        return False

    shutil.rmtree(bag_dir)
    shutil.move(str(output_dir), str(bag_dir))
    detail = preset or config_uri or "storage config"
    print(f"[ok] bag storage compression 적용: {bag_dir} ({detail})")
    return True


# ---------------------------------------------------------------------------
# episode trial 매칭
# ---------------------------------------------------------------------------


def load_trial_order(engine_config: Path) -> list[str]:
    """엔진 config에서 `trials` dict의 삽입 순서대로 키 리스트 반환.

    엔진(`aic_engine.cpp`)이 dict을 순회 실행하므로 이 순서가 곧 실행 순서.
    """
    if not engine_config.exists():
        return []
    with open(engine_config) as f:
        cfg = yaml.safe_load(f) or {}
    trials = cfg.get("trials", {}) or {}
    return list(trials.keys())


def find_episode_by_order(
    demo_dir: Path, trial_key: str, trial_order: list[str]
) -> Path | None:
    """trial_key의 엔진 실행 순서 번호로 `episode_NNNN/` 찾기.

    Episode는 insert_cable 호출 순서대로 episode_0000, episode_0001, ... 로 저장됨.
    CollectCheatCode/CollectWrapper의 _trial_counter는 로컬 카운터라 실제 trial 번호와
    무관하므로 metadata.json의 `trial` 필드는 신뢰할 수 없음.

    Returns:
        demo_dir/episode_<index:04d>/ Path (존재하면), 없으면 None.
    """
    if not demo_dir.exists() or trial_key not in trial_order:
        return None
    idx = trial_order.index(trial_key)
    ep_path = demo_dir / f"episode_{idx:04d}"
    return ep_path if ep_path.exists() else None


def fix_episode_metadata_trial(ep_dir: Path, trial_num: int) -> None:
    """이동한 episode의 metadata.json `trial` 필드를 실제 trial 번호로 갱신.

    CollectCheatCode/CollectWrapper는 로컬 카운터를 기록하므로 부분 수집 시 불일치.
    Postprocess에서 올바른 trial 번호로 덮어쓴다.
    """
    meta_path = ep_dir / "metadata.json"
    if not meta_path.exists():
        return
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except Exception:
        return
    meta["trial"] = trial_num
    meta["trial_key"] = f"trial_{trial_num}"  # 명시적 추가
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Fallback: bag / scoring / config 에서 대체 데이터 추출
# ---------------------------------------------------------------------------


def _bag_duration_sec(bag_dir: Path | None) -> float | None:
    """bag/metadata.yaml의 duration(nanoseconds)에서 초 단위 값 반환."""
    if not bag_dir:
        return None
    meta_path = bag_dir / "metadata.yaml"
    if not meta_path.exists():
        return None
    try:
        with open(meta_path) as f:
            meta = yaml.safe_load(f) or {}
        ns = meta.get("rosbag2_bagfile_information", {}).get("duration", {}).get("nanoseconds")
        if ns is not None:
            return round(int(ns) / 1e9, 3)
    except Exception:
        pass
    return None



def _scoring_duration_sec(trial_scoring: dict) -> float | None:
    """scoring tier_2 > duration > message에서 'Task duration: N.NN seconds' 파싱."""
    tier_2 = trial_scoring.get("tier_2") or {}
    cats = tier_2.get("categories") or {}
    dur_msg = str((cats.get("duration") or {}).get("message", ""))
    m = re.search(r"Task duration:\s*([\d.]+)\s*seconds", dur_msg)
    if m:
        return round(float(m.group(1)), 3)
    return None


def _config_task_info(engine_config: Path | None, trial_key: str) -> dict:
    """engine config의 trials.<trial_key>.tasks.task_1에서 cable/plug/port_type 추출."""
    if not engine_config or not engine_config.exists():
        return {}
    try:
        with open(engine_config) as f:
            cfg = yaml.safe_load(f) or {}
        task = (cfg.get("trials", {}).get(trial_key, {}).get("tasks") or {}).get("task_1") or {}
        info: dict[str, Any] = {}
        for key in ("cable_type", "plug_type", "port_type"):
            if key in task:
                info[key] = task[key]
        return info
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Hz / timestamp 품질 분석
# ---------------------------------------------------------------------------


def _is_rate_critical(topic: str) -> bool:
    """Return True if `topic` should be measured against the target Hz."""
    if any(topic.startswith(prefix) for prefix in LOW_RATE_TOPIC_PREFIXES):
        return False
    for hint in RATE_CRITICAL_TOPIC_HINTS:
        if hint.startswith("/_") and hint.endswith("/image"):
            # Wildcard cam pattern: anything ending in /image.
            if topic.endswith("/image"):
                return True
        elif topic == hint or topic.startswith(hint + "/"):
            return True
    return False


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    pct = max(0.0, min(100.0, pct))
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def _collect_log_times(mcap_path: Path) -> dict[str, list[int]]:
    """Walk an MCAP file and return ``{topic: [log_time_ns, ...]}``.

    Decoding is intentionally skipped — we only need timestamps and topic
    names, which are present on Channel/Message records.
    """
    try:
        from mcap.stream_reader import StreamReader
    except ImportError:
        print("[warn] mcap 패키지 없음 — Hz 분석 건너뜀")
        return {}

    channels: dict[int, str] = {}
    timestamps: dict[str, list[int]] = {}
    try:
        with open(mcap_path, "rb") as fp:
            for record in StreamReader(fp, record_size_limit=None).records:
                rtype = type(record).__name__
                if rtype == "Channel":
                    channels[record.id] = record.topic
                elif rtype == "Message":
                    topic = channels.get(record.channel_id)
                    if topic is None:
                        continue
                    timestamps.setdefault(topic, []).append(int(record.log_time))
    except Exception as exc:
        print(f"[warn] Hz 분석 — MCAP 읽기 실패 ({mcap_path.name}): {exc}")
        return {}
    return timestamps


def _analyze_topic(
    topic: str,
    log_times_ns: list[int],
    target_hz: float,
) -> dict[str, Any]:
    """Compute timing statistics for a single topic."""
    rate_critical = _is_rate_critical(topic)
    count = len(log_times_ns)
    if count < 2:
        return {
            "topic": topic,
            "rate_critical": rate_critical,
            "message_count": count,
            "duration_sec": 0.0,
            "actual_hz": 0.0,
            "target_hz": target_hz,
            "min_hz": target_hz * HZ_MIN_RATIO,
            "median_gap_ms": 0.0,
            "p95_gap_ms": 0.0,
            "max_gap_ms": 0.0,
            "expected_count": 0,
            "dropped_estimate": 0,
            "valid": not rate_critical,
            "note": (
                "메시지 2개 미만 — Hz 측정 불가"
                if rate_critical
                else "low-rate 토픽 (event/latched)"
            ),
        }

    ordered = sorted(log_times_ns)
    duration_ns = ordered[-1] - ordered[0]
    duration_sec = duration_ns / 1_000_000_000.0 if duration_ns > 0 else 0.0
    actual_hz = (count - 1) / duration_sec if duration_sec > 0 else 0.0

    gaps_ms = [
        (ordered[i + 1] - ordered[i]) / 1_000_000.0
        for i in range(len(ordered) - 1)
    ]
    median_gap_ms = _percentile(gaps_ms, 50.0)
    p95_gap_ms = _percentile(gaps_ms, 95.0)
    max_gap_ms = max(gaps_ms) if gaps_ms else 0.0

    expected_count = (
        int(round(duration_sec * target_hz)) + 1
        if rate_critical and duration_sec > 0
        else 0
    )
    dropped_estimate = max(0, expected_count - count) if expected_count else 0

    if rate_critical:
        valid = actual_hz >= target_hz * HZ_MIN_RATIO
    else:
        valid = True

    return {
        "topic": topic,
        "rate_critical": rate_critical,
        "message_count": count,
        "duration_sec": round(duration_sec, 3),
        "actual_hz": round(actual_hz, 2),
        "target_hz": target_hz,
        "min_hz": round(target_hz * HZ_MIN_RATIO, 2),
        "median_gap_ms": round(median_gap_ms, 2),
        "p95_gap_ms": round(p95_gap_ms, 2),
        "max_gap_ms": round(max_gap_ms, 2),
        "expected_count": expected_count,
        "dropped_estimate": dropped_estimate,
        "valid": valid,
    }


def _camera_sync_stats(
    timestamps_by_topic: dict[str, list[int]],
    target_hz: float,
) -> dict[str, Any] | None:
    """Estimate inter-camera sync skew at the slowest tick rate.

    Returns ``None`` when fewer than two camera topics are present.
    """
    cam_topics = sorted(t for t in timestamps_by_topic if t.endswith("/image"))
    if len(cam_topics) < 2:
        return None

    primary = cam_topics[0]
    primary_ticks = sorted(timestamps_by_topic[primary])
    if not primary_ticks:
        return None
    others = {
        t: sorted(timestamps_by_topic[t])
        for t in cam_topics[1:]
        if timestamps_by_topic[t]
    }
    if not others:
        return None

    timegap_ns = int(1_000_000_000 / target_hz) if target_hz > 0 else 0
    tolerance_ns = timegap_ns // 2 if timegap_ns else 0

    skews_ms: list[float] = []
    out_of_tolerance = 0
    for tick in primary_ticks:
        worst = 0
        for other_ticks in others.values():
            # Closest other-camera tick by absolute time.
            idx = _bisect_closest(other_ticks, tick)
            if idx is None:
                continue
            diff = abs(other_ticks[idx] - tick)
            worst = max(worst, diff)
        if worst:
            skews_ms.append(worst / 1_000_000.0)
            if tolerance_ns and worst > tolerance_ns:
                out_of_tolerance += 1

    if not skews_ms:
        return None

    return {
        "primary": primary,
        "others": list(others.keys()),
        "tolerance_ms": round(tolerance_ns / 1_000_000.0, 2) if tolerance_ns else 0.0,
        "median_skew_ms": round(_percentile(skews_ms, 50.0), 2),
        "p95_skew_ms": round(_percentile(skews_ms, 95.0), 2),
        "max_skew_ms": round(max(skews_ms), 2),
        "out_of_tolerance_frames": out_of_tolerance,
        "total_frames": len(primary_ticks),
    }


def _bisect_closest(sorted_values: list[int], target: int) -> int | None:
    if not sorted_values:
        return None
    import bisect

    pos = bisect.bisect_left(sorted_values, target)
    candidates = []
    if pos < len(sorted_values):
        candidates.append(pos)
    if pos > 0:
        candidates.append(pos - 1)
    return min(candidates, key=lambda i: abs(sorted_values[i] - target))


def _episode_wall_duration_sec(episode_meta: dict | None) -> float | None:
    if not episode_meta:
        return None
    duration = episode_meta.get("duration_sec")
    if isinstance(duration, (int, float)) and duration > 0:
        return float(duration)
    return None


def compute_hz_report(
    bag_dir: Path | None,
    target_hz: float = DEFAULT_TARGET_HZ,
    episode_meta: dict | None = None,
) -> dict[str, Any] | None:
    """Build a per-topic Hz/timestamp quality report for one trial bag.

    Returns ``None`` when no MCAP file is found. The structure is JSON-
    serialisable and is consumed by the webapp's "수집 품질" surface.
    """
    if not bag_dir or not bag_dir.exists():
        return None
    mcap_files = sorted(bag_dir.glob("*.mcap"))
    if not mcap_files:
        return None
    primary_mcap = mcap_files[0]

    timestamps = _collect_log_times(primary_mcap)
    if not timestamps:
        return None

    topics: list[dict[str, Any]] = []
    for topic in sorted(timestamps.keys()):
        topics.append(_analyze_topic(topic, timestamps[topic], target_hz))

    rate_critical_stats = [t for t in topics if t["rate_critical"]]
    if rate_critical_stats:
        avg_hz = sum(t["actual_hz"] for t in rate_critical_stats) / len(
            rate_critical_stats
        )
        worst = min(rate_critical_stats, key=lambda t: t["actual_hz"])
        worst_topic = worst["topic"]
        worst_hz = worst["actual_hz"]
        all_pass = all(t["valid"] for t in rate_critical_stats)
        total_dropped = sum(t["dropped_estimate"] for t in rate_critical_stats)
        total_expected = sum(t["expected_count"] for t in rate_critical_stats)
        drop_rate = total_dropped / total_expected if total_expected else 0.0
    else:
        avg_hz = 0.0
        worst_topic = ""
        worst_hz = 0.0
        all_pass = True
        total_dropped = 0
        total_expected = 0
        drop_rate = 0.0

    bag_duration_sec = _bag_duration_sec(bag_dir)
    wall_duration_sec = _episode_wall_duration_sec(episode_meta)
    sim_wall_drift_ratio: float | None = None
    sim_wall_mismatch = False
    if (
        bag_duration_sec is not None
        and wall_duration_sec is not None
        and bag_duration_sec > 0
    ):
        sim_wall_drift_ratio = abs(wall_duration_sec - bag_duration_sec) / bag_duration_sec
        sim_wall_mismatch = sim_wall_drift_ratio >= SIM_WALL_MISMATCH_RATIO

    camera_sync = _camera_sync_stats(timestamps, target_hz)

    return {
        "schema_version": HZ_REPORT_SCHEMA_VERSION,
        "mcap_file": primary_mcap.name,
        "target_hz": target_hz,
        "min_ratio": HZ_MIN_RATIO,
        "topics": topics,
        "summary": {
            "avg_hz": round(avg_hz, 2),
            "worst_topic": worst_topic,
            "worst_hz": round(worst_hz, 2),
            "all_pass": bool(all_pass),
            "total_dropped_estimate": int(total_dropped),
            "total_expected": int(total_expected),
            "drop_rate": round(drop_rate, 4),
        },
        "duration_check": {
            "bag_duration_sec": bag_duration_sec,
            "episode_wall_duration_sec": wall_duration_sec,
            "drift_ratio": round(sim_wall_drift_ratio, 4)
            if sim_wall_drift_ratio is not None
            else None,
            "mismatch": sim_wall_mismatch,
            "threshold_ratio": SIM_WALL_MISMATCH_RATIO,
        },
        "camera_sync": camera_sync,
    }


def hz_report_warnings(report: dict[str, Any] | None, prefix: str) -> list[str]:
    """Render `report` into human-readable warning strings for validation.json."""
    if not report:
        return []
    warnings: list[str] = []

    summary = report.get("summary") or {}
    if not summary.get("all_pass", True):
        worst_topic = summary.get("worst_topic", "?")
        worst_hz = summary.get("worst_hz", 0.0)
        target = report.get("target_hz", DEFAULT_TARGET_HZ)
        min_ratio = report.get("min_ratio", HZ_MIN_RATIO)
        warnings.append(
            f"{prefix}: Hz 미달 — 최저 토픽 `{worst_topic}` "
            f"{worst_hz:.1f}Hz < {target * min_ratio:.1f}Hz"
        )

    drop_rate = float(summary.get("drop_rate", 0.0))
    if drop_rate >= 0.05 and summary.get("total_expected", 0) > 0:
        warnings.append(
            f"{prefix}: 프레임 드롭률 {drop_rate * 100:.1f}% "
            f"(예상 {summary.get('total_expected')} → 실측 "
            f"{summary.get('total_expected', 0) - summary.get('total_dropped_estimate', 0)})"
        )

    duration = report.get("duration_check") or {}
    if duration.get("mismatch"):
        bag_d = duration.get("bag_duration_sec")
        ep_d = duration.get("episode_wall_duration_sec")
        ratio = duration.get("drift_ratio")
        if bag_d is not None and ep_d is not None and ratio is not None:
            warnings.append(
                f"{prefix}: bag(sim) {bag_d:.1f}s vs episode(wall) {ep_d:.1f}s — "
                f"{ratio * 100:.1f}% 차이 (sim_time/wall_time 혼용 가능성)"
            )

    cam_sync = report.get("camera_sync")
    if cam_sync and cam_sync.get("out_of_tolerance_frames", 0) > 0:
        warnings.append(
            f"{prefix}: 카메라 동기 어긋남 — 프레임 "
            f"{cam_sync['out_of_tolerance_frames']}/{cam_sync['total_frames']} "
            f"가 허용오차 {cam_sync.get('tolerance_ms', 0):.1f}ms 초과 "
            f"(p95 {cam_sync.get('p95_skew_ms', 0):.1f}ms)"
        )

    return warnings


# ---------------------------------------------------------------------------
# tags.json 생성
# ---------------------------------------------------------------------------


def build_tags(
    trial_num: int,
    trial_scoring: dict,
    episode_meta: dict | None,
    policy: str,
    seed: int | None,
    parameters: dict[str, float] | None,
    *,
    bag_dir: Path | None = None,
    engine_config: Path | None = None,
) -> dict[str, Any]:
    """
    trial별 tags.json을 생성. 스키마는 추후 확장 가능.

    - success: tier_3 메시지가 "successful" 포함하면 True
    - cable/plug/port_type: episode metadata.json에서 복사, 없으면 engine config에서 추출
    - trial_duration_sec: episode → scoring tier_2 → bag duration 순으로 fallback
    - early_terminated: episode → bag insertion_event → scoring tier_3 순으로 fallback
    - policy, seed: 인자로 전달받음
    - parameters: 이 run에 주입된 파라미터 값 (dict)
    """
    tier_3 = trial_scoring.get("tier_3") or {}
    tier_3_msg = str(tier_3.get("message", ""))
    success_from_scoring = "successful" in tier_3_msg.lower()

    tags: dict[str, Any] = {
        "schema_version": TAGS_SCHEMA_VERSION,
        "trial": trial_num,
        "success": success_from_scoring,
        "scoring": {
            "total": trial_scoring.get("total"),
            "tier_3_message": tier_3_msg,
            "tier_3_score": tier_3.get("score"),
        },
        "policy": policy,
        "seed": seed,
    }

    if episode_meta:
        tags["cable_type"] = episode_meta.get("cable_type")
        tags["plug_type"] = episode_meta.get("plug_type")
        tags["port_type"] = episode_meta.get("port_type")
        tags["plug_port_distance"] = episode_meta.get("plug_port_distance")
        if "early_terminated" in episode_meta:
            tags["early_terminated"] = episode_meta["early_terminated"]
        if "early_term_source" in episode_meta:
            tags["early_term_source"] = episode_meta["early_term_source"]
        if "trial_duration_sec" in episode_meta:
            tags["trial_duration_sec"] = episode_meta["trial_duration_sec"]
    else:
        # Fallback: episode 없을 때 대안 소스에서 추출
        trial_key = f"trial_{trial_num}"

        # cable/plug/port_type ← engine config
        cfg_info = _config_task_info(engine_config, trial_key)
        for key in ("cable_type", "plug_type", "port_type"):
            if key in cfg_info:
                tags[key] = cfg_info[key]

        # trial_duration_sec ← scoring tier_2 → bag duration
        dur = _scoring_duration_sec(trial_scoring)
        if dur is None:
            dur = _bag_duration_sec(bag_dir)
        if dur is not None:
            tags["trial_duration_sec"] = dur

        # early_terminated ← scoring tier_3 기준 (bag insertion_event는 policy가
        # publish하므로 scoring 성공 여부와 무관하여 신뢰 불가)
        if success_from_scoring:
            tags["early_terminated"] = True
            tags["early_term_source"] = "insertion_event"
        else:
            tags["early_terminated"] = False

    if parameters:
        tags["parameters"] = parameters

    return tags


# ---------------------------------------------------------------------------
# 메인 재편 로직
# ---------------------------------------------------------------------------


def process_run(
    run_dir: Path,
    engine_results: Path,
    demo_dir: Path,
    engine_config: Path,
    policy: str,
    seed: int | None,
    parameters: dict[str, float] | None,
    flatten: bool = False,
) -> int:
    """단일 run의 산출물을 run_dir 아래에 재편한다.

    Args:
        flatten: True면 trial이 정확히 1개일 때 `trial_<N>_score<NNN>/` 래퍼를
            생략하고 파일들을 `run_dir` 바로 아래 배치한다. 큐 모드(1 config = 1 trial)
            에서 사용. 다중 trial이면 무시됨.

    Returns:
        0 on success, non-zero on error.
    """
    if not engine_results.exists():
        sys.stderr.write(f"[error] engine-results 없음: {engine_results}\n")
        return 1

    scoring_path = engine_results / "scoring.yaml"
    if not scoring_path.exists():
        sys.stderr.write(f"[error] scoring.yaml 없음: {scoring_path}\n")
        return 1

    with open(scoring_path) as f:
        scoring = yaml.safe_load(f)

    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. 메타 파일 복사
    if engine_config.exists():
        shutil.copy2(engine_config, run_dir / "config.yaml")
    (run_dir / "policy.txt").write_text(policy + "\n")
    if seed is not None:
        (run_dir / "seed.txt").write_text(str(seed) + "\n")
    shutil.copy2(scoring_path, run_dir / "scoring_run.yaml")

    # 엔진 config의 trial 실행 순서 (episode 매칭용)
    trial_order = load_trial_order(engine_config)
    if trial_order:
        print(f"[info] 엔진 trial 실행 순서: {trial_order}")
    storage_cfg = _bag_storage_config(engine_config)

    # 2. trial별 재편
    per_trial = split_scoring(scoring)
    if not per_trial:
        sys.stderr.write("[warn] scoring.yaml에 trial_* 키가 없음\n")
        return 0

    # flatten은 trial이 정확히 1개일 때만 적용 (다중 trial 시 래퍼 필요)
    use_flat = bool(flatten) and len(per_trial) == 1
    if flatten and not use_flat:
        sys.stderr.write(
            f"[warn] flatten=True 요청됐으나 trial이 {len(per_trial)}개 → "
            f"trial 래퍼 유지\n"
        )

    for trial_key, trial_scoring in per_trial.items():
        m = re.match(r"trial_(\d+)$", trial_key)
        if not m:
            sys.stderr.write(f"[warn] 비표준 trial 키 무시: {trial_key}\n")
            continue
        trial_num = int(m.group(1))
        score_int = trial_total_score(trial_scoring)

        if use_flat:
            trial_dir = run_dir
            # 평탄 모드: trial의 scoring은 scoring_run.yaml과 충돌 방지로 별도 이름
            trial_scoring_fn = "trial_scoring.yaml"
        else:
            trial_dir = run_dir / f"trial_{trial_num}_score{score_int}"
            trial_dir.mkdir(exist_ok=True)
            trial_scoring_fn = "scoring.yaml"

        # 2-a. trial scoring
        with open(trial_dir / trial_scoring_fn, "w") as f:
            yaml.safe_dump(
                {trial_key: trial_scoring},
                f,
                sort_keys=False,
                allow_unicode=True,
            )

        # 2-b. bag 이동 (있으면)
        bag = find_bag_for_trial(engine_results, trial_num)
        episode_meta: dict | None = None
        if bag:
            dst_bag = trial_dir / "bag"
            if dst_bag.exists():
                shutil.rmtree(dst_bag)
            shutil.move(str(bag), str(dst_bag))
            print(f"[ok] {trial_key}: bag → {dst_bag}")
            _compress_bag_storage(dst_bag, storage_cfg)
        else:
            print(f"[warn] {trial_key}: bag_trial_{trial_num}_* 없음 (엔진 bag 미기록?)")

        # 2-c. episode 이동 (순서 기반 매칭)
        episode = find_episode_by_order(demo_dir, trial_key, trial_order)
        if episode:
            dst_ep = trial_dir / "episode"
            if dst_ep.exists():
                shutil.rmtree(dst_ep)
            shutil.move(str(episode), str(dst_ep))
            # metadata.json의 잘못된 trial 필드를 실제 번호로 교정
            fix_episode_metadata_trial(dst_ep, trial_num)
            print(f"[ok] {trial_key}: episode → {dst_ep}")
            # episode metadata 로드 (tags용)
            meta_path = dst_ep / "metadata.json"
            if meta_path.exists():
                try:
                    with open(meta_path) as f:
                        episode_meta = json.load(f)
                except Exception:
                    pass
        else:
            if trial_order:
                idx = trial_order.index(trial_key) if trial_key in trial_order else -1
                print(
                    f"[warn] {trial_key}: 매칭되는 episode 없음 "
                    f"(demo_dir/episode_{idx:04d} 기대) demo_dir={demo_dir}"
                )
            else:
                print(f"[warn] {trial_key}: trial_order 비어있음 — engine_config 확인 필요")

        # 2-d. tags.json 생성
        dst_bag = trial_dir / "bag" if (trial_dir / "bag").exists() else None
        tags = build_tags(
            trial_num=trial_num,
            trial_scoring=trial_scoring,
            episode_meta=episode_meta,
            policy=policy,
            seed=seed,
            parameters=parameters,
            bag_dir=dst_bag,
            engine_config=engine_config,
        )

        # 2-e. Hz / timestamp 품질 리포트 (가능하면)
        hz_report = compute_hz_report(
            bag_dir=dst_bag,
            target_hz=DEFAULT_TARGET_HZ,
            episode_meta=episode_meta,
        )
        if hz_report is not None:
            with open(trial_dir / "hz_report.json", "w") as f:
                json.dump(hz_report, f, indent=2, ensure_ascii=False)
            tags["hz_summary"] = hz_report["summary"]
            tags["hz_duration_check"] = hz_report["duration_check"]
            print(
                f"[hz] {trial_key}: avg={hz_report['summary']['avg_hz']:.1f}Hz "
                f"worst={hz_report['summary']['worst_topic']} "
                f"({hz_report['summary']['worst_hz']:.1f}Hz) "
                f"drop={hz_report['summary']['drop_rate'] * 100:.1f}%"
            )

        with open(trial_dir / "tags.json", "w") as f:
            json.dump(tags, f, indent=2, ensure_ascii=False)

    print(f"[done] run 재편 완료: {run_dir}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_params_arg(arg: str | None) -> dict[str, float] | None:
    if not arg:
        return None
    out: dict[str, float] = {}
    for tok in arg.split(","):
        tok = tok.strip()
        if not tok:
            continue
        k, _, v = tok.partition("=")
        try:
            out[k.strip()] = float(v)
        except ValueError:
            sys.stderr.write(f"[warn] --parameters 파싱 실패: {tok}\n")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True, help="run 출력 디렉토리")
    parser.add_argument("--engine-results", type=Path, required=True, help="엔진 결과 디렉토리 (~/aic_results)")
    parser.add_argument("--demo-dir", type=Path, required=True, help="이 run의 episode 임시 디렉토리")
    parser.add_argument("--engine-config", type=Path, required=True, help="사용한 엔진 config 파일")
    parser.add_argument("--policy", required=True, help="policy 이름 (e.g. cheatcode)")
    parser.add_argument("--seed", type=int, default=None, help="샘플링 seed")
    parser.add_argument(
        "--parameters",
        default=None,
        help="이 run에 주입된 파라미터 'k=v,k=v' (tags.json에 기록)",
    )
    parser.add_argument(
        "--parameters-json",
        type=Path,
        default=None,
        help="파라미터 dict를 담은 JSON 파일 (--parameters와 배타)",
    )
    args = parser.parse_args()

    params: dict[str, float] | None = None
    if args.parameters and args.parameters_json:
        sys.stderr.write("[error] --parameters와 --parameters-json 동시 사용 불가\n")
        return 1
    if args.parameters:
        params = parse_params_arg(args.parameters)
    elif args.parameters_json:
        try:
            with open(args.parameters_json) as f:
                params = json.load(f)
            if not isinstance(params, dict):
                sys.stderr.write("[error] parameters-json은 dict 형식이어야 합니다\n")
                return 1
        except Exception as e:
            sys.stderr.write(f"[error] parameters-json 파싱 실패: {e}\n")
            return 1

    return process_run(
        run_dir=args.run_dir,
        engine_results=args.engine_results,
        demo_dir=args.demo_dir,
        engine_config=args.engine_config,
        policy=args.policy,
        seed=args.seed,
        parameters=params,
    )


if __name__ == "__main__":
    sys.exit(main())
