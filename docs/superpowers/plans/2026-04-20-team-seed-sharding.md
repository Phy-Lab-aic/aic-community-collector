# Team Seed Sharding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Six engineers can collect trial_1 simulation data in parallel without filename collisions, seed collisions, or config drift, with a git-tracked audit trail for reproduction.

**Architecture:** A single git-tracked `configs/team/preset.yaml` becomes the team source of truth. The Task Management tab loads the preset, offers a Member dropdown that deterministically derives `start_index = member_index * shard_stride`, locks every other widget to preset values, and appends to `configs/team/seed_ledger.yaml` on submit. The sampler gains a `collection.fixed_target` branch to pin trial_1 to `(nic_rail_0, sfp_port_0)`. The writer defaults `index_width` to 6 to fit 6-digit member slots.

**Tech Stack:** Python 3.12, Streamlit, PyYAML, pytest, NumPy, `fcntl` for ledger locking.

---

## Spec

Reference spec: `docs/superpowers/specs/2026-04-20-team-seed-sharding-design.md`

## File Structure

**Create:**
- `src/aic_collector/team_preset.py` — preset loader, slot math, ledger append/rollback
- `configs/team/preset.yaml` — team-wide preset (example + ready to use)
- `configs/team/seed_ledger.yaml` — empty ledger scaffold
- `tests/test_team_preset.py` — unit tests for `team_preset`
- `tests/test_webapp_team_mode.py` — integration tests for submit flow

**Modify:**
- `src/aic_collector/job_queue/writer.py` — default `index_width` 4 → 6
- `src/aic_collector/sampler.py` — honor `collection.fixed_target`
- `src/aic_collector/webapp.py` — preset-aware Task Management tab
- `tests/test_job_queue.py` — extend for 6-digit defaults + mixed-width `next_sample_index`
- `tests/test_training_sampler.py` — extend for `fixed_target` behavior

Each file has one clear responsibility. `team_preset.py` stays pure (no Streamlit imports) so it is unit-testable in isolation.

---

## Task 1: Widen filename padding to 6 digits

**Files:**
- Modify: `src/aic_collector/job_queue/writer.py:30,50`
- Test: `tests/test_job_queue.py` (extend)

- [ ] **Step 1: Write the failing test for default 6-digit padding**

Append to `tests/test_job_queue.py`:

```python
def test_write_plan_default_index_width_is_six() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        plans = sample_scenes(
            training_cfg={"parameters": {"strategy": "uniform"},
                          "collection": {"seed": 42}},
            task_type="sfp",
            count=1,
            seed=42,
            start_index=200000,
        )
        written = write_plans(plans, root, TEMPLATE_PATH)
        assert written[0].name == "config_sfp_200000.yaml"


def test_write_plan_explicit_index_width_preserved() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        plans = sample_scenes(
            training_cfg={"parameters": {"strategy": "uniform"},
                          "collection": {"seed": 42}},
            task_type="sfp",
            count=1,
            seed=42,
            start_index=50,
        )
        written = write_plans(plans, root, TEMPLATE_PATH, index_width=4)
        assert written[0].name == "config_sfp_0050.yaml"


def test_next_sample_index_handles_mixed_widths() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        pending = queue_dir(root, "sfp", QueueState.PENDING)
        pending.mkdir(parents=True)
        (pending / "config_sfp_0050.yaml").write_text("x: 1")
        (pending / "config_sfp_200000.yaml").write_text("x: 1")
        assert next_sample_index(root, "sfp") == 200001
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_job_queue.py::test_write_plan_default_index_width_is_six -v`
Expected: FAIL (filename is `config_sfp_200000.yaml` vs `config_sfp_0200000.yaml` mismatch? No — the 4-digit default would produce `config_sfp_200000.yaml` because `:04d` on a 6-digit number pads to at least 4, so it stays 6. Re-check: the failure is that current default is 4 but the default filename produced should still match because `{200000:04d}` → `"200000"`. So this test may not fail on default! Instead we need a test that asserts width is *exactly* 6, e.g., for a small number.)

Replace the first test:

```python
def test_write_plan_default_index_width_is_six() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        plans = sample_scenes(
            training_cfg={"parameters": {"strategy": "uniform"},
                          "collection": {"seed": 42}},
            task_type="sfp",
            count=1,
            seed=42,
            start_index=7,
        )
        written = write_plans(plans, root, TEMPLATE_PATH)
        assert written[0].name == "config_sfp_000007.yaml"
```

Run: `uv run pytest tests/test_job_queue.py::test_write_plan_default_index_width_is_six -v`
Expected: FAIL — produces `config_sfp_0007.yaml` with current default of 4.

- [ ] **Step 3: Change the default in writer.py**

In `src/aic_collector/job_queue/writer.py`, change both function signatures:

```python
def write_plan(
    plan: ScenePlan,
    root: Path,
    template_path: Path,
    index_width: int = 6,
) -> Path:
    ...


def write_plans(
    plans: list[ScenePlan],
    root: Path,
    template_path: Path,
    index_width: int = 6,
) -> list[Path]:
    ...
```

Also update the `next_sample_index` signature:

```python
def next_sample_index(
    root: Path,
    task_type: str,
    index_width: int = 6,
) -> int:
    ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_job_queue.py -v`
Expected: PASS for the three new tests. All pre-existing tests that used the default still pass because the regex `\d+` matches both widths.

- [ ] **Step 5: Commit**

```bash
git add src/aic_collector/job_queue/writer.py tests/test_job_queue.py
git commit -m "feat(writer): default index_width to 6 digits for team-shard filenames"
```

---

## Task 2: Honor `collection.fixed_target` in sampler

**Files:**
- Modify: `src/aic_collector/sampler.py:398`
- Test: `tests/test_training_sampler.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_training_sampler.py`:

```python
def test_sample_training_configs_fixed_target_sfp() -> None:
    cfg = {
        "parameters": {"strategy": "uniform"},
        "collection": {
            "seed": 42,
            "fixed_target": {"sfp": {"rail": 0, "port": "sfp_port_0"}},
        },
    }
    samples = sample_training_configs(cfg, task_type="sfp", count=5, seed=42)
    for s in samples:
        assert s.target_rail == 0
        assert s.target_port_name == "sfp_port_0"


def test_sample_training_configs_no_fixed_target_preserves_cycle() -> None:
    # Regression guard: without fixed_target, SFP cycling still happens.
    cfg = {"parameters": {"strategy": "uniform"}, "collection": {"seed": 42}}
    samples = sample_training_configs(cfg, task_type="sfp", count=10, seed=42)
    rails = {s.target_rail for s in samples}
    assert len(rails) > 1, "default cycle must produce multiple rails"


def test_fixed_target_does_not_perturb_per_seed() -> None:
    base = {"parameters": {"strategy": "uniform"}, "collection": {"seed": 42}}
    fixed = {
        "parameters": {"strategy": "uniform"},
        "collection": {
            "seed": 42,
            "fixed_target": {"sfp": {"rail": 0, "port": "sfp_port_0"}},
        },
    }
    a = sample_training_configs(base, task_type="sfp", count=3, seed=42, start_index=0)
    b = sample_training_configs(fixed, task_type="sfp", count=3, seed=42, start_index=0)
    assert [s.seed for s in a] == [s.seed for s in b]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_training_sampler.py::test_sample_training_configs_fixed_target_sfp -v`
Expected: FAIL — rails vary.

- [ ] **Step 3: Add the branch in sampler.py**

In `src/aic_collector/sampler.py` at line ~398, replace:

```python
cycle = SFP_TARGET_CYCLE if task_type == "sfp" else SC_TARGET_CYCLE
```

with:

```python
fixed = (
    training_cfg.get("collection", {})
    .get("fixed_target", {})
    .get(task_type)
)
if fixed:
    cycle = [(int(fixed["rail"]), str(fixed["port"]))]
else:
    cycle = SFP_TARGET_CYCLE if task_type == "sfp" else SC_TARGET_CYCLE
```

Leave `global_index % len(cycle)` logic unchanged — a length-1 cycle always yields the fixed target.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_training_sampler.py -v`
Expected: PASS for all three new tests, plus all pre-existing tests remain green.

- [ ] **Step 5: Commit**

```bash
git add src/aic_collector/sampler.py tests/test_training_sampler.py
git commit -m "feat(sampler): honor collection.fixed_target for trial_1-only runs"
```

---

## Task 3: Scaffold `team_preset` module with `TeamPreset` + `load_preset`

**Files:**
- Create: `src/aic_collector/team_preset.py`
- Create: `tests/test_team_preset.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_team_preset.py`:

```python
#!/usr/bin/env python3
"""Unit tests for aic_collector.team_preset."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

import pytest  # noqa: E402

from aic_collector.team_preset import (  # noqa: E402
    PresetError,
    TeamPreset,
    load_preset,
)


VALID_PRESET_YAML = """
version: 1
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
tasks:
  sfp_default_count: 1000
  sc_default_count: 0
members:
  - {id: M0, name: "alice"}
  - {id: M1, name: "bob"}
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "preset.yaml"
    p.write_text(text)
    return p


def test_load_preset_happy(tmp_path: Path) -> None:
    p = _write(tmp_path, VALID_PRESET_YAML)
    preset = load_preset(p)
    assert preset is not None
    assert preset.base_seed == 42
    assert preset.shard_stride == 100000
    assert preset.index_width == 6
    assert preset.strategy == "uniform"
    assert preset.members[0]["id"] == "M0"
    assert preset.preset_hash.startswith("sha256:")


def test_load_preset_missing_returns_none(tmp_path: Path) -> None:
    assert load_preset(tmp_path / "does_not_exist.yaml") is None


def test_load_preset_malformed_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, "not: [valid: yaml")
    with pytest.raises(PresetError):
        load_preset(p)


def test_load_preset_missing_field_raises(tmp_path: Path) -> None:
    broken = VALID_PRESET_YAML.replace("base_seed: 42", "")
    p = _write(tmp_path, broken)
    with pytest.raises(PresetError, match="base_seed"):
        load_preset(p)


def test_preset_hash_stable_across_key_order(tmp_path: Path) -> None:
    reordered = VALID_PRESET_YAML.replace(
        "base_seed: 42\n  shard_stride: 100000",
        "shard_stride: 100000\n  base_seed: 42",
    )
    a = load_preset(_write(tmp_path / "a", VALID_PRESET_YAML.lstrip() or VALID_PRESET_YAML))
    # Use two dirs to avoid overwriting
```

Simplify the last test — create two temp dirs:

```python
def test_preset_hash_stable_across_key_order(tmp_path: Path) -> None:
    reordered = VALID_PRESET_YAML.replace(
        "base_seed: 42\n  shard_stride: 100000\n  index_width: 6",
        "index_width: 6\n  shard_stride: 100000\n  base_seed: 42",
    )
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir(); b_dir.mkdir()
    a = load_preset(_write(a_dir, VALID_PRESET_YAML))
    b = load_preset(_write(b_dir, reordered))
    assert a is not None and b is not None
    assert a.preset_hash == b.preset_hash
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_team_preset.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Create the module**

Create `src/aic_collector/team_preset.py`:

```python
"""Team-wide preset and seed ledger for coordinated data collection.

This module is pure Python (no Streamlit). It provides:
- Immutable TeamPreset dataclass loaded from configs/team/preset.yaml
- Slot math (member_id -> disjoint start_index range)
- Append-only ledger operations for audit trail

Absent preset.yaml == solo mode. load_preset returns None; callers fall back
to legacy behavior.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml


class PresetError(Exception):
    """Preset file exists but is malformed or missing required fields."""


class SlotExhausted(Exception):
    """start_index + count would exceed the member's slot boundary."""


@dataclass(frozen=True)
class TeamPreset:
    base_seed: int
    shard_stride: int
    index_width: int
    strategy: Literal["uniform", "lhs"]
    ranges: dict[str, Any]
    scene: dict[str, Any]
    tasks: dict[str, int]
    members: list[dict[str, str]]
    preset_hash: str


_REQUIRED_PATHS = [
    ("team", "base_seed"),
    ("team", "shard_stride"),
    ("team", "index_width"),
    ("sampling", "strategy"),
    ("sampling", "ranges"),
    ("scene",),
    ("tasks",),
    ("members",),
]


def _get(data: dict, path: tuple[str, ...]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            raise PresetError(f"missing required field: {'.'.join(path)}")
        cur = cur[key]
    return cur


def _canonical_hash(data: dict) -> str:
    blob = json.dumps(data, sort_keys=True, default=str).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def load_preset(path: Path) -> TeamPreset | None:
    """Load preset yaml. Return None if absent; raise PresetError if malformed."""
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PresetError(f"malformed yaml: {exc}") from exc
    if not isinstance(data, dict):
        raise PresetError("preset root must be a mapping")

    for p in _REQUIRED_PATHS:
        _get(data, p)

    preset_hash = _canonical_hash(data)

    return TeamPreset(
        base_seed=int(_get(data, ("team", "base_seed"))),
        shard_stride=int(_get(data, ("team", "shard_stride"))),
        index_width=int(_get(data, ("team", "index_width"))),
        strategy=str(_get(data, ("sampling", "strategy"))),
        ranges=dict(_get(data, ("sampling", "ranges"))),
        scene=dict(_get(data, ("scene",))),
        tasks=dict(_get(data, ("tasks",))),
        members=list(_get(data, ("members",))),
        preset_hash=preset_hash,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_team_preset.py -v`
Expected: PASS for all 5 tests in this task's group.

- [ ] **Step 5: Commit**

```bash
git add src/aic_collector/team_preset.py tests/test_team_preset.py
git commit -m "feat(team_preset): add TeamPreset dataclass and load_preset"
```

---

## Task 4: Slot math — `slot_range` and `next_start_index_in_slot`

**Files:**
- Modify: `src/aic_collector/team_preset.py`
- Modify: `tests/test_team_preset.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_team_preset.py`:

```python
from aic_collector.job_queue import QueueState, queue_dir  # noqa: E402

from aic_collector.team_preset import (  # noqa: E402
    next_start_index_in_slot,
    slot_range,
)


def _preset_with_members(tmp_path: Path, members: list[dict]) -> TeamPreset:
    lines = VALID_PRESET_YAML.splitlines()
    # Drop existing members: block (last 3 lines of VALID_PRESET_YAML).
    # Simpler: rebuild members yaml.
    member_yaml = "members:\n" + "\n".join(
        f"  - {{id: {m['id']}, name: \"{m['name']}\"}}" for m in members
    )
    text = VALID_PRESET_YAML.split("members:")[0] + member_yaml + "\n"
    p = tmp_path / "preset.yaml"
    p.write_text(text)
    preset = load_preset(p)
    assert preset is not None
    return preset


def test_slot_range_math(tmp_path: Path) -> None:
    preset = _preset_with_members(
        tmp_path, [{"id": f"M{i}", "name": f"u{i}"} for i in range(6)]
    )
    assert slot_range(preset, "M0") == (0, 100000)
    assert slot_range(preset, "M2") == (200000, 300000)
    assert slot_range(preset, "M5") == (500000, 600000)


def test_slot_range_unknown_member_raises(tmp_path: Path) -> None:
    preset = _preset_with_members(tmp_path, [{"id": "M0", "name": "alice"}])
    with pytest.raises(KeyError):
        slot_range(preset, "M99")


def test_next_start_index_empty_slot(tmp_path: Path) -> None:
    preset = _preset_with_members(
        tmp_path, [{"id": f"M{i}", "name": f"u{i}"} for i in range(3)]
    )
    root = tmp_path / "queue"
    assert next_start_index_in_slot(preset, "M1", root, "sfp") == 100000


def test_next_start_index_within_slot(tmp_path: Path) -> None:
    preset = _preset_with_members(
        tmp_path, [{"id": f"M{i}", "name": f"u{i}"} for i in range(3)]
    )
    root = tmp_path / "queue"
    pending = queue_dir(root, "sfp", QueueState.PENDING)
    pending.mkdir(parents=True)
    (pending / "config_sfp_100000.yaml").write_text("x: 1")
    (pending / "config_sfp_100001.yaml").write_text("x: 1")
    assert next_start_index_in_slot(preset, "M1", root, "sfp") == 100002


def test_next_start_index_ignores_other_slot(tmp_path: Path) -> None:
    preset = _preset_with_members(
        tmp_path, [{"id": f"M{i}", "name": f"u{i}"} for i in range(3)]
    )
    root = tmp_path / "queue"
    pending = queue_dir(root, "sfp", QueueState.PENDING)
    pending.mkdir(parents=True)
    # A file in M0's slot should not affect M2.
    (pending / "config_sfp_000500.yaml").write_text("x: 1")
    assert next_start_index_in_slot(preset, "M2", root, "sfp") == 200000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_team_preset.py -v`
Expected: FAIL — `slot_range` / `next_start_index_in_slot` not defined.

- [ ] **Step 3: Add slot math to team_preset.py**

Append to `src/aic_collector/team_preset.py`:

```python
import re

from aic_collector.job_queue.layout import QueueState, legacy_dir, queue_dir


def _member_index(preset: TeamPreset, member_id: str) -> int:
    for i, m in enumerate(preset.members):
        if m["id"] == member_id:
            return i
    raise KeyError(f"unknown member_id: {member_id}")


def slot_range(preset: TeamPreset, member_id: str) -> tuple[int, int]:
    """(slot_start, slot_end_exclusive) for the given member."""
    idx = _member_index(preset, member_id)
    start = idx * preset.shard_stride
    return (start, start + preset.shard_stride)


def next_start_index_in_slot(
    preset: TeamPreset,
    member_id: str,
    queue_root: Path,
    task_type: str,
) -> int:
    """next index bounded to the member's slot. Returns slot_start when empty."""
    slot_start, slot_end = slot_range(preset, member_id)
    pattern = re.compile(rf"^config_{re.escape(task_type)}_(\d+)\.yaml$")
    max_in_slot = slot_start - 1

    def _scan(d: Path) -> None:
        nonlocal max_in_slot
        if not d.exists():
            return
        for f in d.iterdir():
            if not f.is_file():
                continue
            m = pattern.match(f.name)
            if not m:
                continue
            n = int(m.group(1))
            if slot_start <= n < slot_end:
                if n > max_in_slot:
                    max_in_slot = n

    for state in QueueState:
        _scan(queue_dir(queue_root, task_type, state))
    _scan(legacy_dir(queue_root, task_type))

    return max_in_slot + 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_team_preset.py -v`
Expected: PASS for the 5 new slot-math tests.

- [ ] **Step 5: Commit**

```bash
git add src/aic_collector/team_preset.py tests/test_team_preset.py
git commit -m "feat(team_preset): add slot_range and next_start_index_in_slot"
```

---

## Task 5: Ledger operations — `append_claim`, `rollback_claim`, `adjust_claim_count`

**Files:**
- Modify: `src/aic_collector/team_preset.py`
- Modify: `tests/test_team_preset.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_team_preset.py`:

```python
import threading  # noqa: E402

from aic_collector.team_preset import (  # noqa: E402
    adjust_claim_count,
    append_claim,
    rollback_claim,
)


def _fresh_ledger(tmp_path: Path) -> Path:
    p = tmp_path / "seed_ledger.yaml"
    p.write_text("entries: []\n")
    return p


def test_append_claim_writes_entry(tmp_path: Path) -> None:
    ledger = _fresh_ledger(tmp_path)
    entry_id = append_claim(
        ledger,
        member_id="M0",
        task_type="sfp",
        base_seed=42,
        start_index=0,
        count=10,
        strategy="uniform",
        queue_root=str(tmp_path / "queue"),
        preset_hash="sha256:abc",
    )
    assert entry_id == 0
    data = yaml.safe_load(ledger.read_text())
    assert len(data["entries"]) == 1
    entry = data["entries"][0]
    assert entry["member_id"] == "M0"
    assert entry["start_index"] == 0
    assert entry["count"] == 10
    assert entry["preset_hash"] == "sha256:abc"
    assert "created_at" in entry
    assert "git_sha" in entry


def test_append_claim_two_entries(tmp_path: Path) -> None:
    ledger = _fresh_ledger(tmp_path)
    append_claim(
        ledger, member_id="M0", task_type="sfp", base_seed=42,
        start_index=0, count=10, strategy="uniform",
        queue_root=str(tmp_path), preset_hash="sha256:abc",
    )
    second = append_claim(
        ledger, member_id="M1", task_type="sfp", base_seed=42,
        start_index=100000, count=20, strategy="uniform",
        queue_root=str(tmp_path), preset_hash="sha256:abc",
    )
    assert second == 1
    data = yaml.safe_load(ledger.read_text())
    assert len(data["entries"]) == 2
    assert data["entries"][1]["member_id"] == "M1"


def test_rollback_claim_removes_last_entry(tmp_path: Path) -> None:
    ledger = _fresh_ledger(tmp_path)
    append_claim(
        ledger, member_id="M0", task_type="sfp", base_seed=42,
        start_index=0, count=10, strategy="uniform",
        queue_root=str(tmp_path), preset_hash="sha256:abc",
    )
    eid = append_claim(
        ledger, member_id="M1", task_type="sfp", base_seed=42,
        start_index=100000, count=20, strategy="uniform",
        queue_root=str(tmp_path), preset_hash="sha256:abc",
    )
    rollback_claim(ledger, eid)
    data = yaml.safe_load(ledger.read_text())
    assert len(data["entries"]) == 1
    assert data["entries"][0]["member_id"] == "M0"


def test_adjust_claim_count_updates_count(tmp_path: Path) -> None:
    ledger = _fresh_ledger(tmp_path)
    eid = append_claim(
        ledger, member_id="M0", task_type="sfp", base_seed=42,
        start_index=0, count=10, strategy="uniform",
        queue_root=str(tmp_path), preset_hash="sha256:abc",
    )
    adjust_claim_count(ledger, eid, actual_count=7)
    data = yaml.safe_load(ledger.read_text())
    assert data["entries"][eid]["count"] == 7
    # Immutable fields preserved.
    assert data["entries"][eid]["start_index"] == 0
    assert data["entries"][eid]["member_id"] == "M0"


def test_append_claim_concurrent(tmp_path: Path) -> None:
    ledger = _fresh_ledger(tmp_path)

    def worker(i: int) -> None:
        append_claim(
            ledger, member_id=f"M{i % 6}", task_type="sfp", base_seed=42,
            start_index=i * 1000, count=10, strategy="uniform",
            queue_root=str(tmp_path), preset_hash="sha256:abc",
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = yaml.safe_load(ledger.read_text())
    assert len(data["entries"]) == 10
    start_indices = sorted(e["start_index"] for e in data["entries"])
    assert start_indices == [i * 1000 for i in range(10)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_team_preset.py -v`
