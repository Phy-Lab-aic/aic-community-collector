# Team Seed Sharding for trial_1 Collection

- **Date:** 2026-04-20
- **Scope:** Streamlit `Task Management` tab, sampler, job_queue writer
- **Status:** Design approved, pending implementation plan

## Problem

Six engineers will collect simulation data in parallel for a fixed scenario (`trial_1`: SFP cable insertion at `nic_rail_0` / `sfp_port_0`). The current webapp lets each member generate configs independently, but:

- `start_index` is auto-computed from the local queue, so every member starts at `0` — filenames (`config_sfp_0000.yaml`) collide when outputs are merged.
- `mgr_seed` is a single integer with no team coordination — per-sample seeds (`per_seed = seed + start_index + i`) collide too.
- No enforcement of shared randomization range, sampling strategy, or scene parameters — any member drifting from the team preset silently biases the merged dataset.
- `SFP_TARGET_CYCLE` rotates through 10 (rail, port) pairs, so a trial_1-only collection ends up with `nic_rail_1..4` samples mixed in.

## Goals

1. Guarantee disjoint filenames and disjoint per-sample seed spaces across 6 members with zero manual coordination.
2. Enforce a single source of truth for randomization range, strategy, scene parameters, and fixed target — encoded in a git-tracked preset file.
3. Produce an append-only audit trail sufficient to reproduce any collected sample later.
4. Preserve full backward compatibility for solo users and existing CLI workflows.

## Non-Goals

- Per-member range slabs or edge-zone assignments (Codex + Gemini advisor consensus: full-range + disjoint seeds is statistically sufficient for trial_1).
- NFS / shared-filesystem queue roots (MVP assumes local roots).
- Automatic git push of ledger updates.
- A team dashboard or cross-member coverage visualization (deferred; revisit after first collection round reveals real distribution gaps).

## Architecture

### Invariants

1. **Single source of truth.** `configs/team/preset.yaml` determines `base_seed`, `shard_stride`, ranges, strategy, and `fixed_target`. The UI reads but does not modify this file.
2. **Disjoint start_index.** `start_index = member_id_index * shard_stride`. Only the Member dropdown changes it.
3. **Append-safe within slot.** Same member re-entering uses `next_sample_index` bounded to `[slot_start, slot_end)`.
4. **Immutable audit trail.** Every submit appends an entry to `configs/team/seed_ledger.yaml`.

### Layering

```
Streamlit webapp.py (Task Management tab)
  ├── reads  → configs/team/preset.yaml
  ├── writes → configs/team/seed_ledger.yaml (append-only)
  └── writes → configs/train/<task>/pending/config_<task>_NNNNNN.yaml

aic_collector.team_preset  (new)
  - load_preset, slot_range, next_start_index_in_slot, append_claim

aic_collector.sampler  (extended)
  - honors collection.fixed_target, replacing cycle with length-1 list

aic_collector.job_queue.writer  (extended)
  - index_width default 4 → 6
```

### Changed Files

| File | Change | ~LoC |
|---|---|---|
| `configs/team/preset.yaml` | new | 25 |
| `configs/team/seed_ledger.yaml` | new (empty entries list) | 5 |
| `src/aic_collector/team_preset.py` | new module | 80 |
| `src/aic_collector/webapp.py` | preset load, member UI, lock widgets, ledger append | 120 |
| `src/aic_collector/sampler.py` | `fixed_target` branch | 15 |
| `src/aic_collector/job_queue/writer.py` | `index_width` default to 6 | 2 |
| `tests/test_team_preset.py` | new | 80 |
| `tests/test_training_sampler.py` | extend for fixed_target | 30 |
| `tests/test_job_queue.py` | extend for index_width=6 | 20 |
| `tests/test_webapp_team_mode.py` | new submit-flow tests | 60 |

Total ~440 LoC. All existing public APIs stay backward compatible.

## Components

### C1. `configs/team/preset.yaml`

```yaml
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

### C2. `configs/team/seed_ledger.yaml`

```yaml
entries:
  - member_id: M0
    task_type: sfp
    base_seed: 42
    start_index: 0
    count: 1000
    strategy: uniform
    queue_root: "configs/train"
    preset_hash: "sha256:..."
    git_sha: "abc1234"
    created_at: "2026-04-21T10:00:00Z"
