# Team Campaign Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add selectable team campaign presets for `trial_1`, `trial_2`, and `trial_3`, with a `1000`-sample campaign goal, `100`-sample default batches, SC support for `trial_3`, and preset-hash-scoped progress tracking.

**Architecture:** Keep the existing single-file team preset flow as a legacy fallback, but add a new catalog mode under `configs/team/presets/`. `team_preset.py` becomes the schema/ledger boundary for both legacy and catalog presets, while `webapp.py` adds preset selection, task-type-aware inputs, and campaign progress UI. Ledger remains append-only in one shared file, with campaign progress grouped by `preset_hash`.

**Tech Stack:** Python 3.12, Streamlit, PyYAML, pytest, `fcntl`, existing queue/ledger helpers in `aic_collector.team_preset`.

---

## Spec

Reference spec: `docs/superpowers/specs/2026-04-20-team-campaign-presets-design.md`

## File Structure

**Create:**
- `configs/team/presets/trial_1.yaml` — catalog preset for `trial_1` SFP campaign
- `configs/team/presets/trial_2.yaml` — catalog preset for `trial_2` SFP campaign
- `configs/team/presets/trial_3.yaml` — catalog preset for `trial_3` SC campaign
- `docs/superpowers/plans/2026-04-20-team-campaign-presets.md` — this implementation plan

**Modify:**
- `src/aic_collector/team_preset.py` — catalog preset loading, campaign fields, ledger progress helpers, submit-time campaign capacity enforcement
- `src/aic_collector/webapp.py` — preset catalog selection, task-type-aware team mode, campaign summaries, submit path
- `tests/test_team_preset.py` — loader, ledger, and campaign submit coverage
- `tests/test_webapp_team_mode_state.py` — state helpers, caption helpers, clamp behavior, SC team mode
- `tests/test_webapp_team_mode.py` — catalog-mode submit and preset-selection integration coverage
- `README.md` — team mode overview for preset catalog + batching
- `docs/usage-guide.md` — operator workflow for selecting `trial_1/2/3` and batching to `1000`
- `docs/config-reference.md` — new campaign preset schema

**Keep unchanged:**
- `configs/team/preset.yaml` — legacy single-file preset still works when no catalog presets exist
- `configs/team/seed_ledger.yaml` — shared ledger location remains unchanged

The plan deliberately keeps `team_preset.py` free of Streamlit imports and puts all UI branching in `webapp.py`.

---

## Task 1: Add catalog preset loader coverage before changing production code

**Files:**
- Modify: `tests/test_team_preset.py`
- Modify later: `src/aic_collector/team_preset.py`

- [ ] **Step 1: Write failing tests for catalog preset loading**

Append these tests to `tests/test_team_preset.py`:

```python
def test_load_presets_reads_sorted_catalog_and_campaign_fields(tmp_path: Path) -> None:
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    (preset_dir / "trial_2.yaml").write_text(
        """
version: 1
campaign:
  trial_id: trial_2
  task_type: sfp
  total_target_count: 1000
  batch_default_count: 100
team:
  base_seed: 42
  shard_stride: 100000
  index_width: 6
sampling:
  strategy: uniform
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
  target_cycling: false
  fixed_target:
    sfp: {rail: 1, port: "sfp_port_0"}
    sc: null
members:
  - {id: M0, name: alice}
""".strip(),
        encoding="utf-8",
    )
    (preset_dir / "trial_1.yaml").write_text(
        (preset_dir / "trial_2.yaml").read_text(encoding="utf-8").replace("trial_2", "trial_1").replace("rail: 1", "rail: 0"),
        encoding="utf-8",
    )

    presets, issues = load_presets(preset_dir)

    assert [preset.preset_name for preset in presets] == ["trial_1", "trial_2"]
    assert issues == ()
    assert presets[0].trial_id == "trial_1"
    assert presets[0].task_type == "sfp"
    assert presets[0].total_target_count == 1000
    assert presets[0].batch_default_count == 100
    assert presets[0].is_catalog_preset is True


def test_load_presets_reports_invalid_catalog_files(tmp_path: Path) -> None:
    preset_dir = tmp_path / "presets"
    preset_dir.mkdir()
    (preset_dir / "bad.yaml").write_text(
        """
version: 1
campaign:
  trial_id: trial_3
  task_type: sc
  total_target_count: 100
  batch_default_count: 101
team:
  base_seed: 42
  shard_stride: 100000
  index_width: 6
sampling:
  strategy: uniform
  ranges: {}
scene:
  nic_count_range: [1, 1]
  sc_count_range: [1, 1]
  target_cycling: false
  fixed_target:
    sfp: null
    sc: {rail: 1, port: "sc_port_1"}
members:
  - {id: M0, name: alice}
""".strip(),
        encoding="utf-8",
    )

    presets, issues = load_presets(preset_dir)

    assert presets == ()
    assert len(issues) == 1
    assert issues[0].path.name == "bad.yaml"
    assert "batch_default_count" in issues[0].message


def test_load_preset_legacy_schema_still_works(tmp_path: Path) -> None:
    path = tmp_path / "preset.yaml"
    path.write_text(
        """
team:
  base_seed: 42
  shard_stride: 100000
  index_width: 6
sampling:
  strategy: uniform
  ranges:
    nic_translation: [-0.0215, 0.0234]
scene:
  nic_count_range: [1, 1]
tasks:
  sfp_default_count: 1000
  sc_default_count: 0
members:
  - {id: M0, name: alice}
""".strip(),
        encoding="utf-8",
    )

    preset = load_preset(path)

    assert preset is not None
    assert preset.is_catalog_preset is False
    assert preset.trial_id is None
    assert preset.task_type is None
```