Expected: FAIL — `append_claim` / `rollback_claim` / `adjust_claim_count` not defined.

- [ ] **Step 3: Add ledger operations to team_preset.py**

Append to `src/aic_collector/team_preset.py`:

```python
import fcntl
import subprocess
from datetime import datetime, timezone


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        sha = out.stdout.strip() or "uncommitted"
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        return f"dirty:{sha}" if dirty else sha
    except (FileNotFoundError, subprocess.SubprocessError):
        return "uncommitted"


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_rewrite(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False))
    tmp.replace(path)


def append_claim(
    ledger_path: Path,
    *,
    member_id: str,
    task_type: str,
    base_seed: int,
    start_index: int,
    count: int,
    strategy: str,
    queue_root: str,
    preset_hash: str,
) -> int:
    """Atomic append under fcntl.flock. Returns index of the new entry."""
    entry = {
        "member_id": member_id,
        "task_type": task_type,
        "base_seed": base_seed,
        "start_index": start_index,
        "count": count,
        "strategy": strategy,
        "queue_root": queue_root,
        "preset_hash": preset_hash,
        "git_sha": _git_sha(),
        "created_at": _iso_utc_now(),
    }
    with open(ledger_path, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            data = yaml.safe_load(f.read()) or {"entries": []}
            if "entries" not in data or data["entries"] is None:
                data["entries"] = []
            data["entries"].append(entry)
            entry_id = len(data["entries"]) - 1
            _atomic_rewrite(ledger_path, data)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return entry_id


def rollback_claim(ledger_path: Path, entry_id: int) -> None:
    """Remove entry_id only if it is still the last entry."""
    with open(ledger_path, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            data = yaml.safe_load(f.read()) or {"entries": []}
            entries = data.get("entries") or []
            if entries and entry_id == len(entries) - 1:
                entries.pop()
                data["entries"] = entries
                _atomic_rewrite(ledger_path, data)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def adjust_claim_count(ledger_path: Path, entry_id: int, actual_count: int) -> None:
    """Permitted mutation: update count of an existing entry."""
    with open(ledger_path, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            data = yaml.safe_load(f.read()) or {"entries": []}
            entries = data.get("entries") or []
            if 0 <= entry_id < len(entries):
                entries[entry_id]["count"] = int(actual_count)
                data["entries"] = entries
                _atomic_rewrite(ledger_path, data)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_team_preset.py -v`
Expected: PASS for all ledger tests.

- [ ] **Step 5: Commit**

```bash
git add src/aic_collector/team_preset.py tests/test_team_preset.py
git commit -m "feat(team_preset): append-only ledger with rollback and count adjust"
```

---

## Task 6: Ship `preset.yaml` + empty `seed_ledger.yaml` scaffolding

**Files:**
- Create: `configs/team/preset.yaml`
- Create: `configs/team/seed_ledger.yaml`

- [ ] **Step 1: Write preset.yaml**

Create `configs/team/preset.yaml`:

```yaml
# Team-wide preset for trial_1 data collection.
# Edits are changes to the team contract — open a PR.

version: 1
team:
  base_seed: 42
  shard_stride: 100000
  index_width: 6
sampling:
  strategy: uniform
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
  target_cycling:  false
  fixed_target:
    sfp: {rail: 0, port: "sfp_port_0"}
    sc:  null
tasks:
  sfp_default_count: 1000
  sc_default_count:  0
members:
  - {id: M0, name: "alice"}
  - {id: M1, name: "bob"}
  - {id: M2, name: "carol"}
  - {id: M3, name: "dave"}
  - {id: M4, name: "eve"}
  - {id: M5, name: "frank"}
```

- [ ] **Step 2: Write empty seed_ledger.yaml**

Create `configs/team/seed_ledger.yaml`:

```yaml
# Append-only ledger of data-collection claims.
# Each entry is written by the webapp on Submit. Only `count` may be mutated
# post-hoc (partial write_plans failure). All other fields are immutable.

entries: []
```

- [ ] **Step 3: Smoke-test the preset loads**

Run: `uv run python -c "from aic_collector.team_preset import load_preset; from pathlib import Path; p = load_preset(Path('configs/team/preset.yaml')); print(p.preset_hash, p.members)"`
Expected: Prints `sha256:<hex>` and 6 member dicts.

- [ ] **Step 4: Commit**

```bash
git add configs/team/preset.yaml configs/team/seed_ledger.yaml
git commit -m "chore(team): add preset.yaml and empty seed_ledger.yaml"
```

---

## Task 7: Extract submit logic and cover with integration tests

We build the pure-function submit logic before touching Streamlit so the behavior is testable.

**Files:**
- Modify: `src/aic_collector/team_preset.py` (add `submit_team_claim`)
- Create: `tests/test_webapp_team_mode.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_webapp_team_mode.py`:

