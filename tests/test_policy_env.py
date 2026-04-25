from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from aic_collector.prefect.policy_env import (  # noqa: E402
    build_policy_env,
    build_policy_pythonpath,
    deploy_policies,
    normalize_policy_name,
    resolve_inner_class,
)
from aic_collector.webapp import discover_policies  # noqa: E402


def test_resolve_inner_class_normalizes_autocode_case(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "AutoCode.py").write_text("", encoding="utf-8")

    assert normalize_policy_name("autocode", policy_dirs=(source_dir,)) == "AutoCode"
    assert (
        resolve_inner_class("autocode", policy_dirs=(source_dir,))
        == "aic_example_policies.ros.AutoCode"
    )


def test_build_policy_env_normalizes_per_trial_policy_names(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    local_dir = tmp_path / "local"
    source_dir.mkdir()
    local_dir.mkdir()
    (source_dir / "AutoCode.py").write_text("", encoding="utf-8")
    (local_dir / "VisionCheatCode.py").write_text("", encoding="utf-8")

    env = build_policy_env(
        "autocode",
        per_trial={2: "visioncheatcode"},
        policy_dirs=(source_dir, local_dir),
    )

    assert env["AIC_INNER_POLICY"] == "aic_example_policies.ros.AutoCode"
    assert env["AIC_INNER_POLICY_TRIAL_2"] == "aic_example_policies.ros.VisionCheatCode"


def test_build_policy_pythonpath_prepends_source_package_root(tmp_path: Path) -> None:
    source_root = tmp_path / "aic_example_policies"
    source_root.mkdir()

    pythonpath = build_policy_pythonpath(
        existing_pythonpath="/tmp/existing:/tmp/extra",
        source_package_root=source_root,
    )

    assert pythonpath == f"{source_root}:/tmp/existing:/tmp/extra"


def test_discover_policies_includes_aic_source_tree(monkeypatch, tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    pixi_dir = tmp_path / "pixi"
    local_dir = tmp_path / "local"
    for directory in (source_dir, pixi_dir, local_dir):
        directory.mkdir()
    (source_dir / "AutoCode.py").write_text("", encoding="utf-8")
    (pixi_dir / "RunACT.py").write_text("", encoding="utf-8")
    (local_dir / "VisionCheatCode.py").write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "aic_collector.webapp.policy_search_dirs",
        lambda _project_dir=None: (source_dir, pixi_dir, local_dir),
    )

    policies = discover_policies()

    assert "AutoCode" in policies
    assert "VisionCheatCode" in policies


def test_deploy_policies_syncs_newer_aic_source_policy(monkeypatch, tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    pixi_dir = tmp_path / "pixi"
    (project_dir / "policies").mkdir(parents=True)
    source_dir.mkdir()
    pixi_dir.mkdir()

    source_file = source_dir / "AutoCode.py"
    pixi_file = pixi_dir / "AutoCode.py"
    source_file.write_text("source-version\n", encoding="utf-8")
    pixi_file.write_text("pixi-version\n", encoding="utf-8")
    source_file.touch()
    pixi_file.touch()

    monkeypatch.setattr("aic_collector.prefect.policy_env.AIC_SOURCE_POLICIES_DIR", source_dir)
    monkeypatch.setattr("aic_collector.prefect.policy_env.PIXI_POLICIES_DIR", pixi_dir)

    deploy_policies(project_dir)

    assert pixi_file.read_text(encoding="utf-8") == "source-version\n"


def test_deploy_policies_keeps_local_policy_override(monkeypatch, tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    pixi_dir = tmp_path / "pixi"
    local_dir = project_dir / "policies"
    local_dir.mkdir(parents=True)
    source_dir.mkdir()
    pixi_dir.mkdir()

    (source_dir / "AutoCode.py").write_text("source-version\n", encoding="utf-8")
    (local_dir / "AutoCode.py").write_text("local-version\n", encoding="utf-8")

    monkeypatch.setattr("aic_collector.prefect.policy_env.AIC_SOURCE_POLICIES_DIR", source_dir)
    monkeypatch.setattr("aic_collector.prefect.policy_env.PIXI_POLICIES_DIR", pixi_dir)

    deploy_policies(project_dir)

    assert (pixi_dir / "AutoCode.py").read_text(encoding="utf-8") == "local-version\n"
