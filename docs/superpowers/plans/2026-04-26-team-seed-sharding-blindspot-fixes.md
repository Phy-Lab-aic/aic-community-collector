# Team Seed Sharding — Distribution Blindspot Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire P0–P3 fixes from the 2026-04-26 blindspot analysis into `aic-community-collector`: enable 10-target SFP cycling + SC collection, switch to LHS, refuse non-reproducible submits, reconcile ledger against simulator failures, reset stale M0 state.

**Architecture:** Most behavior already exists in `sample_training_configs` (LHS branch, `target_cycling`); fixes are additive — preset rewrite, two new helper functions in `team_preset.py` (`_validate_scene`, `_enforce_repro_gates`), one new public function (`reconcile_ledger_with_queue`), one module-level CLI with two subcommands (`reconcile`, `submit`), one shell script (`archive_legacy_queue.sh`). The webapp's SFP-only guard is relaxed so the new preset loads; the SC collection path goes through the new CLI in this round.

**Tech Stack:** Python 3.12, pytest, pyyaml, scipy (qmc), Streamlit, fcntl-based ledger lock.

**Pre-implementation note (scope clarification vs. spec):** The spec budgeted ~30 LoC for `webapp.py` covering only the gate-error banner. While drafting this plan we discovered `_require_sfp_only_team_mode_tasks` at `webapp.py:1231-1234` raises `PresetError` whenever `sc_default_count != 0`. Without relaxing it, the new `sc_default_count: 100` preset would refuse to load in team mode — the SFP path in the UI would be blocked too. Plan adds the relaxation (Task 2) and adds a CLI submit subcommand (Task 6) so SC samples can be produced this round; no SC UI work is included.

---

## File Map

| Path | Role |
|---|---|
| `src/aic_collector/team_preset.py` | Existing module — add `_validate_scene`, `_env_flag`, `_enforce_repro_gates`, `reconcile_ledger_with_queue`, `__main__` CLI. |
| `src/aic_collector/webapp.py` | Relax SFP-only guard at `_require_sfp_only_team_mode_tasks` to non-negative check. |
| `tests/test_team_preset.py` | Existing — add tests for new helpers/CLI; update SC-zero tests where they assumed the now-relaxed guard. |
| `tests/test_training_sampler.py` | Add 10-target cycling distribution + LHS marginal coverage tests. |
| `tests/test_webapp_team_mode_state.py` | Update SC=0 rejection tests to reflect relaxed guard. |
| `configs/team/preset.yaml` | Rewrite scene/strategy/tasks. |
| `configs/team/seed_ledger.yaml` | Reset to `entries: []`. |
| `scripts/archive_legacy_queue.sh` | New helper. |

---

## Task 1: Add `_validate_scene` and reject `target_cycling=false` without `fixed_target`