```python
#!/usr/bin/env python3
"""Integration tests for the team-mode submit logic."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

import pytest  # noqa: E402
import yaml  # noqa: E402

from aic_collector.job_queue import QueueState, queue_dir  # noqa: E402
from aic_collector.team_preset import (  # noqa: E402
    SlotExhausted,
    load_preset,
    submit_team_claim,
)

TEMPLATE_PATH = PROJECT_DIR / "configs/community_random_config.yaml"


VALID_PRESET_YAML = """
version: 1
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
tasks:
  sfp_default_count: 1000
  sc_default_count: 0
members:
  - {id: M0, name: "alice"}
  - {id: M1, name: "bob"}
"""


@pytest.fixture
def env(tmp_path: Path):
    preset_path = tmp_path / "preset.yaml"
    ledger_path = tmp_path / "seed_ledger.yaml"
    queue_root = tmp_path / "queue"
    preset_path.write_text(VALID_PRESET_YAML)
    ledger_path.write_text("entries: []\n")
    preset = load_preset(preset_path)
    assert preset is not None
    return preset, ledger_path, queue_root


def test_submit_happy_path(env) -> None:
    preset, ledger, root = env
    result = submit_team_claim(
        preset=preset, ledger_path=ledger, queue_root=root,
        member_id="M0", task_type="sfp", count=3,
        template_path=TEMPLATE_PATH,
    )
    assert result.written_count == 3
    assert result.start_index == 0
    files = sorted((queue_dir(root, "sfp", QueueState.PENDING)).glob("*.yaml"))
    assert [f.name for f in files] == [
        "config_sfp_000000.yaml",
        "config_sfp_000001.yaml",
        "config_sfp_000002.yaml",
    ]
    data = yaml.safe_load(ledger.read_text())
    assert len(data["entries"]) == 1
    assert data["entries"][0]["start_index"] == 0
    assert data["entries"][0]["count"] == 3


def test_submit_twice_same_member_continues(env) -> None:
    preset, ledger, root = env
    submit_team_claim(
        preset=preset, ledger_path=ledger, queue_root=root,
        member_id="M0", task_type="sfp", count=2,
        template_path=TEMPLATE_PATH,
    )
    r2 = submit_team_claim(
        preset=preset, ledger_path=ledger, queue_root=root,
        member_id="M0", task_type="sfp", count=2,
        template_path=TEMPLATE_PATH,
    )
    assert r2.start_index == 2


def test_submit_different_members_are_disjoint(env) -> None:
    preset, ledger, root = env
    r0 = submit_team_claim(
        preset=preset, ledger_path=ledger, queue_root=root,
        member_id="M0", task_type="sfp", count=2,
        template_path=TEMPLATE_PATH,
    )
    r1 = submit_team_claim(
        preset=preset, ledger_path=ledger, queue_root=root,
        member_id="M1", task_type="sfp", count=2,
        template_path=TEMPLATE_PATH,
    )
    assert r0.start_index == 0
    assert r1.start_index == 100000


def test_submit_slot_exhaustion_blocks(env) -> None:
    preset, ledger, root = env
    with pytest.raises(SlotExhausted):
        submit_team_claim(
            preset=preset, ledger_path=ledger, queue_root=root,
            member_id="M0", task_type="sfp",
            count=preset.shard_stride + 1,   # exceeds slot
            template_path=TEMPLATE_PATH,
        )
    data = yaml.safe_load(ledger.read_text())
    assert data["entries"] == []
    assert not (queue_dir(root, "sfp", QueueState.PENDING)).exists() \
        or list(queue_dir(root, "sfp", QueueState.PENDING).glob("*.yaml")) == []


def test_submit_rolls_back_on_sample_failure(env, monkeypatch) -> None:
    preset, ledger, root = env
    from aic_collector import team_preset as tp

    def boom(*_a, **_k):
        raise RuntimeError("sampler exploded")

    monkeypatch.setattr(tp, "sample_scenes", boom)

    with pytest.raises(RuntimeError):
        submit_team_claim(
            preset=preset, ledger_path=ledger, queue_root=root,
            member_id="M0", task_type="sfp", count=3,
            template_path=TEMPLATE_PATH,
        )
    data = yaml.safe_load(ledger.read_text())
    assert data["entries"] == []


def test_submit_records_fixed_target(env) -> None:
    preset, ledger, root = env
    submit_team_claim(
        preset=preset, ledger_path=ledger, queue_root=root,
        member_id="M0", task_type="sfp", count=2,
        template_path=TEMPLATE_PATH,
    )
    # Every produced config carries the fixed target.
    files = sorted((queue_dir(root, "sfp", QueueState.PENDING)).glob("*.yaml"))
    for f in files:
        assert "nic_card_mount_0" in f.read_text() or "sfp_port_0" in f.read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_webapp_team_mode.py -v`
Expected: FAIL — `submit_team_claim` not defined.

- [ ] **Step 3: Add `submit_team_claim` to `team_preset.py`**

Append to `src/aic_collector/team_preset.py`:

```python
from dataclasses import dataclass as _dc

from aic_collector.job_queue import write_plans
from aic_collector.sampler import sample_scenes


@_dc(frozen=True)
class SubmitResult:
    start_index: int
    written_count: int
    entry_id: int


def _training_cfg_from_preset(preset: TeamPreset) -> dict:
    """Compose the training-sampler config dict from preset fields."""
    return {
        "parameters": {
            "strategy": preset.strategy,
            "ranges": preset.ranges,
        },
        "scene": preset.scene,
        "collection": {
            "seed": preset.base_seed,
            "fixed_target": preset.scene.get("fixed_target") or {},
        },
    }


def submit_team_claim(
    *,
    preset: TeamPreset,
    ledger_path: Path,
    queue_root: Path,
    member_id: str,
    task_type: str,
    count: int,
    template_path: Path,
) -> SubmitResult:
    """Ledger-append + sample + write, with rollback on failure."""
    slot_start, slot_end = slot_range(preset, member_id)
    start_idx = next_start_index_in_slot(preset, member_id, queue_root, task_type)
    if start_idx + count > slot_end:
        raise SlotExhausted(
            f"slot {slot_start}..{slot_end} remaining "
            f"{slot_end - start_idx} < requested {count}"
        )

    entry_id = append_claim(
        ledger_path,
        member_id=member_id,
        task_type=task_type,
        base_seed=preset.base_seed,
        start_index=start_idx,
        count=count,
        strategy=preset.strategy,
        queue_root=str(queue_root),
        preset_hash=preset.preset_hash,
    )

    try:
        plans = sample_scenes(
            training_cfg=_training_cfg_from_preset(preset),
            task_type=task_type,
            count=count,
            seed=preset.base_seed,
            start_index=start_idx,
        )
    except Exception:
        rollback_claim(ledger_path, entry_id)
        raise

    try:
        written = write_plans(
            plans, queue_root, template_path, index_width=preset.index_width
        )
    except Exception:
        # Keep whatever landed on disk; adjust ledger count if we know it.
        existing = list(queue_dir(queue_root, task_type, QueueState.PENDING).glob(
            f"config_{task_type}_*.yaml"
        ))
        partial = sum(1 for f in existing if f.stat().st_size > 0)
        if partial != count:
            adjust_claim_count(ledger_path, entry_id, partial)
        raise

    return SubmitResult(start_index=start_idx, written_count=len(written), entry_id=entry_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_webapp_team_mode.py -v`