```

Entries are append-only. The one permitted in-place mutation is `count` correction after a partial `write_plans` failure (see Data Flow); all other fields are immutable once written.

### C3. `team_preset.py` API

```python
@dataclass(frozen=True)
class TeamPreset:
    base_seed: int
    shard_stride: int
    index_width: int
    strategy: Literal["uniform", "lhs"]
    ranges: dict[str, list[float] | float]
    scene: dict[str, Any]
    tasks: dict[str, int]
    members: list[dict[str, str]]
    preset_hash: str  # sha256 of canonical dump

def load_preset(path: Path = Path("configs/team/preset.yaml")) -> TeamPreset | None:
    """Return None if file absent (solo mode). Raise PresetError on malformed."""

def slot_range(preset: TeamPreset, member_id: str) -> tuple[int, int]:
    """(slot_start, slot_end_exclusive). Raise KeyError if member unknown."""

def next_start_index_in_slot(
    preset: TeamPreset, member_id: str, queue_root: Path, task_type: str
) -> int:
    """next_sample_index bounded to slot. Returns slot_start when slot is empty."""

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
    """Atomic append with fcntl.flock. Returns entry_id (= index of the
    newly appended entry, valid only until the next append on this file)."""

def rollback_claim(ledger_path: Path, entry_id: int) -> None:
    """Remove the entry at entry_id only if it is still the last one.
    No-op if another append has happened since (shouldn't occur under flock)."""

def adjust_claim_count(ledger_path: Path, entry_id: int, actual_count: int) -> None:
    """Permitted mutation: update the `count` field of an existing entry
    when write_plans finished partially. All other fields stay immutable."""

class PresetError(Exception): ...
class SlotExhausted(Exception): ...
```

### C4. webapp.py Task Management tab changes

Render order when preset is present:

1. Banner `[Team preset v1 (abc1234)]` at top of tab.
2. Member dropdown (options from `preset.members`). Submit button stays `disabled` until selected.
3. Read-only cards: slot range, next filename, used count, remaining capacity.
4. Existing widgets (SC count, NIC/SC count range, target cycling, range sliders, strategy, seed) forced to preset values with `disabled=True`. No unlock affordance in MVP.
5. SFP count `number_input` with `max_value = slot_end - next_idx`, default `preset.tasks.sfp_default_count` (clamped to remaining).
6. Submit: call `append_claim` → `sample_scenes` → `write_plans`. On failure after `append_claim`, call `rollback_claim`.

When preset is absent (solo mode) the tab behaves identically to today.

### C5. sampler.py change

```python
# Replaces the existing cycle = SFP_TARGET_CYCLE / SC_TARGET_CYCLE selection.
fixed = training_cfg.get("collection", {}).get("fixed_target", {}).get(task_type)
if fixed:
    cycle = [(int(fixed["rail"]), str(fixed["port"]))]
else:
    cycle = SFP_TARGET_CYCLE if task_type == "sfp" else SC_TARGET_CYCLE
```

All downstream logic (`global_index % len(cycle)`, `rng.integers`) works unchanged with a length-1 cycle.

### C6. writer.py change

Default `index_width=4` → `6` in both `write_plan` and `write_plans`. The existing `next_sample_index` regex (`\d+`) already matches mixed widths.

## Data Flow

### Submit path (happy)

```
1. start_idx = next_start_index_in_slot(preset, member_id, queue_root, "sfp")
   assert start_idx + count <= slot_end
2. entry_id = append_claim(ledger_path, member_id, "sfp", ...)
3. plans = sample_scenes(training_cfg_with_fixed_target, "sfp", count,
                         seed=preset.base_seed, start_index=start_idx)
