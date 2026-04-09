#!/usr/bin/env python3
"""
Build engine config by filtering trials and injecting parameter values.

템플릿(community_random_config.yaml)에서 원하는 trial만 남기고,
파라미터 플레이스홀더(__NIC0_TRANSLATION__ 등)를 실제 값으로 치환한다.

Usage:
    # 1) trial 2만 + 파라미터 중간값으로 채우기 (smoke test용)
    python build_engine_config.py \\
        --template configs/community_random_config.yaml \\
        --trials 2 \\
        --out /tmp/engine_config_trial2.yaml

    # 2) trial 1,2,3 + 명시한 파라미터 (CLI)
    python build_engine_config.py \\
        --template configs/community_random_config.yaml \\
        --trials 1,2,3 \\
        --params nic0_translation=0.01,nic0_yaw=0.0,... \\
        --out /tmp/engine_config.yaml

    # 3) sampler.py의 JSON 출력 사용 (i번째 샘플)
    python sampler.py --config configs/e2e_default.yaml > /tmp/samples.json
    python build_engine_config.py \\
        --template configs/community_random_config.yaml \\
        --trials 1,2,3 \\
        --params-json /tmp/samples.json \\
        --params-index 0 \\
        --out /tmp/engine_config_run0.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("pyyaml not installed. pip install pyyaml\n")
    sys.exit(1)


# 파라미터 플레이스홀더와 템플릿 키 매핑
# 키: CLI/config 상의 이름 → 값: 템플릿 내 플레이스홀더 문자열
PARAM_PLACEHOLDERS = {
    "nic0_translation": "__NIC0_TRANSLATION__",
    "nic0_yaw": "__NIC0_YAW__",
    "nic1_translation": "__NIC1_TRANSLATION__",
    "nic1_yaw": "__NIC1_YAW__",
    "sc0_translation": "__SC0_TRANSLATION__",
    "sc0_yaw": "__SC0_YAW__",
    "sc1_translation": "__SC1_TRANSLATION__",
    "sc1_yaw": "__SC1_YAW__",
}

# 파라미터 범위 (community_random_config.yaml의 task_board_limits에서 유래)
PARAM_RANGES = {
    "nic0_translation": (-0.0215, 0.0234),
    "nic0_yaw": (-0.1745, 0.1745),
    "nic1_translation": (-0.0215, 0.0234),
    "nic1_yaw": (-0.1745, 0.1745),
    "sc0_translation": (-0.06, 0.055),
    "sc0_yaw": (-0.1745, 0.1745),
    "sc1_translation": (-0.06, 0.055),
    "sc1_yaw": (-0.1745, 0.1745),
}


def midpoints() -> dict[str, float]:
    """각 파라미터의 중간값 반환 (smoke test용)."""
    return {k: round((lo + hi) / 2.0, 4) for k, (lo, hi) in PARAM_RANGES.items()}


def parse_params_arg(arg: str | None) -> dict[str, float]:
    """CLI --params 'k1=v1,k2=v2' 형식을 dict로 파싱."""
    if not arg:
        return {}
    out: dict[str, float] = {}
    for token in arg.split(","):
        token = token.strip()
        if not token:
            continue
        k, _, v = token.partition("=")
        k = k.strip()
        if k not in PARAM_PLACEHOLDERS:
            sys.stderr.write(f"[warn] 알 수 없는 파라미터: {k}\n")
            continue
        out[k] = float(v)
    return out


def filter_trials(template_text: str, trial_ids: list[str]) -> dict:
    """
    YAML 텍스트를 로드하고 원하는 trial만 남긴 dict 반환.

    주의: yaml.safe_load는 플레이스홀더 문자열(__NIC0_TRANSLATION__)을
    그대로 문자열로 로드한다. 이후 dump 시점에도 문자열로 나간다.
    엔진은 숫자를 기대하므로, dump 후 텍스트 치환으로 실제 값을 넣는다.
    """
    cfg = yaml.safe_load(template_text)
    if not cfg or "trials" not in cfg:
        raise ValueError("템플릿에 'trials' 키가 없습니다")

    all_trials = cfg["trials"]
    kept = {}
    for tid in trial_ids:
        key = tid if tid in all_trials else f"trial_{tid}"
        if key not in all_trials:
            raise ValueError(
                f"trial '{tid}' (혹은 '{key}') 가 템플릿에 없습니다. "
                f"사용 가능: {list(all_trials.keys())}"
            )
        kept[key] = all_trials[key]

    if not kept:
        raise ValueError("선택된 trial이 없습니다")

    cfg["trials"] = kept
    return cfg


def inject_params(cfg_text: str, params: dict[str, float]) -> str:
    """플레이스홀더를 실제 숫자 문자열로 치환."""
    out = cfg_text
    for key, placeholder in PARAM_PLACEHOLDERS.items():
        if key not in params:
            continue
        # YAML 안에서 숫자로 해석되도록 따옴표 없이 삽입
        # placeholder는 `translation: __NIC0_TRANSLATION__` 형태로 존재
        out = out.replace(placeholder, f"{params[key]:.4f}")
    # 남아있는 플레이스홀더 검사
    remaining = [p for p in PARAM_PLACEHOLDERS.values() if p in out]
    if remaining:
        sys.stderr.write(
            f"[warn] 치환되지 않은 플레이스홀더 (해당 trial에선 사용되지 않을 수 있음): {remaining}\n"
        )
    return out


def build(template_path: Path, trial_ids: list[str], params: dict[str, float]) -> str:
    template_text = template_path.read_text()
    # trial 필터링은 dict 레벨로
    cfg = filter_trials(template_text, trial_ids)
    # 다시 YAML 텍스트로 덤프 (플레이스홀더는 문자열로 유지됨)
    cfg_text = yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)
    # 플레이스홀더 치환
    cfg_text = inject_params(cfg_text, params)
    return cfg_text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template",
        type=Path,
        default=Path("configs/community_random_config.yaml"),
        help="엔진 config 템플릿 경로",
    )
    parser.add_argument(
        "--trials",
        required=True,
        help="포함할 trial ID 쉼표 구분 (예: '2' 또는 '1,2,3' 또는 'trial_2')",
    )
    parser.add_argument(
        "--params",
        default=None,
        help="파라미터 값 지정 'k=v,k=v' (--params-json과 배타)",
    )
    parser.add_argument(
        "--params-json",
        type=Path,
        default=None,
        help="sampler.py의 JSON 출력 파일 경로 (list of dict). --params-index로 선택",
    )
    parser.add_argument(
        "--params-index",
        type=int,
        default=0,
        help="--params-json 리스트 안에서 선택할 인덱스 (기본 0)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="출력 config 경로",
    )
    args = parser.parse_args()

    if not args.template.exists():
        sys.stderr.write(f"[error] 템플릿 없음: {args.template}\n")
        return 1

    if args.params and args.params_json:
        sys.stderr.write("[error] --params와 --params-json은 동시 사용 불가\n")
        return 1

    trial_ids = [t.strip() for t in args.trials.split(",") if t.strip()]

    # 파라미터 소스 결정: 중간값 < CLI < JSON 파일
    params = midpoints()
    if args.params:
        params.update(parse_params_arg(args.params))
    elif args.params_json:
        if not args.params_json.exists():
            sys.stderr.write(f"[error] params-json 없음: {args.params_json}\n")
            return 1
        try:
            with open(args.params_json) as f:
                samples = json.load(f)
        except Exception as e:
            sys.stderr.write(f"[error] params-json 파싱 실패: {e}\n")
            return 1
        if not isinstance(samples, list):
            sys.stderr.write("[error] params-json은 list of dict 형식이어야 합니다\n")
            return 1
        if args.params_index < 0 or args.params_index >= len(samples):
            sys.stderr.write(
                f"[error] --params-index={args.params_index}가 범위 밖 "
                f"(리스트 길이 {len(samples)})\n"
            )
            return 1
        sample = samples[args.params_index]
        if not isinstance(sample, dict):
            sys.stderr.write(
                f"[error] params-json[{args.params_index}]이 dict가 아님\n"
            )
            return 1
        # 알려진 키만 필터링하여 업데이트
        for k in list(sample.keys()):
            if k in PARAM_PLACEHOLDERS:
                params[k] = float(sample[k])
            else:
                sys.stderr.write(f"[warn] 알 수 없는 파라미터 키 무시: {k}\n")

    try:
        cfg_text = build(args.template, trial_ids, params)
    except Exception as e:
        sys.stderr.write(f"[error] {e}\n")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(cfg_text)
    print(f"[ok] wrote {args.out} (trials={trial_ids}, params={params})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