Expected: PASS for all 6 tests.

- [ ] **Step 5: Commit**

```bash
git add src/aic_collector/team_preset.py tests/test_webapp_team_mode.py
git commit -m "feat(team_preset): add submit_team_claim integration entry point"
```

---

## Task 8: Wire preset + submit into the Streamlit Task Management tab

**Files:**
- Modify: `src/aic_collector/webapp.py:1119-1540` (Task Management tab block)

Streamlit logic is hard to unit-test end-to-end; we rely on Task 7's integration tests for correctness and verify the UI manually.

- [ ] **Step 1: Import team_preset at the top of the tab block**

Near `src/aic_collector/webapp.py:1119` (inside `with tab_manage:` block, after the existing `from aic_collector.job_queue import ...` block), add:

```python
from aic_collector.team_preset import (
    PresetError,
    SlotExhausted,
    load_preset,
    slot_range,
    next_start_index_in_slot,
    submit_team_claim,
)

PRESET_PATH = PROJECT_DIR / "configs/team/preset.yaml"
LEDGER_PATH = PROJECT_DIR / "configs/team/seed_ledger.yaml"
```

- [ ] **Step 2: Load preset and render banner at the top of the tab**

Immediately after `st.subheader("📋 작업 관리")` (around webapp.py:1132), insert:

```python
try:
    _preset = load_preset(PRESET_PATH)
    _preset_error: str | None = None
except PresetError as exc:
    _preset = None
    _preset_error = str(exc)

if _preset_error is not None:
    st.error(f"❌ 팀 프리셋 오류 — preset.yaml 복구 필요: {_preset_error}")
elif _preset is not None:
    _hash_short = _preset.preset_hash.split(":")[1][:7]
    st.info(f"🔒 팀 프리셋 v1 활성 — `{_hash_short}`  \n"
            "설정 위젯은 잠겨 있습니다. Member만 선택해서 적재하세요.")
```

- [ ] **Step 3: Add Member dropdown + slot info card**

After the banner, before the existing 큐 루트 input, insert:

```python
if _preset is not None:
    member_options = [m["id"] for m in _preset.members]
    mgr_member_id = st.selectbox(
        "Member ID",
        options=[""] + member_options,
        index=0,
        key="mgr_member_id",
        help="팀 수집 모드. 선택 즉시 start_index가 자동 산출됩니다.",
    )
    if mgr_member_id:
        _slot_start, _slot_end = slot_range(_preset, mgr_member_id)
        _next_idx = next_start_index_in_slot(
            _preset, mgr_member_id,
            Path(st.session_state.get("mgr_queue_root", str(PROJECT_DIR / "configs/train"))),
            "sfp",
        )
        _used = _next_idx - _slot_start
        _remaining = _slot_end - _next_idx
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("슬롯", f"{_slot_start}~{_slot_end - 1}")
        col_b.metric("사용", f"{_used} / {_preset.shard_stride}")
        col_c.metric("여유", _remaining)
        st.caption(f"다음 파일: `config_sfp_{_next_idx:0{_preset.index_width}d}.yaml`")
    else:
        st.warning("Member를 선택하세요.")
else:
    mgr_member_id = ""
```

- [ ] **Step 4: Lock existing widgets when preset active**

Find the widget blocks (`mgr_sc_count`, `mgr_nic_max`, `mgr_sc_max`, `mgr_target_cycling`, the range sliders in the `Parameters` expander, `mgr_param_strategy`, `mgr_seed`). For each, thread a `disabled=_preset is not None` parameter and set the `value` to the preset equivalent. Example for `mgr_sc_count`:

```python
mgr_sc_count = st.number_input(
    "SC configs",
    min_value=0, max_value=10000,
    value=(_preset.tasks.get("sc_default_count", 0) if _preset is not None else 10),
    step=2,
    key="mgr_sc_count",
    disabled=_preset is not None,
    help=("team 프리셋 활성 시 0으로 고정" if _preset is not None else "(기존 도움말)"),
)
```

Apply the same pattern (locked to preset value, `disabled=_preset is not None`) to:
- `mgr_sfp_count` — replace default value with `_preset.tasks.get("sfp_default_count", 20)` when `_preset` and `mgr_member_id`, but clamp by remaining. Leave editable so the member can reduce.
- `mgr_nic_fixed` → locked to `_preset.scene["nic_count_range"][0] == _preset.scene["nic_count_range"][1]`
- `mgr_nic_max` → `_preset.scene["nic_count_range"][1]`
- `mgr_sc_fixed`, `mgr_sc_max` → mirror
- `mgr_target_cycling` → `_preset.scene["target_cycling"]`
- Range sliders → locked to preset ranges
- `mgr_param_strategy` → `_preset.strategy`
- `mgr_seed` → `_preset.base_seed`

For `mgr_sfp_count`, clamp when preset active:

```python
if _preset is not None and mgr_member_id:
    _max_sfp = max(0, _remaining)
else:
    _max_sfp = 10000
mgr_sfp_count = st.number_input(
    "SFP configs",
    min_value=0,
    max_value=_max_sfp,
    value=min(
        _preset.tasks.get("sfp_default_count", 20) if _preset else 20,
        _max_sfp,
    ),
    step=10,
    key="mgr_sfp_count",
    help="팀 프리셋 활성 시 남은 슬롯 여유로 자동 상한 설정.",
)
```