4. write_plans(plans, queue_root, template, index_width=preset.index_width)
5. st.success(...); st.rerun()
```

### Failure modes and responses

| Failure | Response |
|---|---|
| `preset.yaml` malformed | Top-level error banner, submit disabled, no solo fallback |
| Member not selected | Submit disabled |
| `start_idx + count > slot_end` | Validation error before any write, submit disabled |
| `flock` timeout (5s) on ledger | Retry once, then error; no sample_scenes call |
| `sample_scenes` raises after claim | `rollback_claim` removes last entry; no files written |
| `write_plans` fails mid-batch | Keep written files, update ledger entry `count` to actual written, warn user |
| `preset_hash` differs from past entries | Warning banner, submit still allowed |

### Reproduction flow

1. Find target entry in `seed_ledger.yaml`.
2. `git checkout entry.git_sha`.
3. Verify current `preset_hash` matches `entry.preset_hash`.
4. Select the same Member and same count in webapp → identical files emitted.

`per_seed = base_seed + start_index + i` in sampler is already deterministic; no new code needed for reproduction.

## Edge Cases

- **Absent preset.yaml** → solo mode, no behavior change for existing users.
- **Malformed preset.yaml** → fail loud, do not silently fall back to solo mode.
- **Adding a new member** → append to `preset.members`; existing slots must not shift.
- **Same member re-submits** → `next_start_index_in_slot` continues within the slot.
- **Manually deleted queue files** → next_idx advances past max-seen; ledger-vs-files drift raises a warning badge.
- **NFS queue_root** → best-effort heuristic warning; out of MVP scope.
- **Mixed 4-digit + 6-digit filenames** → `\d+` regex handles both; no migration tool provided.
- **`base_seed` or `shard_stride` changed in preset** → preset_hash differs from past entries; `shard_stride` change triggers preflight check against past entries and blocks if any overflow.
- **CLI users bypassing the preset** → sampler still works; if `collection.fixed_target` absent, legacy cycle is used.
- **Concurrent tabs on same machine** → `fcntl.flock` serializes; second tab sees up-to-date `next_idx` after first commits.
- **Dirty git tree** → entry records `"dirty:<sha>"`, warning badge displayed.

## Testing

### Unit (`tests/test_team_preset.py`)

- `load_preset` happy / missing-returns-None / malformed-raises / missing-field-raises
- `preset_hash` stable across key order, stable across unrelated whitespace
- `slot_range` math, unknown member raises
- `next_start_index_in_slot` empty / within-slot / ignores-other-slot
- `append_claim` atomic append; concurrent 10-thread stress; rollback removes only last entry

### Sampler (`tests/test_training_sampler.py`)

- `fixed_target` on sfp → all samples carry rail=0, port="sfp_port_0"
- `fixed_target` absent → legacy cycle preserved (regression guard)
- `fixed_target` on vs off → per_seed unchanged for same start_index (target change doesn't perturb RNG draws)

### Writer (`tests/test_job_queue.py`)

- default `index_width` produces 6-digit filenames
- explicit `index_width=4` preserved (backward compat)
- `next_sample_index` reads mixed 4/6-digit files correctly
- `next_start_index_in_slot` respects slot bounds (unit-tested via `team_preset.py`)

### Integration (`tests/test_webapp_team_mode.py`)

- extract submit logic into a pure function, test with `tmp_path`
- happy submit → 1 ledger entry + N files at correct path
- re-submit same member → start_index continues
- count > slot remaining → `SlotExhausted`, 0 files, 0 ledger entries
- `sample_scenes` raises → ledger rolled back, 0 files
- `write_plans` mid-batch failure → files kept, ledger count adjusted, warning surfaced
- solo mode (no preset.yaml) → original code path, no ledger touched

### Manual checklist (pre-merge)

- Open webapp in solo mode → UI unchanged
- Add `preset.yaml` → banner + Member dropdown appear; other widgets disabled
- M0, M1 each submit 100 → filenames `000000..000099` and `100000..100199` created
- `seed_ledger.yaml` diff readable in PR
- Re-submit M0 → filenames continue from `000100`
- Change `base_seed`, reload → preset_hash warning banner
- Run sampler CLI directly → unaffected by preset

## Rollout

1. Land behind preset file absence: merging the code alone changes nothing for users without `configs/team/preset.yaml`.
2. Open a PR that adds `configs/team/preset.yaml` with the six members. Each engineer pulls and sees the locked UI.
3. First collection round: 1000 samples per member (total 6000). After completion, revisit distribution coverage before deciding on Approach C (team dashboard) or range-slab experiments.

## Open Questions

None at design time. Items to revisit after the first round:

- Is 6000 episodes enough for the downstream policy, or do we need a second round?
- Do observed failure clusters justify per-member edge slabs in a follow-up?
- Does anyone need to run from a shared NAS? If yes, add flock-NFS handling and queue_root sentinel.