- [ ] **Step 2: Run the focused tests and confirm they fail for missing API / fields**

Run: `PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_team_preset.py -q`

Expected: FAIL with missing `load_presets`, missing `TeamPreset` fields, or invalid attribute errors.

- [ ] **Step 3: Implement catalog preset fields and `load_presets()` in `team_preset.py`**

Add the new dataclass fields and loader surface in `src/aic_collector/team_preset.py`:

```python
@dataclass(frozen=True)
class CatalogPresetIssue:
    path: Path
    message: str


@dataclass(frozen=True)
class TeamPreset:
    base_seed: int
    shard_stride: int
    index_width: int
    strategy: Literal["uniform", "lhs"]
    ranges: Mapping[str, Any]
    scene: Mapping[str, Any]
    tasks: Mapping[str, int]
    members: tuple[Mapping[str, str], ...]
    preset_hash: str
    preset_name: str
    preset_path: Path
    trial_id: Literal["trial_1", "trial_2", "trial_3"] | None = None
    task_type: Literal["sfp", "sc"] | None = None
    total_target_count: int | None = None
    batch_default_count: int | None = None
    is_catalog_preset: bool = False
```

Add a catalog loader path:

```python
def load_presets(
    dir_path: Path,
) -> tuple[tuple[TeamPreset, ...], tuple[CatalogPresetIssue, ...]]:
    if not dir_path.exists():
        return (), ()

    presets: list[TeamPreset] = []
    issues: list[CatalogPresetIssue] = []
    for path in sorted(dir_path.glob("*.yaml")):
        try:
            presets.append(_load_catalog_preset(path))
        except PresetError as exc:
            issues.append(CatalogPresetIssue(path=path, message=str(exc)))
    return tuple(presets), tuple(issues)
```

Keep `load_preset(path)` working for the legacy schema. Use `path.stem` for `preset_name`, persist `preset_path`, and set `is_catalog_preset=False` there.

- [ ] **Step 4: Add catalog schema validation helpers**

Add small validation helpers instead of parsing inline:

```python
def _validate_trial_id(value: Any) -> Literal["trial_1", "trial_2", "trial_3"]:
    if value not in {"trial_1", "trial_2", "trial_3"}:
        raise PresetError("Invalid campaign field: campaign.trial_id")
    return value


def _validate_task_type(value: Any) -> Literal["sfp", "sc"]:
    if value not in {"sfp", "sc"}:
        raise PresetError("Invalid campaign field: campaign.task_type")
    return value


def _validate_campaign_counts(total: Any, batch: Any) -> tuple[int, int]:
    total_count = _validate_positive_int(total, "campaign.total_target_count")
    batch_count = _validate_positive_int(batch, "campaign.batch_default_count")
    if batch_count > total_count:
        raise PresetError("Invalid campaign field: campaign.batch_default_count")
    return total_count, batch_count
```

Implement `_load_catalog_preset(path)` so it reads `campaign.*`, validates the active `fixed_target`, and returns `TeamPreset(..., is_catalog_preset=True)`.

- [ ] **Step 5: Re-run the loader tests**

Run: `PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_team_preset.py -q`

Expected: PASS for the new catalog loader tests and the existing legacy tests.

- [ ] **Step 6: Commit**

```bash
git add src/aic_collector/team_preset.py tests/test_team_preset.py
git commit -m "feat(team): add catalog preset loader and schema validation"
```

---

## Task 2: Add campaign progress helpers and submit-time capacity enforcement

**Files:**
- Modify: `tests/test_team_preset.py`
- Modify later: `src/aic_collector/team_preset.py`

- [ ] **Step 1: Write failing tests for campaign progress and capped submit**

Append these tests to `tests/test_team_preset.py`:

```python
def test_claimed_count_for_preset_hash_sums_only_matching_entries(tmp_path: Path) -> None:
    ledger = tmp_path / "seed_ledger.yaml"
    ledger.write_text("entries: []\n", encoding="utf-8")
    append_claim(
        ledger,
        member_id="M0",
        task_type="sfp",
        base_seed=42,
        start_index=0,
        count=100,
        strategy="uniform",
        queue_root=tmp_path / "queue",
        preset_hash="sha256:a",
        trial_id="trial_1",
        preset_name="trial_1",
    )
    append_claim(
        ledger,
        member_id="M1",
        task_type="sfp",
        base_seed=42,
        start_index=100000,
        count=50,
        strategy="uniform",
        queue_root=tmp_path / "queue",
        preset_hash="sha256:b",
        trial_id="trial_2",
        preset_name="trial_2",
    )

    assert claimed_count_for_preset(ledger, "sha256:a") == 100
    assert claimed_count_for_preset(ledger, "sha256:b") == 50


def test_submit_team_claim_rejects_request_beyond_campaign_remaining(tmp_path: Path) -> None:
    ledger = tmp_path / "seed_ledger.yaml"
    ledger.write_text("entries: []\n", encoding="utf-8")
    queue_root = tmp_path / "queue"
    preset = TeamPreset(
        base_seed=42,
        shard_stride=100000,
        index_width=6,
        strategy="uniform",
        ranges={"nic_translation": (-0.0215, 0.0234)},
        scene={
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
            "fixed_target": {"sfp": {"rail": 0, "port": "sfp_port_0"}, "sc": None},
        },
        tasks={"sfp": 100},
        members=({"id": "M0", "name": "alice"},),
        preset_hash="sha256:trial1",
        preset_name="trial_1",
        preset_path=tmp_path / "trial_1.yaml",
        trial_id="trial_1",
        task_type="sfp",
        total_target_count=120,
        batch_default_count=100,
        is_catalog_preset=True,
    )
    append_claim(
        ledger,
        member_id="M0",
        task_type="sfp",
        base_seed=42,
        start_index=0,
        count=50,
        strategy="uniform",
        queue_root=queue_root,
        preset_hash=preset.preset_hash,
        trial_id="trial_1",
        preset_name="trial_1",
    )

    with pytest.raises(SlotExhausted, match="campaign"):
        submit_team_claim(
            preset,
            member_id="M0",
            task_type="sfp",
            queue_root=queue_root,
            ledger_path=ledger,
            template_path=PROJECT_DIR / "configs/community_random_config.yaml",
        )
```

- [ ] **Step 2: Run the focused team preset tests and confirm failure**

Run: `PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_team_preset.py -q`

Expected: FAIL because `append_claim` does not accept `trial_id` / `preset_name`, `claimed_count_for_preset()` does not exist, and `submit_team_claim()` does not enforce campaign remaining.

- [ ] **Step 3: Extend ledger entry helpers with campaign metadata**

Update the ledger append path in `src/aic_collector/team_preset.py`:

```python
def _append_claim_locked(
    entries: list[dict[str, Any]],
    *,
    member_id: str,
    task_type: str,
    base_seed: int,
    start_index: int,
    count: int,
    strategy: str,
    queue_root: Path,
    preset_hash: str,
    trial_id: str | None = None,
    preset_name: str | None = None,
) -> int:
    entry: dict[str, Any] = {
        "member_id": member_id,
        "task_type": task_type,
        "base_seed": base_seed,
        "start_index": start_index,
        "count": count,
        "strategy": strategy,
        "queue_root": str(queue_root),
        "preset_hash": preset_hash,
        "git_sha": _git_sha(),
        "created_at": _iso_utc_now(),
    }
    if trial_id is not None:
        entry["trial_id"] = trial_id
    if preset_name is not None:
        entry["preset_name"] = preset_name
    entries.append(entry)
    return len(entries) - 1
```

Thread `trial_id` and `preset_name` through `append_claim()` and `submit_team_claim()`.

- [ ] **Step 4: Add a helper for campaign claimed count and enforce remaining capacity inside the lock**

Add:

```python
def claimed_count_for_preset(ledger_path: Path, preset_hash: str) -> int:
    return sum(
        int(entry["count"])
        for entry in _ledger_entries(ledger_path)
        if entry.get("preset_hash") == preset_hash
        and isinstance(entry.get("count"), int)
        and not isinstance(entry.get("count"), bool)
    )
```

Inside `submit_team_claim()`, after loading `entries` and before `_append_claim_locked(...)`, add:

```python
if preset.is_catalog_preset and preset.total_target_count is not None:
    claimed = sum(
        int(entry["count"])
        for entry in entries
        if entry.get("preset_hash") == preset.preset_hash
        and isinstance(entry.get("count"), int)
        and not isinstance(entry.get("count"), bool)
    )
    remaining_campaign = preset.total_target_count - claimed
    if remaining_campaign <= 0:
        raise SlotExhausted(f"No remaining campaign capacity for preset: {preset.preset_name}")
    if requested_count > remaining_campaign:
        raise SlotExhausted(
            f"Requested count exceeds remaining campaign capacity for preset: {preset.preset_name}"
        )
```

Keep the existing slot-range enforcement as a separate check.