**Files:**
- Modify: `src/aic_collector/team_preset.py` (add helper, call from `load_preset`)
- Test: `tests/test_team_preset.py` (add three tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_team_preset.py`:

```python
def test_load_preset_rejects_target_cycling_false_without_fixed_target(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path / "preset.yaml",
        """
team: {base_seed: 1, shard_stride: 10, index_width: 4}
sampling:
  strategy: uniform
  ranges:
    nic_translation: [-0.01, 0.01]
scene:
  nic_count_range: [1, 1]
  sc_count_range:  [1, 1]
  target_cycling:  false
tasks: {sfp_default_count: 1, sc_default_count: 0}
members:
  - {id: M0, name: alice}
""".strip(),
    )
    with pytest.raises(PresetError, match="scene.target_cycling"):
        load_preset(path)


def test_load_preset_allows_target_cycling_false_when_fixed_target_present(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path / "preset.yaml",
        """
team: {base_seed: 1, shard_stride: 10, index_width: 4}
sampling:
  strategy: uniform
  ranges:
    nic_translation: [-0.01, 0.01]
scene:
  nic_count_range: [1, 1]
  sc_count_range:  [1, 1]
  target_cycling:  false
  fixed_target:
    sfp: {rail: 0, port: sfp_port_0}
    sc: null
tasks: {sfp_default_count: 1, sc_default_count: 0}
members:
  - {id: M0, name: alice}
""".strip(),
    )
    preset = load_preset(path)
    assert preset is not None


def test_load_preset_allows_target_cycling_true(tmp_path: Path) -> None:
    path = _write_preset(
        tmp_path / "preset.yaml",
        """
team: {base_seed: 1, shard_stride: 10, index_width: 4}
sampling:
  strategy: lhs
  ranges:
    nic_translation: [-0.01, 0.01]
scene:
  nic_count_range: [1, 1]
  sc_count_range:  [1, 1]
  target_cycling:  true
tasks: {sfp_default_count: 1, sc_default_count: 0}
members:
  - {id: M0, name: alice}
""".strip(),
    )
    preset = load_preset(path)
    assert preset is not None
```

- [ ] **Step 2: Run tests, confirm 1 of 3 fails**

```bash
cd /home/weed/ws_aic/src/aic-community-collector
uv run pytest tests/test_team_preset.py -k "target_cycling" -v
```

Expected: `test_load_preset_rejects_target_cycling_false_without_fixed_target` FAILS (no PresetError raised); the other two PASS (current code accepts both).

- [ ] **Step 3: Add `_validate_scene` and call it from `load_preset`**

Edit `src/aic_collector/team_preset.py`. Insert after `_validate_members` (around line 230):

```python
def _validate_scene(value: Any) -> dict[str, Any]:
    scene = _validate_mapping(value, "scene")
    fixed_target = scene.get("fixed_target")
    target_cycling = scene.get("target_cycling", True)
    if fixed_target in (None, {}) and target_cycling is False:
        raise PresetError(
            "scene.target_cycling must be true when scene.fixed_target is unset"
        )
    return scene
```

Replace the `scene = ...` assignment in `load_preset` (currently at line 598):

```python
    scene = _freeze(_validate_scene(_require_path(raw, "scene")))
```

- [ ] **Step 4: Run tests, confirm all pass**

```bash
uv run pytest tests/test_team_preset.py -k "target_cycling" -v
```

Expected: 3 PASS.

- [ ] **Step 5: Run the full team_preset suite to confirm no regression**

```bash
uv run pytest tests/test_team_preset.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/aic_collector/team_preset.py tests/test_team_preset.py
git commit -m "feat(team): validate scene.target_cycling against fixed_target"
```

---

## Task 2: Relax `_require_sfp_only_team_mode_tasks` to allow `sc_default_count > 0`

**Files:**
- Modify: `src/aic_collector/webapp.py:1231-1234`
- Test: `tests/test_webapp_team_mode_state.py` (update two existing tests)

- [ ] **Step 1: Update the two SC-rejection tests**

Locate these tests in `tests/test_webapp_team_mode_state.py`:

- `test_build_team_mode_state_rejects_nonzero_sc_default_count`
- `test_build_team_submit_preset_rejects_nonzero_sc_default_count`

Replace their bodies with positive assertions:

```python
def test_build_team_mode_state_accepts_nonzero_sc_default_count(tmp_path: Path) -> None:
    preset, queue_root, ledger_path, member_id = _make_preset_with_sc_default_count(
        tmp_path, sc_default_count=100
    )
    state = build_team_mode_state(
        preset,
        queue_root=queue_root,
        ledger_path=ledger_path,
        member_id=member_id,
    )
    # Default SFP path remains usable; SC count is reflected in the preset only.
    assert state["default_sfp_count"] >= 0
    assert state["selected_sfp_count"] >= 0


def test_build_team_submit_preset_accepts_nonzero_sc_default_count() -> None:
    preset = _make_simple_preset(sc_default_count=100)
    submit_preset = build_team_submit_preset(preset, sfp_count=5)
    assert submit_preset.tasks["sfp"] == 5
    assert submit_preset.tasks["sc_default_count"] == 100
```

If `_make_preset_with_sc_default_count` / `_make_simple_preset` aren't already in the module, copy the existing `test_build_team_mode_state_rejects_nonzero_sc_default_count` fixture inline and parametrize it on `sc_default_count`. Use `inspect`/grep to confirm — they likely exist as ad-hoc helpers per test.

- [ ] **Step 2: Run tests, confirm they fail under current code**

```bash
uv run pytest tests/test_webapp_team_mode_state.py -k "nonzero_sc_default_count" -v
```

Expected: both FAIL (current `_require_sfp_only_team_mode_tasks` still raises `PresetError`).

- [ ] **Step 3: Relax the guard in `webapp.py`**

Replace `webapp.py:1231-1235`:

```python
def _require_sfp_only_team_mode_tasks(preset: TeamPreset) -> int:
    default_sfp_count = _preset_task_count(preset, "sfp_default_count")
    if _preset_task_count(preset, "sc_default_count") != 0:
        raise PresetError("Unsupported team preset task count: tasks.sc_default_count must be 0")
    return default_sfp_count
```

with:

```python
def _require_sfp_only_team_mode_tasks(preset: TeamPreset) -> int:
    default_sfp_count = _preset_task_count(preset, "sfp_default_count")
    if default_sfp_count < 0:
        raise PresetError("Invalid team preset task count: tasks.sfp_default_count must be non-negative")
    sc_default_count = _preset_task_count(preset, "sc_default_count")
    if sc_default_count < 0:
        raise PresetError("Invalid team preset task count: tasks.sc_default_count must be non-negative")
    return default_sfp_count
```

The function name stays for git history continuity; internally it now accepts SC counts and only the SFP path is exposed in the UI this round (SC submission goes through the CLI added in Task 6).

- [ ] **Step 4: Run tests, confirm they pass**

```bash
uv run pytest tests/test_webapp_team_mode_state.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aic_collector/webapp.py tests/test_webapp_team_mode_state.py
git commit -m "refactor(webapp): relax team-mode SFP-only guard to non-negative check"
```

---

## Task 3: Add `_env_flag` and `_enforce_repro_gates` helpers (pure)

**Files:**
- Modify: `src/aic_collector/team_preset.py` (add two helpers; not yet wired into submit)
- Test: `tests/test_team_preset.py` (add gate tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_team_preset.py`:

```python
from aic_collector.team_preset import (  # noqa: E402  add to existing import block
    _enforce_repro_gates,
    _env_flag,
)


def test_env_flag_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("1", "true", "TRUE", "yes", "Yes"):
        monkeypatch.setenv("AIC_TEST_FLAG", value)
        assert _env_flag("AIC_TEST_FLAG") is True


def test_env_flag_falsy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("", "0", "false", "no", "off"):
        monkeypatch.setenv("AIC_TEST_FLAG", value)
        assert _env_flag("AIC_TEST_FLAG") is False
    monkeypatch.delenv("AIC_TEST_FLAG", raising=False)
    assert _env_flag("AIC_TEST_FLAG") is False


def test_enforce_repro_gates_rejects_dirty_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIC_ALLOW_DIRTY", raising=False)
    with pytest.raises(PresetError, match="dirty tree"):
        _enforce_repro_gates(
            entries=[],
            task_type="sfp",
            preset_hash="sha256:abc",
            git_sha="dirty:abcdef",
        )


def test_enforce_repro_gates_allows_dirty_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIC_ALLOW_DIRTY", "1")
    _enforce_repro_gates(
        entries=[],
        task_type="sfp",
        preset_hash="sha256:abc",
        git_sha="dirty:abcdef",
    )


def test_enforce_repro_gates_passes_clean_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIC_ALLOW_DIRTY", raising=False)
    _enforce_repro_gates(
        entries=[],
        task_type="sfp",
        preset_hash="sha256:abc",
        git_sha="abcdef",
    )


def test_enforce_repro_gates_allows_uncommitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIC_ALLOW_DIRTY", raising=False)
    _enforce_repro_gates(
        entries=[],
        task_type="sfp",
        preset_hash="sha256:abc",
        git_sha="uncommitted",
    )


def test_enforce_repro_gates_rejects_preset_hash_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIC_ALLOW_PRESET_DRIFT", raising=False)
    entries = [{"task_type": "sfp", "preset_hash": "sha256:old"}]
    with pytest.raises(PresetError, match="preset_hash drift"):
        _enforce_repro_gates(
            entries=entries,
            task_type="sfp",
            preset_hash="sha256:new",
            git_sha="abcdef",
        )


def test_enforce_repro_gates_allows_drift_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIC_ALLOW_PRESET_DRIFT", "1")
    entries = [{"task_type": "sfp", "preset_hash": "sha256:old"}]
    _enforce_repro_gates(
        entries=entries,
        task_type="sfp",
        preset_hash="sha256:new",
        git_sha="abcdef",
    )


def test_enforce_repro_gates_drift_isolated_per_task_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIC_ALLOW_PRESET_DRIFT", raising=False)
    entries = [{"task_type": "sfp", "preset_hash": "sha256:sfphash"}]
    # SC submission with different hash should pass — drift is checked per task_type.
    _enforce_repro_gates(
        entries=entries,
        task_type="sc",
        preset_hash="sha256:schash",
        git_sha="abcdef",
    )


def test_enforce_repro_gates_first_submit_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIC_ALLOW_PRESET_DRIFT", raising=False)
    _enforce_repro_gates(
        entries=[],
        task_type="sfp",
        preset_hash="sha256:any",
        git_sha="abcdef",
    )
```

- [ ] **Step 2: Run tests, confirm they fail with import errors**

```bash
uv run pytest tests/test_team_preset.py -k "env_flag or enforce_repro_gates" -v
```

Expected: ImportError (functions not defined).

- [ ] **Step 3: Add the helpers**

Edit `src/aic_collector/team_preset.py`. Insert after `_iso_utc_now` (around line 98):

```python
def _env_flag(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in {"1", "true", "yes"}


def _enforce_repro_gates(
    entries: list[dict[str, Any]],
    *,
    task_type: str,
    preset_hash: str,
    git_sha: str,
) -> None:
    """Refuse non-reproducible submits.

    `dirty:<sha>` => raises unless AIC_ALLOW_DIRTY env flag is truthy.
    `uncommitted` (no git checkout) is permitted — the 2026-04-20 spec
    explicitly supports it for wheel installs.

    preset_hash drift vs. the most recent same-task_type entry => raises
    unless AIC_ALLOW_PRESET_DRIFT env flag is truthy. First-ever submit
    has no prior entry to compare against, so it passes.
    """
    if git_sha.startswith("dirty:") and not _env_flag("AIC_ALLOW_DIRTY"):
        raise PresetError(
            "Refusing to submit on a dirty tree. Commit or stash, "
            "or set AIC_ALLOW_DIRTY=1 to override."
        )
    same_task = [e for e in entries if e.get("task_type") == task_type]
    if same_task:
        prior_hash = same_task[-1].get("preset_hash")
        if prior_hash != preset_hash and not _env_flag("AIC_ALLOW_PRESET_DRIFT"):
            raise PresetError(
                f"preset_hash drift vs latest {task_type} entry "
                f"({prior_hash!r} -> {preset_hash!r}). "
                "Set AIC_ALLOW_PRESET_DRIFT=1 to override."
            )
```

- [ ] **Step 4: Run tests, confirm all pass**

```bash
uv run pytest tests/test_team_preset.py -k "env_flag or enforce_repro_gates" -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aic_collector/team_preset.py tests/test_team_preset.py
git commit -m "feat(team): add reproducibility gate helpers"
```

---

## Task 4: Wire `_enforce_repro_gates` into `submit_team_claim`

**Files:**
- Modify: `src/aic_collector/team_preset.py` (`submit_team_claim`)
- Test: `tests/test_team_preset.py` (add integration tests)

- Note: the `_make_submit_fixture` template was widened in Task 6 to include `task_board_limits: {}` and `robot: {}` (required by `scene_builder.load_fixed_sections` for the full submit path). The fixture block below reflects that wider template content.

- [ ] **Step 1: Write failing integration test**

Append to `tests/test_team_preset.py` (uses helpers already present in the file):

```python
def test_submit_team_claim_blocks_on_dirty_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aic_collector import team_preset as tp_mod

    monkeypatch.delenv("AIC_ALLOW_DIRTY", raising=False)
    monkeypatch.setattr(tp_mod, "_git_sha", lambda: "dirty:cafef00d")

    preset, queue_root, ledger_path, template_path = _make_submit_fixture(tmp_path)

    with pytest.raises(PresetError, match="dirty tree"):
        tp_mod.submit_team_claim(
            preset,
            member_id="M0",
            task_type="sfp",
            queue_root=queue_root,
            ledger_path=ledger_path,
            template_path=template_path,
        )
    # Ledger must be untouched.
    assert not ledger_path.exists() or _load_ledger(ledger_path) == {"entries": []}


def test_submit_team_claim_blocks_on_preset_hash_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aic_collector import team_preset as tp_mod

    monkeypatch.delenv("AIC_ALLOW_PRESET_DRIFT", raising=False)
    monkeypatch.setattr(tp_mod, "_git_sha", lambda: "cleanabc")

    preset, queue_root, ledger_path, template_path = _make_submit_fixture(tmp_path)

    # Seed an entry with a different preset_hash.
    seeded = [
        {
            "member_id": "M0",
            "task_type": "sfp",
            "base_seed": preset.base_seed,
            "start_index": 0,
            "count": 0,
            "strategy": preset.strategy,
            "queue_root": str(queue_root),
            "preset_hash": "sha256:olddifferent",
            "git_sha": "cleanabc",
            "created_at": "2026-04-20T00:00:00Z",
        }
    ]
    yaml.safe_dump({"entries": seeded}, ledger_path.open("w"), sort_keys=False)

    with pytest.raises(PresetError, match="preset_hash drift"):
        tp_mod.submit_team_claim(
            preset,
            member_id="M1",
            task_type="sfp",
            queue_root=queue_root,
            ledger_path=ledger_path,
            template_path=template_path,
        )
    # Only the seeded entry survives.
    assert len(_load_ledger(ledger_path)["entries"]) == 1
```

If `_make_submit_fixture` doesn't already exist in this test file, add the helper near the top (after the existing `_load_ledger` helper):

```python
def _make_submit_fixture(tmp_path: Path) -> tuple[TeamPreset, Path, Path, Path]:
    """Build a minimal valid preset + queue/ledger/template paths."""
    preset_path = _write_preset(
        tmp_path / "preset.yaml",
        """
team: {base_seed: 42, shard_stride: 1000, index_width: 6}
sampling:
  strategy: lhs
  ranges:
    nic_translation: [-0.0215, 0.0234]
    nic_yaw:         [-0.1745, 0.1745]
    sc_translation:  [-0.06, 0.055]
    gripper_xy:      0.002
    gripper_z:       0.002
    gripper_rpy:     0.04
scene:
  nic_count_range: [1, 1]
  sc_count_range:  [1, 1]
  target_cycling:  true
tasks: {sfp_default_count: 10, sc_default_count: 0, sfp: 10}
members:
  - {id: M0, name: alice}
  - {id: M1, name: bob}
""".strip(),
    )
    preset = load_preset(preset_path)
    assert preset is not None

    queue_root = tmp_path / "train"
    queue_root.mkdir()
    ledger_path = tmp_path / "ledger.yaml"
    template_path = tmp_path / "template.yaml"
    template_path.write_text("scoring:\n  topics: []\ntask_board_limits: {}\nrobot: {}\n", encoding="utf-8")
    return preset, queue_root, ledger_path, template_path
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
uv run pytest tests/test_team_preset.py -k "submit_team_claim_blocks" -v
```

Expected: FAIL — current `submit_team_claim` doesn't call `_enforce_repro_gates`.

- [ ] **Step 3: Wire the gate into `submit_team_claim`**

Edit `src/aic_collector/team_preset.py`. In `submit_team_claim`, immediately after the `start_index + requested_count > slot_end_exclusive` check (around line 528), insert:

```python
        _enforce_repro_gates(
            entries,
            task_type=task_type,
            preset_hash=preset.preset_hash,
            git_sha=_git_sha(),
        )
```

The call lives **inside** `_ledger_lock(ledger_path)` (the existing `with` block) so the comparison is race-free.

- [ ] **Step 4: Run tests, confirm they pass**

```bash
uv run pytest tests/test_team_preset.py -k "submit_team_claim" -v
```

Expected: all PASS (including any pre-existing happy-path tests).

- [ ] **Step 5: Run full project tests for regression check**

```bash
uv run pytest tests/ -v 2>&1 | tail -30
```

Expected: 0 failures. Some pre-existing happy-path submit tests may need their fixtures touched up to set a clean `_git_sha` if they relied on the unchecked old behavior — fix any failing ones in this same step.

- [ ] **Step 6: Commit**

```bash
git add src/aic_collector/team_preset.py tests/test_team_preset.py
git commit -m "feat(team): enforce reproducibility gates on submit"
```

---

## Task 5: Add `reconcile_ledger_with_queue`

**Files:**
- Modify: `src/aic_collector/team_preset.py` (add public function)
- Test: `tests/test_team_preset.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_team_preset.py`:

```python
from aic_collector.team_preset import reconcile_ledger_with_queue  # noqa: E402


def _seed_ledger(ledger_path: Path, entries: list[dict[str, object]]) -> None:
    yaml.safe_dump({"entries": entries}, ledger_path.open("w"), sort_keys=False)


def _touch_failed(queue_root: Path, task_type: str, indices: list[int], width: int = 6) -> None:
    failed = queue_dir(queue_root, task_type, QueueState.FAILED)
    failed.mkdir(parents=True, exist_ok=True)
    for idx in indices:
        (failed / f"config_{task_type}_{idx:0{width}d}.yaml").write_text("{}", encoding="utf-8")


def test_reconcile_records_failed_indices_within_window(tmp_path: Path) -> None:
    queue_root = tmp_path / "train"
    ledger_path = tmp_path / "ledger.yaml"
    _seed_ledger(ledger_path, [
        {"member_id": "M0", "task_type": "sfp", "start_index": 0, "count": 100,
         "base_seed": 42, "strategy": "lhs", "queue_root": str(queue_root),
         "preset_hash": "sha256:x", "git_sha": "abc", "created_at": "2026-04-26T00:00:00Z"},
    ])
    _touch_failed(queue_root, "sfp", [3, 17, 99, 200])  # 200 is out of window

    updated = reconcile_ledger_with_queue(ledger_path, queue_root)

    entry = updated[0]
    assert entry["failed_indices"] == [3, 17, 99]
    assert entry["validated_count"] == 100 - 3
    assert "reconciled_at" in entry


def test_reconcile_is_idempotent(tmp_path: Path) -> None:
    queue_root = tmp_path / "train"
    ledger_path = tmp_path / "ledger.yaml"
    _seed_ledger(ledger_path, [
        {"member_id": "M0", "task_type": "sfp", "start_index": 0, "count": 50,
         "base_seed": 42, "strategy": "lhs", "queue_root": str(queue_root),
         "preset_hash": "sha256:x", "git_sha": "abc", "created_at": "2026-04-26T00:00:00Z"},
    ])
    _touch_failed(queue_root, "sfp", [4, 8])

    first = reconcile_ledger_with_queue(ledger_path, queue_root)
    second = reconcile_ledger_with_queue(ledger_path, queue_root)

    assert first[0]["failed_indices"] == second[0]["failed_indices"] == [4, 8]
    assert first[0]["validated_count"] == second[0]["validated_count"] == 48


def test_reconcile_handles_missing_failed_dir(tmp_path: Path) -> None:
    queue_root = tmp_path / "train"  # not created
    ledger_path = tmp_path / "ledger.yaml"
    _seed_ledger(ledger_path, [
        {"member_id": "M0", "task_type": "sfp", "start_index": 0, "count": 5,
         "base_seed": 42, "strategy": "lhs", "queue_root": str(queue_root),
         "preset_hash": "sha256:x", "git_sha": "abc", "created_at": "2026-04-26T00:00:00Z"},
    ])

    updated = reconcile_ledger_with_queue(ledger_path, queue_root)

    assert updated[0]["failed_indices"] == []
    assert updated[0]["validated_count"] == 5


def test_reconcile_two_entries_share_failed_dir(tmp_path: Path) -> None:
    queue_root = tmp_path / "train"
    ledger_path = tmp_path / "ledger.yaml"
    _seed_ledger(ledger_path, [
        {"member_id": "M0", "task_type": "sfp", "start_index": 0, "count": 100,
         "base_seed": 42, "strategy": "lhs", "queue_root": str(queue_root),
         "preset_hash": "sha256:x", "git_sha": "abc", "created_at": "2026-04-26T00:00:00Z"},
        {"member_id": "M1", "task_type": "sfp", "start_index": 100000, "count": 100,
         "base_seed": 42, "strategy": "lhs", "queue_root": str(queue_root),
         "preset_hash": "sha256:x", "git_sha": "abc", "created_at": "2026-04-26T00:00:00Z"},
    ])
    _touch_failed(queue_root, "sfp", [10, 100005])

    updated = reconcile_ledger_with_queue(ledger_path, queue_root)

    assert updated[0]["failed_indices"] == [10]
    assert updated[1]["failed_indices"] == [100005]
```

- [ ] **Step 2: Run tests, confirm import error**

```bash
uv run pytest tests/test_team_preset.py -k "reconcile" -v
```

Expected: ImportError (function not defined).

- [ ] **Step 3: Implement `reconcile_ledger_with_queue`**

Edit `src/aic_collector/team_preset.py`. Insert near the other public functions (after `adjust_claim_count`, around line 352):

```python
def reconcile_ledger_with_queue(
    ledger_path: Path,
    queue_root: Path,
) -> list[dict[str, Any]]:
    """Annotate every ledger entry with simulator-stage failure data.

    Scans `<queue_root>/<task_type>/failed/config_<task>_NNNNNN.yaml` and
    sets, for each entry whose `[start_index, start_index + count)` window
    contains failed indices:
      - failed_indices:  sorted list of in-window failed sample indices
      - validated_count: count - len(failed_indices)
      - reconciled_at:   ISO-UTC timestamp of this reconciliation pass

    Idempotent: re-running with the same `failed/` contents produces the
    same payload modulo `reconciled_at`. Missing or absent `failed/`
    directories are treated as zero failures.
    """
    with _ledger_lock(ledger_path):
        entries = _ledger_entries(ledger_path)

        failed_by_task: dict[str, list[int]] = {}
        for entry in entries:
            task_type = entry.get("task_type")
            if not isinstance(task_type, str) or task_type in failed_by_task:
                continue
            failed_dir = queue_dir(queue_root, task_type, QueueState.FAILED)
            indices: list[int] = []
            if failed_dir.exists():
                pattern = re.compile(
                    _CONFIG_INDEX_RE_TEMPLATE.format(task_type=re.escape(task_type))
                )
                for path in failed_dir.iterdir():
                    match = pattern.fullmatch(path.name)
                    if match is not None:
                        indices.append(int(match.group(1)))
            failed_by_task[task_type] = sorted(indices)

        now = _iso_utc_now()
        for entry in entries:
            task_type = entry.get("task_type")
            start_index = entry.get("start_index")
            count = entry.get("count")
            if (
                not isinstance(task_type, str)
                or isinstance(start_index, bool)
                or isinstance(count, bool)
                or not isinstance(start_index, int)
                or not isinstance(count, int)
            ):
                continue
            window_end = start_index + count
            in_window = [
                idx
                for idx in failed_by_task.get(task_type, [])
                if start_index <= idx < window_end
            ]
            entry["failed_indices"] = in_window
            entry["validated_count"] = max(0, count - len(in_window))
            entry["reconciled_at"] = now

        _write_ledger_entries(ledger_path, entries)
        return entries
```

- [ ] **Step 4: Run tests, confirm pass**

```bash
uv run pytest tests/test_team_preset.py -k "reconcile" -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aic_collector/team_preset.py tests/test_team_preset.py
git commit -m "feat(team): add reconcile_ledger_with_queue for failure feedback"
```

---

## Task 6: Add `__main__` CLI with `reconcile` and `submit` subcommands

**Files:**
- Modify: `src/aic_collector/team_preset.py` (add CLI block at bottom)
- Test: `tests/test_team_preset.py`

- [ ] **Step 1: Write failing CLI smoke tests**

Append to `tests/test_team_preset.py`:

```python
import subprocess


def test_cli_reconcile_smoke(tmp_path: Path) -> None:
    queue_root = tmp_path / "train"
    ledger_path = tmp_path / "ledger.yaml"
    _seed_ledger(ledger_path, [
        {"member_id": "M0", "task_type": "sfp", "start_index": 0, "count": 5,
         "base_seed": 42, "strategy": "lhs", "queue_root": str(queue_root),
         "preset_hash": "sha256:x", "git_sha": "abc", "created_at": "2026-04-26T00:00:00Z"},
    ])
    _touch_failed(queue_root, "sfp", [2])

    result = subprocess.run(
        [
            sys.executable, "-m", "aic_collector.team_preset", "reconcile",
            "--ledger", str(ledger_path),
            "--queue-root", str(queue_root),
        ],
        capture_output=True, text=True, check=True,
        cwd=PROJECT_DIR,
        env={**os.environ, "PYTHONPATH": str(PROJECT_DIR / "src")},
    )
    assert "M0" in result.stdout
    assert "1 failed" in result.stdout
    payload = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    assert payload["entries"][0]["failed_indices"] == [2]


def test_cli_submit_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preset, queue_root, ledger_path, template_path = _make_submit_fixture(tmp_path)
    # Avoid dirty-tree check polluting the smoke test.
    env = {**os.environ, "PYTHONPATH": str(PROJECT_DIR / "src"), "AIC_ALLOW_DIRTY": "1"}

    result = subprocess.run(
        [
            sys.executable, "-m", "aic_collector.team_preset", "submit",
            "--preset", str(tmp_path / "preset.yaml"),
            "--ledger", str(ledger_path),
            "--queue-root", str(queue_root),
            "--template", str(template_path),
            "--member", "M0",
            "--task-type", "sfp",
        ],
        capture_output=True, text=True, check=True,
        cwd=PROJECT_DIR,
        env=env,
    )
    assert "start_index" in result.stdout
    payload = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    assert payload["entries"][0]["member_id"] == "M0"
```

The `import os` line should already be present at the top of the test file via the `subprocess.run` env construction; if not, add `import os` to the imports block.

- [ ] **Step 2: Run tests, confirm fail**

```bash
uv run pytest tests/test_team_preset.py -k "cli_" -v
```

Expected: FAIL — `python -m aic_collector.team_preset` exits non-zero (no `__main__`).

- [ ] **Step 3: Implement the CLI**

Append to `src/aic_collector/team_preset.py`:

```python
def _cli_reconcile(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger)
    queue_root = Path(args.queue_root)
    entries = reconcile_ledger_with_queue(ledger_path, queue_root)
    for entry in entries:
        member_id = entry.get("member_id")
        task_type = entry.get("task_type")
        count = entry.get("count")
        failed = entry.get("failed_indices") or []
        validated = entry.get("validated_count")
        print(
            f"{member_id}/{task_type}: {count} written, "
            f"{len(failed)} failed, {validated} validated"
        )
    return 0


def _cli_submit(args: argparse.Namespace) -> int:
    preset = load_preset(Path(args.preset))
    if preset is None:
        print(f"[error] preset missing: {args.preset}", file=sys.stderr)
        return 2
    result = submit_team_claim(
        preset,
        member_id=args.member,
        task_type=args.task_type,
        queue_root=Path(args.queue_root),
        ledger_path=Path(args.ledger),
        template_path=Path(args.template),
    )
    print(
        f"submitted: member={args.member} task={args.task_type} "
        f"start_index={result.start_index} written={result.written_count}"
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aic_collector.team_preset")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("reconcile", help="Annotate ledger with sim-stage failures")
    rec.add_argument("--ledger", required=True)
    rec.add_argument("--queue-root", required=True)
    rec.set_defaults(func=_cli_reconcile)

    sub_submit = sub.add_parser("submit", help="Append a claim and write configs")
    sub_submit.add_argument("--preset", required=True)
    sub_submit.add_argument("--ledger", required=True)
    sub_submit.add_argument("--queue-root", required=True)
    sub_submit.add_argument("--template", required=True)
    sub_submit.add_argument("--member", required=True)
    sub_submit.add_argument("--task-type", required=True, choices=("sfp", "sc"))
    sub_submit.set_defaults(func=_cli_submit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
```

Add `import argparse` and `import sys` to the existing imports at the top of the module. They are not currently imported.

- [ ] **Step 4: Run tests, confirm pass**

```bash
uv run pytest tests/test_team_preset.py -k "cli_" -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/aic_collector/team_preset.py tests/test_team_preset.py
git commit -m "feat(team): add CLI for reconcile and submit"
```

---

## Task 7: Sampler distribution tests for `target_cycling=true` and LHS

**Files:**
- Modify: `tests/test_training_sampler.py`

- [ ] **Step 1: Add new tests**

Append to `tests/test_training_sampler.py` (no impl change needed — sampler already supports both modes):

```python
def test_target_cycling_distributes_uniformly_across_10_targets() -> None:
    """1000 SFP samples with target_cycling=True hit every (rail, port) 100 times."""
    cfg = {
        "scene": {
            "nic_count_range": [1, 1],
            "sc_count_range":  [1, 1],
            "target_cycling":  True,
        },
        "ranges": {},
        "param_strategy": "lhs",
    }
    samples = sample_training_configs(cfg, "sfp", count=1000, seed=42, strategy="lhs")
    counts: dict[tuple[int, str], int] = {}
    for s in samples:
        key = (s.target_rail, s.target_port_name)
        counts[key] = counts.get(key, 0) + 1
    assert len(counts) == 10
    assert all(v == 100 for v in counts.values()), counts


def test_lhs_pose_marginal_has_no_empty_bin() -> None:
    """LHS guarantees every dimension's 10-bin histogram is non-empty for n>=10."""
    cfg = {
        "scene": {
            "nic_count_range": [1, 1],
            "sc_count_range":  [1, 1],
            "target_cycling":  True,
        },
        "ranges": {
            "nic_translation": [-0.0215, 0.0234],
            "nic_yaw":         [-0.1745, 0.1745],
            "sc_translation":  [-0.06, 0.055],
            "gripper_xy": 0.002,
            "gripper_z":  0.002,
            "gripper_rpy": 0.04,
        },
        "param_strategy": "lhs",
    }
    samples = sample_training_configs(cfg, "sfp", count=100, seed=42, strategy="lhs")
    # Pull nic translation from the active rail of each sample.
    nic_values = [next(iter(s.nic_poses.values()))["translation"] for s in samples]
    bins = [0] * 10
    lo, hi = -0.0215, 0.0234
    for v in nic_values:
        # clamp to [lo, hi) defensively, then bucket into 10 bins.
        idx = min(9, max(0, int((v - lo) / (hi - lo) * 10)))
        bins[idx] += 1
    assert all(b > 0 for b in bins), bins


def test_fixed_target_legacy_path_unchanged_regression() -> None:
    """Sanity: fixed_target still collapses to a single (rail, port)."""
    cfg = {
        "scene": {
            "nic_count_range": [1, 1],
            "sc_count_range":  [1, 1],
            "target_cycling":  False,
        },
        "ranges": {},
        "collection": {"fixed_target": {"sfp": {"rail": 2, "port": "sfp_port_1"}}},
        "param_strategy": "uniform",
    }
    samples = sample_training_configs(cfg, "sfp", count=20, seed=42, strategy="uniform")
    assert all(s.target_rail == 2 and s.target_port_name == "sfp_port_1" for s in samples)
```

- [ ] **Step 2: Run tests**

```bash
uv run pytest tests/test_training_sampler.py -k "target_cycling_distributes or lhs_pose or fixed_target_legacy" -v
```

Expected: all PASS (existing sampler already supports both modes).

If any test fails, fix the test rather than touching the sampler — these are characterization tests, not new behavior.

- [ ] **Step 3: Commit**

```bash
git add tests/test_training_sampler.py
git commit -m "test(sampler): cover 10-target cycling and LHS marginal distribution"
```

---

## Task 8: Rewrite `configs/team/preset.yaml` and reset `configs/team/seed_ledger.yaml`

**Files:**
- Modify: `configs/team/preset.yaml`
- Modify: `configs/team/seed_ledger.yaml`

- [ ] **Step 1: Rewrite preset**

Replace `configs/team/preset.yaml` with:

```yaml
# Team-wide preset for blindspot-fix collection round (2026-04-26).
# Edits are changes to the team contract - open a PR.

version: 1
team:
  base_seed: 42
  shard_stride: 100000
  index_width: 6
sampling:
  strategy: lhs
  ranges:
    nic_translation: [-0.0215, 0.0234]
    nic_yaw: [-0.1745, 0.1745]
    sc_translation: [-0.06, 0.055]
    gripper_xy: 0.002
    gripper_z: 0.002
    gripper_rpy: 0.04
scene:
  nic_count_range: [1, 1]
  sc_count_range: [1, 1]
  target_cycling: true
tasks:
  sfp_default_count: 1000
  sc_default_count: 100
members:
  - id: M0
    name: alice
  - id: M1
    name: bob
  - id: M2
    name: carol
  - id: M3
    name: dave
  - id: M4
    name: eve
  - id: M5
    name: frank
```

Key diff: `strategy: uniform → lhs`; removed `fixed_target` block; `target_cycling: false → true`; `sc_default_count: 0 → 100`.

- [ ] **Step 2: Reset ledger**

Replace `configs/team/seed_ledger.yaml` with:

```yaml
entries: []
```

- [ ] **Step 3: Run the loader on the new preset to confirm it parses**

```bash
uv run python -c "
from pathlib import Path
from aic_collector.team_preset import load_preset
preset = load_preset(Path('configs/team/preset.yaml'))
assert preset is not None
assert preset.strategy == 'lhs'
assert preset.tasks['sc_default_count'] == 100
assert preset.scene['target_cycling'] is True
assert 'fixed_target' not in preset.scene
print('preset OK:', preset.preset_hash)
"
```

Expected: prints `preset OK: sha256:...`.

- [ ] **Step 4: Run the full test suite**

```bash
uv run pytest tests/ 2>&1 | tail -20
```

Expected: 0 failures.

- [ ] **Step 5: Commit**

```bash
git add configs/team/preset.yaml configs/team/seed_ledger.yaml
git commit -m "config(team): rewrite preset for 10-target LHS round, reset ledger"
```

---

## Task 9: Add `scripts/archive_legacy_queue.sh`

**Files:**
- Create: `scripts/archive_legacy_queue.sh`

- [ ] **Step 1: Write the script**

Create `scripts/archive_legacy_queue.sh`:

```bash
#!/usr/bin/env bash
# Move stale queue files into an archive directory before starting the
# 2026-04-26 blindspot-fix collection round.
#
# Targets:
#   - 4-digit filenames (config_<task>_NNNN.yaml)  — pre-2026-04-20 layout
#   - 6-digit filenames with index in [0, 100000)  — M0 slot from the
#     2026-04-20 round, which used the old fixed_target preset
#
# Idempotent: missing dirs are skipped; the archive dir is appended to.

set -euo pipefail

ROOT="${1:-configs/train}"
ARCHIVE="${ROOT}/_archive_2026-04-26"
mkdir -p "$ARCHIVE"

moved=0
for task in sfp sc; do
  for state in pending running done failed legacy; do
    src="$ROOT/$task/$state"
    [ -d "$src" ] || continue
    dst="$ARCHIVE/$task/$state"
    mkdir -p "$dst"
    while IFS= read -r -d '' f; do
      name="$(basename "$f")"
      # extract NNNN or NNNNNN
      idx_part="${name#config_${task}_}"
      idx_part="${idx_part%.yaml}"
      [[ "$idx_part" =~ ^[0-9]+$ ]] || continue
      width="${#idx_part}"
      idx=$((10#$idx_part))
      if [ "$width" -eq 4 ]; then
        mv -- "$f" "$dst/$name"
        moved=$((moved + 1))
      elif [ "$width" -ge 6 ] && [ "$idx" -ge 0 ] && [ "$idx" -lt 100000 ]; then
        mv -- "$f" "$dst/$name"
        moved=$((moved + 1))
      fi
    done < <(find "$src" -maxdepth 1 -type f -name "config_${task}_*.yaml" -print0)
  done
done

echo "archived $moved file(s) to $ARCHIVE"
```

- [ ] **Step 2: Make it executable and dry-run with a tmp dir**

```bash
chmod +x scripts/archive_legacy_queue.sh

TMP=$(mktemp -d)
mkdir -p "$TMP/sfp/pending" "$TMP/sfp/done" "$TMP/sc/failed"
touch "$TMP/sfp/pending/config_sfp_0001.yaml"          # 4-digit -> archive
touch "$TMP/sfp/pending/config_sfp_000020.yaml"        # 6-digit M0 slot -> archive
touch "$TMP/sfp/pending/config_sfp_100000.yaml"        # 6-digit M1 slot -> KEEP
touch "$TMP/sc/failed/config_sc_0007.yaml"             # 4-digit -> archive
scripts/archive_legacy_queue.sh "$TMP"
ls "$TMP/sfp/pending"
ls "$TMP/_archive_2026-04-26/sfp/pending"
ls "$TMP/_archive_2026-04-26/sc/failed"
rm -rf "$TMP"
```

Expected output:
- `$TMP/sfp/pending` lists `config_sfp_100000.yaml` only
- `_archive_2026-04-26/sfp/pending` contains `config_sfp_0001.yaml` and `config_sfp_000020.yaml`
- `_archive_2026-04-26/sc/failed` contains `config_sc_0007.yaml`
- script reports `archived 3 file(s)`

- [ ] **Step 3: Idempotency check**

```bash
TMP=$(mktemp -d)
mkdir -p "$TMP/sfp/pending"
touch "$TMP/sfp/pending/config_sfp_0001.yaml"
scripts/archive_legacy_queue.sh "$TMP"  # archives 1 file
scripts/archive_legacy_queue.sh "$TMP"  # archives 0 files (no source files left)
rm -rf "$TMP"
```

Expected: second run prints `archived 0 file(s)`.

- [ ] **Step 4: Commit**

```bash
git add scripts/archive_legacy_queue.sh
git commit -m "feat(scripts): archive_legacy_queue moves stale 4-digit and M0-slot configs"
```

---

## Task 10: Update spec progress note + final test run

**Files:**
- Modify: `docs/superpowers/specs/2026-04-26-team-seed-sharding-blindspot-fixes-design.md` (append a "Status" line at the bottom; one line, two minutes)

- [ ] **Step 1: Append status note**

Append to the bottom of the spec:

```markdown

---
**Status (2026-04-26):** Implemented in commits on `subagent/team-seed-sharding`. Rollout step 2 (`scripts/archive_legacy_queue.sh`) and step 4 (post-collection reconcile) remain manual operator actions.
```

- [ ] **Step 2: Run the entire test suite from a clean import path**

```bash
uv run pytest tests/ -v 2>&1 | tail -40
```

Expected: 0 failures.

- [ ] **Step 3: Verify CLI help works end-to-end**

```bash
uv run python -m aic_collector.team_preset --help
uv run python -m aic_collector.team_preset reconcile --help
uv run python -m aic_collector.team_preset submit --help
```

Expected: each prints argparse help text.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-04-26-team-seed-sharding-blindspot-fixes-design.md
git commit -m "docs(team): mark 2026-04-26 blindspot-fix spec as implemented"
```

---

## Self-Review Checklist (post-write)

Spec section coverage:
- §C1 preset rewrite → Task 8 ✓
- §C2 scene validation → Task 1 ✓
- §C3 reproducibility gates → Tasks 3 + 4 ✓
- §C4 reconcile + CLI → Tasks 5 + 6 ✓
- §C5 webapp gate banner → handled passively (existing `_team_submit` already converts `PresetError` to `st.error`); no new code ✓
- §C6 stale state cleanup → Tasks 8 (ledger reset) + 9 (archive script) ✓
- Webapp SFP-only guard relaxation (gap discovered while planning) → Task 2 ✓

Sampler coverage:
- target_cycling=true distribution test → Task 7 ✓
- LHS marginal coverage test → Task 7 ✓
- fixed_target regression → Task 7 ✓

Type/name consistency:
- `_enforce_repro_gates` signature stable across Tasks 3, 4
- `reconcile_ledger_with_queue` signature stable across Tasks 5, 6
- CLI subcommand names (`reconcile`, `submit`) stable across Task 6
- `AIC_ALLOW_DIRTY` / `AIC_ALLOW_PRESET_DRIFT` env names consistent across Tasks 3, 4, 6

No "TBD" / "TODO" / "implement later" markers in any task body. Every step shows the actual code.
