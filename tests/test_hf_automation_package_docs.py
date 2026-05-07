from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _normalized_requirement_names(requirements: list[str]) -> set[str]:
    names: set[str] = set()
    for requirement in requirements:
        name = re.split(r"[<>=!~;\[\s]", requirement, maxsplit=1)[0]
        names.add(name.replace("_", "-").lower())
    return names


def test_pyproject_exposes_batch_cli_and_huggingface_dependency() -> None:
    pyproject = _pyproject()

    assert "huggingface-hub" in _normalized_requirement_names(pyproject["project"]["dependencies"])
    assert (
        pyproject["project"]["scripts"]["aic-automation-batch"]
        == "aic_collector.automation.batch_runner:main"
    )


def test_hf_batch_runbook_documents_auth_submodule_recovery_and_cleanup_safety() -> None:
    runbook_path = ROOT / "docs" / "hf-batch-automation-runbook.md"

    assert runbook_path.exists()
    runbook = runbook_path.read_text(encoding="utf-8")

    required_fragments = [
        "HF_TOKEN",
        "huggingface-cli login",
        "붙여넣지",
        "UI",
        "third_party/rosbag-to-lerobot",
        "git submodule update --init --recursive third_party/rosbag-to-lerobot",
        "remote_verified",
        "cleanup_eligible",
        "cleanup_done",
        "uploaded",
        "재개",
    ]
    for fragment in required_fragments:
        assert fragment in runbook


def test_hf_batch_runbook_forbids_ui_token_persistence_and_unverified_cleanup() -> None:
    runbook = (ROOT / "docs" / "hf-batch-automation-runbook.md").read_text(encoding="utf-8").lower()

    assert "붙여넣지" in runbook and "token" in runbook and "ui" in runbook
    assert "저장하면 안 됩니다" in runbook and "token" in runbook
    assert "remote_verified` 전에" in runbook and "삭제하면 안 됩니다" in runbook
    assert "remote_verified" in runbook and "있을 때만" in runbook and "cleanup" in runbook