- [ ] **Step 5: Run the full team preset test file again**

Run: `PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_team_preset.py -q`

Expected: PASS, including the new progress/capacity coverage and all previously added tests.

- [ ] **Step 6: Commit**

```bash
git add src/aic_collector/team_preset.py tests/test_team_preset.py
git commit -m "feat(team): track campaign progress and enforce remaining capacity"
```

---

## Task 3: Add the three shipped campaign preset files

**Files:**
- Create: `configs/team/presets/trial_1.yaml`
- Create: `configs/team/presets/trial_2.yaml`
- Create: `configs/team/presets/trial_3.yaml`
- Test via loader commands only

- [ ] **Step 1: Create `trial_1.yaml`**

Create `configs/team/presets/trial_1.yaml`:

```yaml
version: 1
campaign:
  trial_id: trial_1
  task_type: sfp
  total_target_count: 1000
  batch_default_count: 100
team:
  base_seed: 42
  shard_stride: 100000
  index_width: 6
sampling:
  strategy: uniform
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
  target_cycling: false
  fixed_target:
    sfp: {rail: 0, port: "sfp_port_0"}
    sc: null
members:
  - {id: M0, name: alice}
  - {id: M1, name: bob}
  - {id: M2, name: carol}
  - {id: M3, name: dave}
  - {id: M4, name: eve}
  - {id: M5, name: frank}
```

- [ ] **Step 2: Create `trial_2.yaml` and `trial_3.yaml`**

Create `configs/team/presets/trial_2.yaml`:

```yaml
version: 1
campaign:
  trial_id: trial_2
  task_type: sfp
  total_target_count: 1000
  batch_default_count: 100
team:
  base_seed: 42
  shard_stride: 100000
  index_width: 6
sampling:
  strategy: uniform
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
  target_cycling: false
  fixed_target:
    sfp: {rail: 1, port: "sfp_port_0"}
    sc: null
members:
  - {id: M0, name: alice}
  - {id: M1, name: bob}
  - {id: M2, name: carol}
  - {id: M3, name: dave}
  - {id: M4, name: eve}
  - {id: M5, name: frank}
```

Create `configs/team/presets/trial_3.yaml`:

```yaml
version: 1
campaign:
  trial_id: trial_3
  task_type: sc
  total_target_count: 1000
  batch_default_count: 100
team:
  base_seed: 42
  shard_stride: 100000
  index_width: 6
sampling:
  strategy: uniform
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
  target_cycling: false
  fixed_target:
    sfp: null
    sc: {rail: 1, port: "sc_port_1"}
members:
  - {id: M0, name: alice}
  - {id: M1, name: bob}
  - {id: M2, name: carol}
  - {id: M3, name: dave}
  - {id: M4, name: eve}
  - {id: M5, name: frank}
```

- [ ] **Step 3: Smoke-test the preset catalog through the loader**

Run:

```bash
PYTHONPATH=src python - <<'PY'
from pathlib import Path
from aic_collector.team_preset import load_presets
presets, issues = load_presets(Path("configs/team/presets"))
print([p.preset_name for p in presets])
print([(p.trial_id, p.task_type, p.batch_default_count) for p in presets])
print(issues)
PY
```

Expected output:

```text
['trial_1', 'trial_2', 'trial_3']
[('trial_1', 'sfp', 100), ('trial_2', 'sfp', 100), ('trial_3', 'sc', 100)]
()
```

- [ ] **Step 4: Commit**

```bash
git add configs/team/presets/trial_1.yaml configs/team/presets/trial_2.yaml configs/team/presets/trial_3.yaml
git commit -m "chore(team): add catalog presets for trials 1 2 and 3"
```

---

## Task 4: Generalize webapp state helpers for catalog mode and SC team mode

**Files:**
- Modify: `tests/test_webapp_team_mode_state.py`
- Modify later: `src/aic_collector/webapp.py`

- [ ] **Step 1: Write failing tests for generic team mode state and campaign captions**

Append these tests to `tests/test_webapp_team_mode_state.py`:

```python
def test_build_team_mode_state_uses_batch_default_and_campaign_remaining(tmp_path: Path) -> None:
    preset = TeamPreset(
        base_seed=42,
        shard_stride=100000,
        index_width=6,
        strategy="uniform",
        ranges=_preset().ranges,
        scene={
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
            "fixed_target": {"sfp": {"rail": 0, "port": "sfp_port_0"}, "sc": None},
        },
        tasks={"sfp_default_count": 0},
        members=_preset().members,
        preset_hash="sha256:trial_1",
        preset_name="trial_1",
        preset_path=tmp_path / "trial_1.yaml",
        trial_id="trial_1",
        task_type="sfp",
        total_target_count=1000,
        batch_default_count=100,
        is_catalog_preset=True,
    )
    ledger = tmp_path / "seed_ledger.yaml"
    ledger.write_text(
        \"\"\"entries:
  - member_id: m0
    task_type: sfp
    trial_id: trial_1
    preset_name: trial_1
    base_seed: 42
    start_index: 0
    count: 940
    strategy: uniform
    queue_root: queue
    preset_hash: sha256:trial_1
    git_sha: abc
    created_at: 2026-04-20T00:00:00Z
\"\"\",
        encoding="utf-8",
    )

    state = build_team_mode_state(
        preset,
        queue_root=tmp_path / "queue",
        ledger_path=ledger,
        member_id="m0",
    )

    assert state["task_type"] == "sfp"
    assert state["default_count"] == 60
    assert state["selected_count"] == 60
    assert state["campaign_claimed"] == 940
    assert state["campaign_remaining"] == 60
    assert state["campaign_complete"] is False


def test_build_team_mode_state_supports_sc_catalog_preset(tmp_path: Path) -> None:
    preset = TeamPreset(
        base_seed=42,
        shard_stride=100000,
        index_width=6,
        strategy="uniform",
        ranges=_preset().ranges,
        scene={
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
            "fixed_target": {"sfp": None, "sc": {"rail": 1, "port": "sc_port_1"}},
        },
        tasks={"sc_default_count": 0},
        members=_preset().members,
        preset_hash="sha256:trial_3",
        preset_name="trial_3",
        preset_path=tmp_path / "trial_3.yaml",
        trial_id="trial_3",
        task_type="sc",
        total_target_count=1000,
        batch_default_count=100,
        is_catalog_preset=True,
    )

    state = build_team_mode_state(
        preset,
        queue_root=tmp_path / "queue",
        ledger_path=tmp_path / "seed_ledger.yaml",
        member_id="m0",
    )

    assert state["task_type"] == "sc"
    assert state["preview_filename"] == "config_sc_000000.yaml"
    assert state["default_count"] == 100


def test_build_team_campaign_summary_formats_progress() -> None:
    summary = build_team_campaign_summary(
        TeamPreset(
            base_seed=42,
            shard_stride=100000,
            index_width=6,
            strategy="uniform",
            ranges={},
            scene={},
            tasks={},
            members=(),
            preset_hash="sha256:trial_2",
            preset_name="trial_2",
            preset_path=Path("trial_2.yaml"),
            trial_id="trial_2",
            task_type="sfp",
            total_target_count=1000,
            batch_default_count=100,
            is_catalog_preset=True,
        ),
        {
            "campaign_claimed": 600,
            "campaign_remaining": 400,
            "campaign_complete": False,
        },
    )

    assert summary == {
        "caption": "캠페인: trial_2 · SFP · 목표 1000 · 예약/생성 600 · 남은 목표 400",
        "campaign_complete_info": "",
    }
```

- [ ] **Step 2: Run the focused state tests and confirm they fail**

Run: `PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_webapp_team_mode_state.py -q`

Expected: FAIL because `build_team_mode_state()` is SFP-only, `build_team_campaign_summary()` does not exist, and filenames are hard-coded to `sfp`.

- [ ] **Step 3: Generalize `build_team_mode_state()` and `build_team_submit_preset()`**

In `src/aic_collector/webapp.py`, replace the SFP-only logic with task-type-aware logic:

```python
def _active_team_task_type(preset: TeamPreset) -> str:
    if preset.is_catalog_preset and preset.task_type is not None:
        return preset.task_type
    if _preset_task_count(preset, "sc_default_count") != 0:
        raise PresetError("Unsupported team preset task count: tasks.sc_default_count must be 0")
    return "sfp"
```

Update `build_team_mode_state()` so it computes:

```python
task_type = _active_team_task_type(preset)
requested_count = default_count if requested_count is None else int(requested_count)
preview_filename = f"config_{task_type}_{next_start_index:0{preset.index_width}d}.yaml"
```

For catalog presets, derive:

```python
campaign_claimed = claimed_count_for_preset(ledger_path, preset.preset_hash) if ledger_path else 0
campaign_remaining = max(int(preset.total_target_count or 0) - campaign_claimed, 0)
default_count = min(int(preset.batch_default_count or 0), remaining_slots, campaign_remaining)
selected_count = min(requested_count, remaining_slots, campaign_remaining)
campaign_complete = campaign_remaining == 0
```

Update `build_team_submit_preset()` to set the active task count dynamically:

```python
def build_team_submit_preset(preset: TeamPreset, *, count: int) -> TeamPreset:
    task_type = _active_team_task_type(preset)
    tasks = dict(preset.tasks)
    tasks[task_type] = int(count)
    if task_type == "sfp":
        tasks["sc"] = 0
    else:
        tasks["sfp"] = 0
    return replace(preset, tasks=tasks)
```

- [ ] **Step 4: Add campaign summary helper and keep slot summary intact**

Add:

```python
def build_team_campaign_summary(
    preset: TeamPreset | None,
    team_state: dict[str, Any] | None,
) -> dict[str, str] | None:
    if preset is None or team_state is None or not preset.is_catalog_preset:
        return None
    task_label = str(preset.task_type or "").upper()
    return {
        "caption": (
            f"캠페인: {preset.trial_id} · {task_label} · 목표 {int(preset.total_target_count or 0)}"
            f" · 예약/생성 {int(team_state['campaign_claimed'])}"
            f" · 남은 목표 {int(team_state['campaign_remaining'])}"
        ),
        "campaign_complete_info": (
            f"{preset.preset_name} 캠페인 목표를 모두 채웠습니다."
            if team_state.get("campaign_complete")
            else ""
        ),
    }
```

- [ ] **Step 5: Re-run the state test file**

Run: `PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_webapp_team_mode_state.py -q`

Expected: PASS for the new generic state/caption tests and existing state coverage.

- [ ] **Step 6: Commit**

```bash
git add src/aic_collector/webapp.py tests/test_webapp_team_mode_state.py
git commit -m "feat(webapp): generalize team state for catalog campaigns and sc mode"
```

---

## Task 5: Add catalog preset selection and submit integration to the webapp

**Files:**
- Modify: `tests/test_webapp_team_mode.py`
- Modify later: `src/aic_collector/webapp.py`

- [ ] **Step 1: Write failing integration tests for `trial_2` and `trial_3` submits**

Append these tests to `tests/test_webapp_team_mode.py`:

```python
def test_submit_team_claim_trial_2_writes_fixed_sfp_target(tmp_path: Path) -> None:
    preset = TeamPreset(
        base_seed=42,
        shard_stride=100000,
        index_width=6,
        strategy="uniform",
        ranges={},
        scene={
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
            "fixed_target": {"sfp": {"rail": 1, "port": "sfp_port_0"}, "sc": None},
        },
        tasks={"sfp": 2, "sc": 0},
        members=({"id": "M1", "name": "bob"},),
        preset_hash="sha256:trial_2",
        preset_name="trial_2",
        preset_path=tmp_path / "trial_2.yaml",
        trial_id="trial_2",
        task_type="sfp",
        total_target_count=1000,
        batch_default_count=100,
        is_catalog_preset=True,
    )
    ledger = tmp_path / "seed_ledger.yaml"
    ledger.write_text("entries: []\n", encoding="utf-8")

    result = submit_team_claim(
        preset,
        member_id="M1",
        task_type="sfp",
        queue_root=tmp_path / "queue",
        ledger_path=ledger,
        template_path=PROJECT_DIR / "configs/community_random_config.yaml",
    )

    assert result.written_count == 2
    payload = yaml.safe_load(
        (tmp_path / "queue" / "sfp" / "pending" / "config_sfp_100000.yaml").read_text(encoding="utf-8")
    )
    assert payload["training"]["collection"]["fixed_target"]["sfp"] == {"rail": 1, "port": "sfp_port_0"}


def test_submit_team_claim_trial_3_writes_sc_files(tmp_path: Path) -> None:
    preset = TeamPreset(
        base_seed=42,
        shard_stride=100000,
        index_width=6,
        strategy="uniform",
        ranges={},
        scene={
            "nic_count_range": [1, 1],
            "sc_count_range": [1, 1],
            "target_cycling": False,
            "fixed_target": {"sfp": None, "sc": {"rail": 1, "port": "sc_port_1"}},
        },
        tasks={"sfp": 0, "sc": 2},
        members=({"id": "M0", "name": "alice"},),
        preset_hash="sha256:trial_3",
        preset_name="trial_3",
        preset_path=tmp_path / "trial_3.yaml",
        trial_id="trial_3",
        task_type="sc",
        total_target_count=1000,
        batch_default_count=100,
        is_catalog_preset=True,
    )
    ledger = tmp_path / "seed_ledger.yaml"
    ledger.write_text("entries: []\n", encoding="utf-8")

    result = submit_team_claim(
        preset,
        member_id="M0",
        task_type="sc",
        queue_root=tmp_path / "queue",
        ledger_path=ledger,
        template_path=PROJECT_DIR / "configs/community_random_config.yaml",
    )

    assert result.written_count == 2
    assert (tmp_path / "queue" / "sc" / "pending" / "config_sc_000000.yaml").exists()
```

- [ ] **Step 2: Run the webapp integration tests and confirm failure**

Run: `PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_webapp_team_mode.py -q`

Expected: FAIL because catalog presets are not selectable in UI state, SC submit is not wired in team mode, or `submit_team_claim()` metadata is incomplete.

- [ ] **Step 3: Add catalog preset discovery and selection constants**

At the top of `src/aic_collector/webapp.py`, replace the single preset constant block:

```python
PRESET_PATH = PROJECT_DIR / "configs/team/preset.yaml"
LEDGER_PATH = PROJECT_DIR / "configs/team/seed_ledger.yaml"
```

with:

```python
PRESET_DIR = PROJECT_DIR / "configs/team/presets"
LEGACY_PRESET_PATH = PROJECT_DIR / "configs/team/preset.yaml"
LEDGER_PATH = PROJECT_DIR / "configs/team/seed_ledger.yaml"
```

and import `load_presets`:

```python
from aic_collector.team_preset import (
    CatalogPresetIssue,
    PresetError,
    SlotExhausted,
    TeamPreset,
    claimed_count_for_preset,
    load_preset,
    load_presets,
    next_start_index_in_slot,
    slot_range,
    submit_team_claim,
)
```

- [ ] **Step 4: Implement team mode activation precedence and preset selectbox**

In the team-mode setup block, use:

```python
catalog_presets, catalog_issues = load_presets(PRESET_DIR)
legacy_preset = load_preset(LEGACY_PRESET_PATH)
```

and apply the spec precedence exactly:

```python
if list(PRESET_DIR.glob("*.yaml")):
    if catalog_presets:
        team_mode_active = True
        active_catalog_mode = True
    else:
        team_mode_error = PresetError("No valid team campaign presets found in configs/team/presets")
elif legacy_preset is not None:
    team_mode_active = True
    active_catalog_mode = False
    team_preset = legacy_preset
else:
    team_mode_active = False
```

When catalog mode is active, add:

```python
if st.session_state.get("mgr_team_preset") not in [p.preset_name for p in catalog_presets]:
    st.session_state["mgr_team_preset"] = catalog_presets[0].preset_name
selected_preset_name = st.selectbox(
    "Preset",
    options=[p.preset_name for p in catalog_presets],
    key="mgr_team_preset",
    format_func=lambda name: next(
        f"{p.preset_path.name} - {p.trial_id} - {str(p.task_type).upper()}"
        for p in catalog_presets
        if p.preset_name == name
    ),
)
team_preset = next(p for p in catalog_presets if p.preset_name == selected_preset_name)
```

Also surface any `catalog_issues` with `st.error(f"팀 preset 오류 ({issue.path.name}): {issue.message}")`.

- [ ] **Step 5: Wire task-type-aware inputs, preview, and submit**

Change the team-mode count widgets to read `team_state["task_type"]`:

```python
active_task_type = str(team_state["task_type"]) if team_widgets_locked else None
mgr_sfp_count = st.number_input(..., disabled=team_widgets_locked and active_task_type != "sfp")
mgr_sc_count = st.number_input(..., disabled=team_widgets_locked and active_task_type != "sc")
```

When locked:

```python
active_count_value = int(team_state["selected_count"])
if active_task_type == "sfp":
    st.session_state["mgr_sfp_count"] = active_count_value
    st.session_state["mgr_sc_count"] = 0
else:
    st.session_state["mgr_sfp_count"] = 0
    st.session_state["mgr_sc_count"] = active_count_value
```

Use the active task type in preview and submit:

```python
preview_task_type = active_task_type or "sfp"
preview_fixed_target = team_preview_scene_cfg.get("collection", {}).get("fixed_target") if team_preview_scene_cfg else None
_scene_svg = render_scene_svg(
    nic_range=mgr_nic_count_range,
    sc_range=mgr_sc_count_range,
    target_cycling=bool(mgr_target_cycling),
    ranges=dict(user_ranges),
    fixed_target=preview_fixed_target,
    task_type=preview_task_type,
    sample_count=3,
)
```

```python
submit_count = int(mgr_sfp_count if active_task_type == "sfp" else mgr_sc_count)
submit_preset = build_team_submit_preset(team_preset, count=submit_count)
result = submit_team_claim(
    submit_preset,
    member_id=mgr_team_member_id,
    task_type=active_task_type,
    queue_root=mgr_queue_root,
    ledger_path=LEDGER_PATH,
    template_path=template,
)
```

- [ ] **Step 6: Re-run the webapp integration tests**

Run: `PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_webapp_team_mode.py tests/test_webapp_team_mode_state.py -q`

Expected: PASS, including `trial_2` fixed-target submit and `trial_3` SC queue writes.

- [ ] **Step 7: Commit**

```bash
git add src/aic_collector/webapp.py tests/test_webapp_team_mode.py tests/test_webapp_team_mode_state.py
git commit -m "feat(webapp): add selectable catalog team campaigns"
```

---

## Task 6: Update operator-facing docs for catalog presets and batching

**Files:**
- Modify: `README.md`
- Modify: `docs/usage-guide.md`
- Modify: `docs/config-reference.md`

- [ ] **Step 1: Update README team mode section**

Replace the current single-preset wording in `README.md` with:

```md
`configs/team/presets/*.yaml` 파일이 있으면 **작업 관리 탭**이 catalog team mode로 전환됩니다.

- `Preset`에서 `trial_1`, `trial_2`, `trial_3` 중 하나를 고릅니다.
- 각 preset은 하나의 캠페인입니다. 기본 목표는 `1000`개이고, 기본 배치는 `100`개입니다.
- `trial_1`, `trial_2`는 SFP만, `trial_3`는 SC만 생성합니다.
- 슬롯 예약은 항상 전역 `configs/team/seed_ledger.yaml` 기준으로 처리됩니다.
- catalog preset이 없고 `configs/team/preset.yaml`만 있으면 기존 legacy team mode로 동작합니다.
```

- [ ] **Step 2: Update usage guide with the batching workflow**

In `docs/usage-guide.md`, replace the current team mode subsection with:

```md
### 👥 Team Mode

`configs/team/presets/*.yaml`이 존재하면 작업 관리 탭은 catalog team mode로 전환됩니다.

- `Preset`에서 `trial_1`, `trial_2`, `trial_3` 중 하나를 선택합니다.
- 각 preset은 `goal=1000`, `batch default=100`을 가집니다. 즉, 한 번에 1000개를 몰아서 만들지 않고 여러 번 나눠서 수집할 수 있습니다.
- `trial_1`, `trial_2`는 `SFP configs`만 활성화되고, `trial_3`는 `SC configs`만 활성화됩니다.
- UI에는 멤버 슬롯 진행과 캠페인 진행(`목표`, `예약/생성`, `남은 목표`)이 함께 표시됩니다.
- 목표 수량을 모두 채우면 해당 preset submit은 자동으로 비활성화됩니다.
- catalog preset이 없고 `configs/team/preset.yaml`만 있으면 기존 legacy team mode를 유지합니다.
```

- [ ] **Step 3: Add config reference for the campaign schema**

Append to `docs/config-reference.md`:

```md
## Team Campaign Preset (`configs/team/presets/*.yaml`)

| 경로 | 타입 | 설명 |
|------|------|------|
| `campaign.trial_id` | string | `trial_1`, `trial_2`, `trial_3` 중 하나 |
| `campaign.task_type` | string | `sfp` 또는 `sc` |
| `campaign.total_target_count` | int | 캠페인 전체 목표 수량 |
| `campaign.batch_default_count` | int | UI 기본 배치 크기 |
| `scene.fixed_target` | mapping | active task의 고정 rail/port |

예시:

    campaign:
      trial_id: trial_3
      task_type: sc
      total_target_count: 1000
      batch_default_count: 100
    scene:
      fixed_target:
        sfp: null
        sc: {rail: 1, port: "sc_port_1"}
```

- [ ] **Step 4: Smoke-check the docs for the new preset paths**

Run: `rg -n "configs/team/presets|batch_default_count|trial_3" README.md docs/usage-guide.md docs/config-reference.md`

Expected: matches in all three files with no remaining README/guide claims that catalog mode is SFP-only.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/usage-guide.md docs/config-reference.md
git commit -m "docs(team): document catalog campaign presets and batching"
```

---

## Final Verification

- [ ] **Step 1: Run the targeted regression suite**

Run:

```bash
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
  tests/test_team_preset.py \
  tests/test_webapp_team_mode.py \
  tests/test_webapp_team_mode_state.py -q
```

Expected: all three files PASS.

- [ ] **Step 2: Run Ruff on touched code**

Run:

```bash
uv run --with ruff ruff check \
  src/aic_collector/team_preset.py \
  src/aic_collector/webapp.py \
  tests/test_team_preset.py \
  tests/test_webapp_team_mode.py \
  tests/test_webapp_team_mode_state.py
```

Expected: `All checks passed!`

- [ ] **Step 3: Optional manual smoke**

Run:

```bash
uv run src/aic_collector/webapp.py
```

Verify manually:

1. `Preset` dropdown shows `trial_1`, `trial_2`, `trial_3`.
2. `trial_1` / `trial_2` enable only `SFP configs`.
3. `trial_3` enables only `SC configs`.
4. campaign caption shows `목표 1000` and default batch `100`.
5. repeated submits reduce `남은 목표` until completion.

- [ ] **Step 4: Final commit**

```bash
git status --short
git add configs/team/presets src/aic_collector/team_preset.py src/aic_collector/webapp.py tests/test_team_preset.py tests/test_webapp_team_mode.py tests/test_webapp_team_mode_state.py README.md docs/usage-guide.md docs/config-reference.md
git commit -m "feat(team): add selectable campaign presets for trials 1 2 and 3"
```

---

## Self-Review Notes

- **Spec coverage:** catalog presets, `trial_1/2/3`, `1000` goal, `100` batch default, SC support for `trial_3`, preset-hash-scoped progress, legacy fallback, docs, and tests are all mapped to tasks.
- **Placeholder scan:** no `TODO`, `TBD`, or "implement later" markers remain.
- **Type consistency:** the plan consistently uses `trial_id`, `task_type`, `total_target_count`, `batch_default_count`, `preset_name`, `preset_hash`, and `CatalogPresetIssue`.
