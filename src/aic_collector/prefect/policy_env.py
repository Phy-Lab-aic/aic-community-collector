from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from pathlib import Path

_INNER_CLASS_MAP = {
    "cheatcode": "aic_example_policies.ros.CheatCodeInner",
    "hybrid": "aic_example_policies.ros.RunACTHybrid",
    "act": "aic_example_policies.ros.RunACTv1",
}

POLICY_CLASS = "aic_example_policies.ros.CollectDispatchWrapper"
PROJECT_DIR = Path(__file__).resolve().parents[3]
LOCAL_POLICIES_DIR = PROJECT_DIR / "policies"
AIC_REPO_DIR = Path.home() / "ws_aic/src/aic"
AIC_SOURCE_POLICY_PACKAGE_ROOT = AIC_REPO_DIR / "aic_example_policies"
AIC_SOURCE_POLICIES_DIR = AIC_SOURCE_POLICY_PACKAGE_ROOT / "aic_example_policies/ros"
PIXI_POLICIES_DIR = (
    AIC_REPO_DIR
    / ".pixi/envs/default/lib/python3.12/site-packages/aic_example_policies/ros"
)


def policy_search_dirs(project_dir: str | Path | None = None) -> tuple[Path, ...]:
    local_dir = Path(project_dir).expanduser().resolve() / "policies" if project_dir else LOCAL_POLICIES_DIR
    return (
        AIC_SOURCE_POLICIES_DIR,
        PIXI_POLICIES_DIR,
        local_dir,
    )


def normalize_policy_name(
    name: str,
    policy_dirs: Iterable[Path] | None = None,
) -> str:
    if name in _INNER_CLASS_MAP:
        return name

    needle = name.casefold()
    for directory in policy_dirs or policy_search_dirs():
        if not directory.exists():
            continue
        for policy_file in sorted(directory.glob("*.py")):
            if policy_file.stem.casefold() == needle:
                return policy_file.stem
    return name


def resolve_inner_class(
    name: str,
    policy_dirs: Iterable[Path] | None = None,
) -> str:
    normalized = normalize_policy_name(name, policy_dirs=policy_dirs)
    return _INNER_CLASS_MAP.get(normalized, f"aic_example_policies.ros.{normalized}")


def build_policy_pythonpath(
    existing_pythonpath: str | None = None,
    *,
    source_package_root: Path | None = None,
) -> str | None:
    entries: list[str] = []
    root = source_package_root or AIC_SOURCE_POLICY_PACKAGE_ROOT
    if root.is_dir():
        entries.append(str(root))
    if existing_pythonpath:
        entries.extend(part for part in existing_pythonpath.split(os.pathsep) if part)

    deduped: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry in seen:
            continue
        seen.add(entry)
        deduped.append(entry)
    return os.pathsep.join(deduped) or None


def build_policy_env(
    policy_default: str,
    per_trial: dict[int, str] | None = None,
    act_model_path: str | None = None,
    policy_dirs: Iterable[Path] | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {
        "POLICY_CLASS": POLICY_CLASS,
        "AIC_INNER_POLICY": resolve_inner_class(policy_default, policy_dirs=policy_dirs),
    }
    if per_trial:
        for trial, name in per_trial.items():
            env[f"AIC_INNER_POLICY_TRIAL_{trial}"] = resolve_inner_class(
                name,
                policy_dirs=policy_dirs,
            )
    if act_model_path is not None:
        env["ACT_MODEL_PATH"] = act_model_path
    return env


def deploy_policies(project_dir: str | Path) -> int:
    src = Path(project_dir) / "policies"
    dst = PIXI_POLICIES_DIR

    if not dst.is_dir():
        raise FileNotFoundError(f"대상 디렉터리 없음: {dst}")

    count = 0
    if AIC_SOURCE_POLICIES_DIR.is_dir():
        for f in sorted(AIC_SOURCE_POLICIES_DIR.glob("*.py")):
            shutil.copy2(f, dst / f.name)
            print(f"[OK] {f.name} → AIC source 동기화")
            count += 1

    if src.is_dir():
        for f in sorted(src.glob("*.py")):
            shutil.copy2(f, dst / f.name)
            print(f"[OK] {f.name} → 로컬 policy 배포 완료")
            count += 1

    print(f"=== {count}개 Policy 배포 완료 ===")
    return count
