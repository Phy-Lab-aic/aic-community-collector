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
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    sys.stderr.write("pyyaml 필요: pip install pyyaml\n")
    sys.exit(1)


TAGS_SCHEMA_VERSION = "0.1.0"


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
) -> int:
    """단일 run의 산출물을 run_dir 아래에 재편한다.

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

    # 2. trial별 재편
    per_trial = split_scoring(scoring)
    if not per_trial:
        sys.stderr.write("[warn] scoring.yaml에 trial_* 키가 없음\n")
        return 0

    for trial_key, trial_scoring in per_trial.items():
        m = re.match(r"trial_(\d+)$", trial_key)
        if not m:
            sys.stderr.write(f"[warn] 비표준 trial 키 무시: {trial_key}\n")
            continue
        trial_num = int(m.group(1))
        score_int = trial_total_score(trial_scoring)
        trial_dir = run_dir / f"trial_{trial_num}_score{score_int}"
        trial_dir.mkdir(exist_ok=True)

        # 2-a. trial scoring
        with open(trial_dir / "scoring.yaml", "w") as f:
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
