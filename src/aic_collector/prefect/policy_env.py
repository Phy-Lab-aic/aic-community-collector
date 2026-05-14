from __future__ import annotations

import shutil
from pathlib import Path

_INNER_CLASS_MAP = {
    "cheatcode": "aic_example_policies.ros.CheatCodeInner",
    "hybrid": "aic_example_policies.ros.RunACTHybrid",
    "act": "aic_example_policies.ros.RunACTv1",
    "openpi": "aic_example_policies.ros.RunOpenPI",
}

POLICY_CLASS = "aic_example_policies.ros.CollectDispatchWrapper"


def resolve_inner_class(name: str) -> str:
    return _INNER_CLASS_MAP.get(name, f"aic_example_policies.ros.{name}")


def build_policy_env(
    policy_default: str,
    per_trial: dict[int, str] | None = None,
    act_model_path: str | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {
        "POLICY_CLASS": POLICY_CLASS,
        "AIC_INNER_POLICY": resolve_inner_class(policy_default),
    }
    if per_trial:
        for trial, name in per_trial.items():
            env[f"AIC_INNER_POLICY_TRIAL_{trial}"] = resolve_inner_class(name)
    if act_model_path is not None:
        env["ACT_MODEL_PATH"] = act_model_path
    return env


def deploy_policies(project_dir: str | Path) -> int:
    src = Path(project_dir) / "policies"
    dst = (
        Path.home()
        / "ws_aic/src/aic/.pixi/envs/default/lib/python3.12/site-packages/aic_example_policies/ros"
    )

    if not src.is_dir():
        raise FileNotFoundError(f"소스 디렉터리 없음: {src}")
    if not dst.is_dir():
        raise FileNotFoundError(f"대상 디렉터리 없음: {dst}")

    count = 0
    for f in sorted(src.glob("*.py")):
        shutil.copy2(f, dst / f.name)
        print(f"[OK] {f.name} → 배포 완료")
        count += 1

    # OpenPI 추론에 필요한 assets/ 폴더 (norm_stats.json 등) 복사
    src_assets = src / "assets"
    if src_assets.is_dir():
        dst_assets = dst / "assets"
        if dst_assets.exists():
            shutil.rmtree(dst_assets)
        shutil.copytree(src_assets, dst_assets)
        print(f"[OK] assets/ → 배포 완료")

    aic_autocode = (
        Path.home()
        / "ws_aic/src/aic/aic_example_policies/aic_example_policies/ros/AutoCode.py"
    )
    if aic_autocode.is_file():
        shutil.copy2(aic_autocode, dst / "AutoCode.py")
        print(f"[OK] {aic_autocode} → 배포 완료")
        count += 1

    print(f"=== {count}개 Policy 배포 완료 ===")
    return count