- [ ] **Step 5: Gate and reroute the submit button**

Find the existing submit handler (webapp.py:1509 block) and wrap it:

```python
if _preset is not None:
    submit_disabled = not mgr_member_id or int(mgr_sfp_count) == 0
    if st.button(
        f"✅ 팀 모드 적재 ({mgr_member_id or '—'})",
        key="mgr_submit_team",
        disabled=submit_disabled,
    ):
        try:
            result = submit_team_claim(
                preset=_preset,
                ledger_path=LEDGER_PATH,
                queue_root=mgr_queue_root,
                member_id=mgr_member_id,
                task_type="sfp",
                count=int(mgr_sfp_count),
                template_path=PROJECT_DIR / "configs/community_random_config.yaml",
            )
            st.success(
                f"{result.written_count}개 적재 완료 "
                f"({mgr_member_id}, start_index={result.start_index})"
            )
            st.rerun()
        except SlotExhausted as exc:
            st.error(f"슬롯 여유 부족: {exc}")
        except Exception as exc:
            st.error(f"적재 실패 (롤백됨): {exc}")
else:
    # Existing solo-mode submit block — leave untouched.
    ...
```

The existing solo-mode code (the current `sample_scenes(...)` + `write_plans(...)` call) stays in the `else:` branch verbatim, so users without a preset see no change.

- [ ] **Step 6: Manual smoke test — solo mode unchanged**

Temporarily rename `configs/team/preset.yaml` to `preset.yaml.off`.

Run: `./scripts/run_webapp.sh`
Expected: Task Management tab looks identical to pre-change. Create 2 SFP configs, see `config_sfp_000000.yaml` / `config_sfp_000001.yaml` (6-digit from Task 1).

Restore the file: `mv configs/team/preset.yaml.off configs/team/preset.yaml`.

- [ ] **Step 7: Manual smoke test — team mode works**

Run: `./scripts/run_webapp.sh`
Expected:
- Blue banner `🔒 팀 프리셋 v1 — <hash>`
- Member dropdown with 6 options
- Select `M2` → metrics show slot `200000~299999`, 사용 0, 여유 100000, next file `config_sfp_200000.yaml`
- Range sliders / strategy / seed visibly greyed-out
- SFP configs default 1000, clamped to ≤ 100000
- Set count=3, click `✅ 팀 모드 적재 (M2)` → success toast, 3 files in `configs/train/sfp/pending/`
- `configs/team/seed_ledger.yaml` has 1 entry with `start_index: 200000`, `count: 3`
- Rerun without changes → next file shows `config_sfp_200003.yaml`

- [ ] **Step 8: Commit**

```bash
git add src/aic_collector/webapp.py
git commit -m "feat(webapp): team-mode Task Management tab with preset + ledger"
```

---

## Task 9: Run the full test suite and update documentation

**Files:**
- Modify: `README.md` (team-mode quickstart)
- Modify: `docs/usage-guide.md` (if it describes the Task Management tab)

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -v`
Expected: All tests pass, no regressions.

- [ ] **Step 2: Lint**

Run: `uv run ruff check src/aic_collector/team_preset.py src/aic_collector/webapp.py src/aic_collector/sampler.py src/aic_collector/job_queue/writer.py tests/test_team_preset.py tests/test_webapp_team_mode.py tests/test_job_queue.py tests/test_training_sampler.py`
Expected: no errors.

- [ ] **Step 3: Append a Team Mode section to README.md**

Add this block after the existing "Quickstart" section (look for the section header; insert before the next top-level `##`):

```markdown
## Team Mode (Six-Person Collection)

When `configs/team/preset.yaml` exists, the Task Management tab switches to
team mode:

1. Select your Member ID from the dropdown.
2. `start_index` and all randomization parameters are auto-filled from the
   preset and locked.
3. Set `SFP configs` (defaults to `sfp_default_count`, capped at your slot's
   remaining capacity) and click `✅ 팀 모드 적재`.
4. Each submit appends to `configs/team/seed_ledger.yaml`; commit that file
   after collection to share claim history with the team.

To run solo (ignore team mode), delete or rename `configs/team/preset.yaml`.
```

- [ ] **Step 4: Commit**

```bash
git add README.md docs/usage-guide.md
git commit -m "docs: document team-mode collection workflow"
```

---

## Self-Review

**Spec coverage:**
- Architecture invariants 1–4 → Task 3/4/5/6/7 (preset SoT), Task 7 (slot math), Task 7 (submit), Task 5 (ledger)
- `preset.yaml` schema → Task 6
- `seed_ledger.yaml` schema → Task 5 (entry shape) + Task 6 (scaffold)
- `team_preset.py` API → Tasks 3–5, 7
- `webapp.py` UI changes → Task 8
- `sampler.py` `fixed_target` → Task 2
- `writer.py` `index_width=6` → Task 1
- Data-flow happy path → Task 7 test `test_submit_happy_path`
- Failure modes → Task 7 tests `test_submit_slot_exhaustion_blocks`, `test_submit_rolls_back_on_sample_failure`
- Edge cases (solo mode fallback, mixed-width filenames, concurrent append, preset_hash stability) → Tasks 1, 3, 5, 8 (solo manual test)
- Manual checklist items → Task 8 Steps 6–7, Task 9 Step 1

**Placeholder scan:** no `TBD` / `TODO` / `implement later` / `similar to Task N`. Every code step has a code block.

**Type consistency:** `TeamPreset` fields match across Tasks 3/4/5/7/8. `submit_team_claim` signature in Task 7 matches the call site in Task 8 Step 5. `next_start_index_in_slot` parameter order (`preset, member_id, queue_root, task_type`) is identical in Tasks 4 / 7 / 8.

No gaps found.
